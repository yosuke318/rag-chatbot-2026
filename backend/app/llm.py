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


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """テキスト群をベクトル化。input_type は "document" か "query"。"""
    result = _voyage.embed(texts, model=EMBED_MODEL, input_type=input_type)
    return result.embeddings


def embed_query(text: str) -> list[float]:
    return embed_texts([text], input_type="query")[0]


SYSTEM_PROMPT = (
    "あなたは社内文書アシスタントです。以下のコンテキストだけを根拠に、"
    "日本語で簡潔に回答してください。コンテキストに答えが無い場合は"
    "「資料からは分かりません」と答えてください。"
)


def rank_by_relevance(question: str, passages: list[str]) -> list[int]:
    """候補文書を質問への関連が高い順に並べ替え、その番号リストを返す。

    Claudeに番号付きで候補を渡し、「関連順に番号を返せ」と指示する。
    出力パースは防御的に（数字だけ抽出・範囲内・重複排除）。
    """
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
    context_block = "\n\n---\n\n".join(contexts) if contexts else "(該当なし)"
    user_content = f"# コンテキスト\n{context_block}\n\n# 質問\n{question}"

    response = _anthropic.messages.create(
        model=CHAT_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
