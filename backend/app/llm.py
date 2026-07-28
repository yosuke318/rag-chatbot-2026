"""LLM 呼び出し薄ラッパ。埋め込み=Voyage、回答生成=Claude(SDK直叩き)。

Anthropicには埋め込みAPIが無いため、埋め込みだけ別プロバイダ(Voyage)を使う。
ここを差し替えれば OpenAI 埋め込みやローカルモデルにも切り替えられる。
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import anthropic
import voyageai

from app.config import (
    ANTHROPIC_API_KEY,
    CHAT_MODEL,
    CONTEXT_CONCURRENCY,
    CONTEXT_MODEL,
    EMBED_MODEL,
    RERANK_MODEL,
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


def _voyage_call(call, retry_waits: list[int] | None):
    """Voyage APIを1回呼ぶ。429なら retry_waits の秒数だけ待って再試行する。

    retry_waits: レート制限(429)を受けたときに待つ秒数の並び。既定の None は
      「再試行しない」＝ 429 をそのまま投げる。APIリクエストの処理中に何十秒も
      待つと利用者を待たせるため、Web経路は既定のまま 429 を即返す
      （main.py の例外ハンドラ）。待っても困らないバッチ処理（app.seed）だけが
      待ち時間を渡す。無料枠(3 RPM)は文書を4件以上連続投入すると必ず当たる。
    """
    for wait in [*(retry_waits or []), None]:
        try:
            return call()
        except voyageai.error.RateLimitError:
            if wait is None:  # 待ち時間を使い切った
                raise
            logger.warning("Voyage APIのレート制限。%d秒待って再試行します", wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


# ============================================================
# 埋め込み (Voyage)
# ============================================================


def embed_texts(
    texts: list[str],
    input_type: str = "document",
    retry_waits: list[int] | None = None,
) -> list[list[float]]:
    """テキスト群をベクトル化。input_type は "document" か "query"。

    retry_waits の意味は _voyage_call を参照。
    """
    _require(VOYAGE_API_KEY, "VOYAGE_API_KEY")
    result = _voyage_call(
        lambda: _voyage.embed(texts, model=EMBED_MODEL, input_type=input_type),
        retry_waits,
    )
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
# 検索・リランク
#   検索そのもの（ベクトル / 字面 / BM25 → RRF）は retrieval.py 側にあり、
#   必要なAPIは質問のベクトル化に使う Voyage の埋め込みだけ。Claudeは呼ばない。
#   下の2つは「任意」のリランク（USE_RERANK有効時のみ）。どちらも
#   (question, passages) -> 関連順の番号リスト という同じ形で、切り替えて比較できる。
#     voyage_rerank     … Voyage の専用リランクAPI(rerank-2)。既定。Claude不要
#     rank_by_relevance … Claudeに番号を並べ替えさせるプロンプト式。比較用
#   素の検索・検索評価そのものに生成APIは要らない。
# ============================================================


def voyage_rerank(
    question: str, passages: list[str], retry_waits: list[int] | None = None
) -> list[int]:
    """Voyage の専用リランクAPIで並べ替え、関連が高い順の番号リストを返す。

    生成モデルに番号を書かせる（rank_by_relevance）のに比べて:
      - 安い・速い    … 順位付け専用の小さいモデルで、出力はスコアだけ
      - 順位が安定する … 生成のゆらぎが無く、同じ入力なら同じ順位
      - 取りこぼしが無い … 全候補に必ずスコアが付く（番号の書き漏らしが起きない）
    埋め込みで既にVoyageを使っているので、キーも契約もそのまま流用できる。

    APIは relevance_score の降順で results を返すので、その index を並べるだけ。
    ※リランクは質問1件につき1リクエスト（埋め込みのようにまとめられない）。
      無料枠(3 RPM)では評価を回すと4問目で429になるため、retry_waits を渡すか
      支払い方法の登録で上限を緩和すること。
    """
    _require(VOYAGE_API_KEY, "VOYAGE_API_KEY")
    if not passages:
        return []
    result = _voyage_call(
        lambda: _voyage.rerank(question, passages, model=RERANK_MODEL),
        retry_waits,
    )
    return [r.index for r in result.results]


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
#
#   ★チャンク単位の根拠明示★
#     コンテキストに [1] [2] … と番号を振って渡し、回答の各文の末尾に
#     その文の根拠になった番号を書かせる。返ってきた本文の [n] は、
#     呼び出し側が渡した contexts の n 番目（1始まり）に対応する
#     ＝ 回答のどの主張がどのチャンク由来かを利用者が自分で検証できる。
#     出典名だけでは「文書のどこか」までしか分からず、検証の役に立たないため。
# ============================================================

SYSTEM_PROMPT = (
    "あなたは文書検索アシスタントです。以下のコンテキストだけを根拠に、"
    "日本語で簡潔に回答してください。コンテキストに答えが無い場合は"
    "「資料からは分かりません」と答えてください。"
    "\n\n各コンテキストには [1] [2] のような番号が付いています。"
    "回答の各文には、その文の根拠になったコンテキストの番号を文末に [1] の形式で"
    "必ず付けてください（複数の根拠があれば [1][3] のように並べる）。"
    "番号は与えられたものだけを使い、存在しない番号を書かないでください。"
    "根拠が無い文（「資料からは分かりません」など）には番号を付けないでください。"
)


def number_contexts(contexts: list[str]) -> str:
    """コンテキストを [1] [2] … の番号付きブロックにまとめる（1始まり）。

    番号は回答本文の引用マーカー [n] と対応する。回答から根拠チャンクを
    引き当てる唯一の手がかりなので、並び順は呼び出し側の contexts と必ず一致させる。
    """
    if not contexts:
        return "(該当なし)"
    return "\n\n---\n\n".join(f"[{i}] {c}" for i, c in enumerate(contexts, start=1))


def generate_answer(question: str, contexts: list[str]) -> str:
    """検索した関連チャンクをコンテキストに与えて回答を生成する。

    戻り値の本文には [n] の引用マーカーが含まれる（n は contexts の1始まりの位置）。
    """
    _require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    user_content = f"# コンテキスト\n{number_contexts(contexts)}\n\n# 質問\n{question}"

    response = _anthropic.messages.create(
        model=CHAT_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
