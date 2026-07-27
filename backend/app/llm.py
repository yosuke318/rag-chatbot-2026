"""LLM 呼び出し薄ラッパ。埋め込み=Voyage、回答生成=Claude(SDK直叩き)。

Anthropicには埋め込みAPIが無いため、埋め込みだけ別プロバイダ(Voyage)を使う。
ここを差し替えれば OpenAI 埋め込みやローカルモデルにも切り替えられる。
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import anthropic
import voyageai

from app.config import (
    ANTHROPIC_API_KEY,
    CHAT_MODEL,
    CONTEXT_CONCURRENCY,
    CONTEXT_MODEL,
    EMBED_MODEL,
    VOYAGE_API_KEY,
)

_voyage = voyageai.Client(api_key=VOYAGE_API_KEY)
_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

logger = logging.getLogger(__name__)


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
# 取り込み: チャンクへの文脈付与 (contextual retrieval)
#   チャンク単体だと「これを超える場合は所属長の承認を要する」のように
#   主語も金額も分からない断片ができ、埋め込みも字面も質問に当たらない。
#   そこで「文書のどこの何の話か」を1〜2文で書かせ、埋め込む直前に前置する。
#   ※前置するのは埋め込み用のテキストだけ。回答生成に渡す本文（chunks.content）は
#     原文のまま残す（生成した文脈が回答の根拠として混ざらないように）。
# ============================================================

CONTEXT_SYSTEM_PROMPT = (
    "あなたは検索インデックスの前処理を行うアシスタントです。"
    "与えられた文書全体の中で、指定されたチャンクが何の話題かを日本語1〜2文で述べてください。"
    "目的は、チャンク単体では意味が取れない指示語・省略された主語・所属する条や章を補い、"
    "検索で見つけやすくすることです。"
    "チャンクの要約ではなく『文書内での位置づけ』を書き、前置き・見出し・箇条書きは使わず"
    "説明文だけを返してください。"
)


def generate_chunk_context(document: str, chunk: str) -> str:
    """文書全体を踏まえて、そのチャンクの位置づけを1〜2文で書かせる。

    ★プロンプトキャッシュの効かせ方★
      プロンプトは tools → system → messages の順に組み立てられ、
      キャッシュは「先頭からの一致」で効く。つまり毎回変わるものを後ろに置くのが鉄則。
      ここでは文書全体（毎回同じ）を先に置いて cache_control を付け、
      チャンク（毎回変わる）をその後ろに置く。こうすると2件目以降は文書部分が
      キャッシュから読まれ、入力コストが約1/10になる。逆順にすると一切効かない。
      ※キャッシュには最小長（モデルにより 512〜4096 トークン）がある。短い文書では
        エラーにならず単に効かないだけなので、効いているかは usage で確かめること。
    """
    _require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    response = _anthropic.messages.create(
        model=CONTEXT_MODEL,
        max_tokens=300,
        system=CONTEXT_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<document>\n{document}\n</document>",
                        # ここまで（system + 文書）をキャッシュする
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"<chunk>\n{chunk}\n</chunk>\n\n"
                            "このチャンクの文書内での位置づけを1〜2文で書いてください。"
                        ),
                    },
                ],
            }
        ],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    return text.strip()


def generate_chunk_contexts(document: str, chunks: list[str]) -> list[str]:
    """複数チャンクぶんの文脈をまとめて生成する。失敗したチャンクは空文字。

    1件目だけ先に直列で投げるのは、キャッシュが「最初のレスポンスが流れ始めてから」
    読めるようになるため。いきなり並列で投げると全件がキャッシュ書き込み側に回り、
    誰も読めずに割高になる。1件目でキャッシュを作ってから残りを並列化する。

    1チャンクの失敗で取り込み全体を落とさない（呼び出し側が見出しなどで代替できる）。
    ただし★黙って落とさない★: 失敗はまとめて WARNING に出す。ここを無言にすると
    「APIキーが空で全件フォールバックしていた」ことに気づけない（実際に踏んだ）。
    """
    if not chunks:
        return []

    failures: list[Exception] = []

    def one(chunk: str) -> str:
        try:
            return generate_chunk_context(document, chunk)
        except Exception as exc:  # APIキー未設定・レート制限・APIエラー等
            failures.append(exc)
            return ""

    first = one(chunks[0])
    if len(chunks) == 1:
        contexts = [first]
    else:
        with ThreadPoolExecutor(max_workers=max(1, CONTEXT_CONCURRENCY)) as pool:
            rest = list(pool.map(one, chunks[1:]))
        contexts = [first, *rest]

    if failures:
        logger.warning(
            "文脈生成に失敗: %d/%d チャンク（見出しで代替します）。最初のエラー: %s: %s",
            len(failures),
            len(chunks),
            type(failures[0]).__name__,
            failures[0],
        )
    return contexts


# ============================================================
# 検索（Voyageのみ / Claudeは任意のリランクだけ）
#   検索そのもの（ベクトル / 字面 / BM25 → RRF）は retrieval.py 側にあり、
#   必要なAPIは質問のベクトル化に使う Voyage の埋め込みだけ。Claudeは呼ばない。
#   下の rank_by_relevance は「任意」のLLMリランク（USE_RERANK有効時のみ）で、
#   これだけは例外的にClaudeを使う。素の検索・検索評価に生成APIは要らない。
# ============================================================


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


# ============================================================
# 回答生成 (Claude)
#   検索で拾ったチャンクを根拠に、実際の回答文を作る工程。ここはClaudeが必須。
# ============================================================

SYSTEM_PROMPT = (
    "あなたは文書検索アシスタントです。以下のコンテキストだけを根拠に、"
    "日本語で簡潔に回答してください。コンテキストに答えが無い場合は"
    "「資料からは分かりません」と答えてください。"
)


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
