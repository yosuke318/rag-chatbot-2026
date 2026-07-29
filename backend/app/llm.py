"""LLM 呼び出し薄ラッパ。埋め込み=Voyage、回答生成=Claude(SDK直叩き)。

Anthropicには埋め込みAPIが無いため、埋め込みだけ別プロバイダ(Voyage)を使う。
ここを差し替えれば OpenAI 埋め込みやローカルモデルにも切り替えられる。
"""
import base64
import logging
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import anthropic
import voyageai

from app.config import (
    ANTHROPIC_API_KEY,
    CAPTION_CONCURRENCY,
    CAPTION_MODEL,
    CHAT_MODEL,
    CONTEXT_CONCURRENCY,
    CONTEXT_MODEL,
    EMBED_MODEL,
    MULTIMODAL_EMBED_MODEL,
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
# マルチモーダル埋め込み (Voyage) — 案B（5-2）
#   voyage-multimodal-3 は画像とテキストを「同じ空間」に埋め込む。つまり
#   テキストの質問ベクトルと画像ベクトルを直接コサインで比較できる。
#   ★EMBED_MODEL(voyage-3.5)とは別の空間★なので混ぜて比較してはいけない。
#   下の2つは必ずペアで使う（画像側=document / 質問側=query）。
# ============================================================


def _multimodal_embed(
    inputs: list[list], input_type: str, retry_waits: list[int] | None
) -> list[list[float]]:
    """multimodal_embed の共通呼び出し。inputs は「1件 = 要素の並び」の二重リスト。

    要素には文字列と PIL.Image を混ぜられる（今は片方だけしか使っていない）。
    """
    _require(VOYAGE_API_KEY, "VOYAGE_API_KEY")
    result = _voyage_call(
        lambda: _voyage.multimodal_embed(
            inputs, model=MULTIMODAL_EMBED_MODEL, input_type=input_type
        ),
        retry_waits,
    )
    return result.embeddings


def embed_images(
    images: list[bytes], retry_waits: list[int] | None = None
) -> list[list[float]]:
    """画像のバイト列をまとめてベクトル化する（取り込み側 = document）。

    SDK は PIL.Image を受け取るのでここで開く。Pillow は画像抽出(app.parsers)で
    既に依存に入っている。

    ★開いた画像は途中で失敗しても閉じる★
      内包表記で一度に開くと、途中の1枚が壊れていて Image.open が投げたとき、
      それより前に開いた分の参照がどこにも残らず閉じられない。ExitStack に
      登録しながら開けば、どこで失敗しても開いた分だけが確実に閉じられる。
    """
    import io
    from contextlib import ExitStack

    from PIL import Image

    with ExitStack() as stack:
        opened = [stack.enter_context(Image.open(io.BytesIO(data))) for data in images]
        return _multimodal_embed([[img] for img in opened], "document", retry_waits)


def embed_multimodal_queries(
    texts: list[str], retry_waits: list[int] | None = None
) -> list[list[float]]:
    """質問テキストを★画像と同じ空間★でベクトル化する（検索側 = query）。

    embed_texts と同じ「質問のベクトル化」だが、モデルが違うので別関数にしてある。
    間違えて embed_texts の結果で image_embedding を検索すると、エラーにならず
    ただ無意味な順位が返る（次元は同じ1024）ので、呼び分けを型ではなく名前で守る。
    """
    return _multimodal_embed([[t] for t in texts], "query", retry_waits)


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
# 取り込み: 画像の自動キャプション — 案A（5-2）
#   画像はそのままでは字面にもベクトルにも当たらない。そこで Claude に
#   「検索で引っかかる説明文」を書かせ、その文を既存のテキスト経路
#   （埋め込み + 名詞の字面検索）へ流す。
#
#   ★この説明文は"索引"であって"根拠"ではない★
#     説明文に書かれなかったことは後から問えない、というのが言語化方式の弱点。
#     5-3 で回答生成には原本画像そのものを渡すようにし、この文の役割を
#     「検索で見つけるため」だけに限定する（言語化を索引に格下げする）。
# ============================================================

CAPTION_SYSTEM_PROMPT = (
    "あなたは検索インデックスの前処理を行うアシスタントです。"
    "与えられた画像を、後から日本語のテキスト検索で見つけられるような説明文にしてください。"
    "\n\n書くこと: 画像の種類（表・グラフ・写真・スクリーンショット・図解など）、"
    "見出しや軸ラベル・凡例に書かれている文字、扱っている対象や項目名、"
    "読み取れる傾向や大小関係。数値が読める場合は代表的なものを含めてください。"
    "\n\n書かないこと: 推測や評価、前置き、見出し、箇条書き。"
    "説明文だけを日本語3〜5文で返してください。"
    "画像から何も読み取れない場合は「読み取れる情報がありません」とだけ返してください。"
)


def generate_image_caption(
    image: bytes, media_type: str, source: str, label: str
) -> str:
    """画像1枚を「検索で引っかかる説明文」にする。

    source（文書名）と label（「3ページ目」等）を添えるのは、画像単体では
    分からない所属を説明文に含められるようにするため。図の中身は画像そのものに
    全部写っているので、文書本文までは渡さない（ページ画像はページの文字も
    画像に含まれる ＝ 本文を別途渡すのは二重コストになる）。
    """
    _require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    response = _anthropic.messages.create(
        model=CAPTION_MODEL,
        max_tokens=500,
        system=CAPTION_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image).decode("ascii"),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"この画像は文書「{source}」の{label}から取り出したものです。"
                            "検索用の説明文を書いてください。"
                        ),
                    },
                ],
            }
        ],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    return text.strip()


def generate_image_captions(
    images: list[tuple[bytes, str, str]], source: str
) -> list[str]:
    """複数画像ぶんの説明文をまとめて生成する。失敗した画像は空文字。

    images: (バイト列, MIMEタイプ, ラベル) の並び。

    generate_chunk_contexts と同じ方針で、1枚の失敗が取り込み全体を落とさない
    ようにしつつ★黙って落とさない★（失敗はまとめて WARNING）。
    文脈生成と違い共通の前置き（文書全体）が無いので、プロンプトキャッシュを
    作るための「1件目だけ直列」はしない ＝ 最初から全件並列で投げる。
    """
    if not images:
        return []

    failures: list[Exception] = []

    def one(item: tuple[bytes, str, str]) -> str:
        data, media_type, label = item
        try:
            return generate_image_caption(data, media_type, source, label)
        except Exception as exc:  # APIキー未設定・レート制限・APIエラー等
            failures.append(exc)
            return ""

    with ThreadPoolExecutor(max_workers=max(1, CAPTION_CONCURRENCY)) as pool:
        captions = list(pool.map(one, images))

    if failures:
        logger.warning(
            "画像キャプションの生成に失敗: %d/%d 枚（その画像は検索に出ません）。"
            "最初のエラー: %s: %s",
            len(failures),
            len(images),
            type(failures[0]).__name__,
            failures[0],
        )
    return captions


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
#
#   ★原本画像を根拠にする（5-3）★
#     ヒットしたチャンクが文書内の図表なら、言語化テキストではなく
#     ★画像そのもの★をコンテキストに載せる（ImageContext）。
#     言語化は「検索で見つけるための索引」に格下げし、判断は毎回原本に対して
#     行わせる ＝ 言語化した時点で書かれなかったことも後から問える。
#     （2023年方式の「図を逐一言語化して、以後はその文章だけを見る」の弱点を外す）
# ============================================================


@dataclass(frozen=True)
class ImageContext:
    """コンテキストに載せる原本画像1枚（5-3）。

    contexts に str の代わりにこれを混ぜると、その番号の根拠が画像になる。
    label は「3ページ目」「シート「売上」の画像1」のような★由来の名前★で、
    中身の説明ではない（説明はさせない ＝ 中身は画像から読ませる）。
    """

    data: bytes
    media_type: str
    label: str


# Claude に渡せる画像フォーマット（app.parsers.SUPPORTED_IMAGE_FORMATS と対）。
# 取り込み時にこの範囲へ揃えてあるが、S3に古い形式が残っている場合の防波堤として
# 添付側(app.main)でも検査する。
ANSWER_IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

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

# 画像を1枚でも載せるときだけ SYSTEM_PROMPT に足す指示。
# ★常に足さない★のは、画像が無い質問のプロンプトを従来と1文字も変えないため
# （変えると eval の数字が画像機能と無関係に動き、過去の測定と比較できなくなる）。
IMAGE_SYSTEM_PROMPT = (
    "\n\nコンテキストには文書内の図・表・チャートの画像が含まれることがあります。"
    "画像の中身は必ずあなた自身が画像を見て読み取ってください。"
    "画像の直前に置かれた短い見出しは「文書のどこにある図か」を示すだけのもので、"
    "中身の説明ではありません。見出しから中身を推測しないでください。"
    "画像から読み取れないことは推測せず、その点は分からないと答えてください。"
)


def _system_prompt(contexts: list) -> str:
    """コンテキストに画像があるときだけ画像用の指示を足したシステムプロンプト。"""
    if any(isinstance(c, ImageContext) for c in contexts):
        return SYSTEM_PROMPT + IMAGE_SYSTEM_PROMPT
    return SYSTEM_PROMPT


def number_contexts(contexts: list[str]) -> str:
    """コンテキストを [1] [2] … の番号付きブロックにまとめる（1始まり）。

    番号は回答本文の引用マーカー [n] と対応する。回答から根拠チャンクを
    引き当てる唯一の手がかりなので、並び順は呼び出し側の contexts と必ず一致させる。

    ★テキストだけのとき用★。画像が混ざる場合は content block の並びが要るので
    _context_blocks を使う（番号の振り方はどちらも同じ）。
    """
    if not contexts:
        return "(該当なし)"
    return "\n\n---\n\n".join(f"[{i}] {c}" for i, c in enumerate(contexts, start=1))


def _context_blocks(contexts: list) -> list[dict]:
    """コンテキストを Anthropic の content block の並びにする（画像を含む場合）。

    画像は「番号と由来を書いたテキスト → 画像」の順に置く。逆にすると、どの番号の
    根拠がその画像なのかが対応付かず、引用マーカーがずれる。
    """
    blocks: list[dict] = []
    for i, c in enumerate(contexts, start=1):
        if isinstance(c, ImageContext):
            blocks.append({"type": "text", "text": f"[{i}] 次の画像（{c.label}）"})
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": c.media_type,
                        "data": base64.b64encode(c.data).decode("ascii"),
                    },
                }
            )
        else:
            blocks.append({"type": "text", "text": f"[{i}] {c}"})
    return blocks


CITATION_MARKER = re.compile(r"\s*\[\d+\]")


def strip_citations(text: str) -> str:
    """本文から引用マーカー [n] を取り除く。

    過去の回答を履歴として渡すときに使う。★番号は毎回付け直される★ため
    （今回の検索結果の並びで決まる）、古い [1] を残したままにすると
    「前の回答で [1] と書いたから」と別のチャンクを指す番号を再利用されうる。
    """
    return CITATION_MARKER.sub("", text)


def _answer_messages(
    question: str, contexts: list, history: list[dict] | None = None
) -> list[dict]:
    """回答生成に渡すメッセージ列を組み立てる。

    contexts の各要素は本文テキスト(str)か原本画像(ImageContext)。

    並びは [過去のやり取り…, 今回の質問(コンテキスト付き)]。
    ★コンテキストは毎回「今回の質問」にだけ付ける★
      過去の質問に当時のコンテキストまで足すと、古い根拠が新しい回答に混ざる。
      根拠は常に今回の検索結果だけに限る。画像も同じで、履歴には残さない
      （過去のやり取りに画像を積むと、会話が続くほど入力が膨らみ続ける）。
    """
    messages = [
        {
            "role": m["role"],
            "content": strip_citations(m["content"])
            if m["role"] == "assistant"
            else m["content"],
        }
        for m in (history or [])
    ]
    if any(isinstance(c, ImageContext) for c in contexts):
        content = [
            {"type": "text", "text": "# コンテキスト"},
            *_context_blocks(contexts),
            {"type": "text", "text": f"# 質問\n{question}"},
        ]
    else:
        # ★画像が無いときは従来と同じ1本のテキスト★
        # プロンプトを1文字も変えないことで、画像機能を入れる前後で eval の
        # 数字が地続きに比較できる（形だけ変えて数字が動くのを避ける）。
        content = f"# コンテキスト\n{number_contexts(contexts)}\n\n# 質問\n{question}"
    messages.append({"role": "user", "content": content})
    return messages


def generate_answer(
    question: str, contexts: list, history: list[dict] | None = None
) -> str:
    """検索した関連チャンクをコンテキストに与えて回答を生成する。

    contexts の要素は本文テキスト(str)か原本画像(ImageContext)。画像を混ぜると
    その番号の根拠が「画像そのもの」になる（5-3）。
    戻り値の本文には [n] の引用マーカーが含まれる（n は contexts の1始まりの位置）。
    history に直近のやり取りを渡すと、続きの質問（「その上限は？」等）に答えられる。
    """
    _require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    response = _anthropic.messages.create(
        model=CHAT_MODEL,
        max_tokens=1024,
        system=_system_prompt(contexts),
        messages=_answer_messages(question, contexts, history),
    )
    return "".join(block.text for block in response.content if block.type == "text")


def stream_answer(
    question: str, contexts: list, history: list[dict] | None = None
) -> Iterator[str]:
    """generate_answer のストリーミング版。生成された文字を順に yield する。

    回答が出るまで数秒待たされると「固まった」ように見えるため、書けたところから
    表示する。渡すもの（system / メッセージ列）は非ストリーミング版と同一で、
    受け取り方だけが違う ＝ 同じ質問なら同じ品質の回答になる（画像の扱いも同じ）。
    """
    _require(ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")
    with _anthropic.messages.stream(
        model=CHAT_MODEL,
        max_tokens=1024,
        system=_system_prompt(contexts),
        messages=_answer_messages(question, contexts, history),
    ) as stream:
        yield from stream.text_stream
