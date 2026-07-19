"""LLM 呼び出し薄ラッパ。埋め込み=Voyage、回答生成=Claude(SDK直叩き)。

Anthropicには埋め込みAPIが無いため、埋め込みだけ別プロバイダ(Voyage)を使う。
ここを差し替えれば OpenAI 埋め込みやローカルモデルにも切り替えられる。
"""
import re

import anthropic
import voyageai

from app.config import (
    ANTHROPIC_API_KEY,
    CHAT_MODEL,
    EMBED_MODEL,
    VOYAGE_API_KEY,
)

_voyage = voyageai.Client(api_key=VOYAGE_API_KEY)
_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class MissingAPIKey(RuntimeError):
    """必要なAPIキーが未設定。

    キーが空のままSDKを呼ぶと、Anthropicは通信前に TypeError を投げるため
    AuthenticationError では捕捉できない。事前に検査して明示的に落とす。
    """


def _require(key: str | None, name: str) -> None:
    if not key:
        raise MissingAPIKey(name)


# ============================================================
# 埋め込み (Voyage)
# ============================================================


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """テキスト群をベクトル化。input_type は "document" か "query"。"""
    _require(VOYAGE_API_KEY, "VOYAGE_API_KEY")
    result = _voyage.embed(texts, model=EMBED_MODEL, input_type=input_type)
    return result.embeddings


def embed_query(text: str) -> list[float]:
    """検索クエリ1件をベクトル化。"""
    return embed_texts([text], input_type="query")[0]


# ============================================================
# 検索 / 回答生成 (Claude)
# ============================================================

SYSTEM_PROMPT = (
    "あなたは文書検索アシスタントです。以下のコンテキストだけを根拠に、"
    "日本語で簡潔に回答してください。コンテキストに答えが無い場合は"
    "「資料からは分かりません」と答えてください。"
)


def rank_by_relevance(question: str, passages: list[str]) -> list[int]:
    """候補文書を質問への関連が高い順に並べ替え、その番号リストを返す。

    Claudeに番号付きで候補を渡し、「関連順に番号を返せ」と指示する。
    出力パースは防御的に（数字だけ抽出・範囲内・重複排除）。
    """
    _require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    numbered = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(passages))
    prompt = (
        f"質問: {question}\n\n"
        f"以下の文書を、質問への関連が高い順に並べ替えてください。\n"
        f"番号だけをカンマ区切りで返してください（例: 3,0,2,1）。説明は不要です。\n\n"
        f"{numbered}"
    )
    response = _anthropic.messages.create(
        model=CHAT_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")

    order: list[int] = []
    for token in re.findall(r"\d+", text):
        i = int(token)
        if 0 <= i < len(passages) and i not in order:
            order.append(i)
    return order


def generate_answer(question: str, contexts: list[str]) -> str:
    """検索した関連チャンクをコンテキストに与えて回答を生成する。"""
    _require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    context_block = "\n\n---\n\n".join(contexts) if contexts else "(該当なし)"
    user_content = f"# コンテキスト\n{context_block}\n\n# 質問\n{question}"

    response = _anthropic.messages.create(
        model=CHAT_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
