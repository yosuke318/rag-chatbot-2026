"""FastAPI エントリポイント。最小RAGループ: /ingest で入れて /chat で聞く。"""
from __future__ import annotations

import json
import logging
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from typing import Optional

import anthropic
import voyageai.error
from fastapi import APIRouter, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import apikeys, charts, conversations, saved_questions, scopes, storage
from app.config import (
    ADMIN_TOKEN,
    ANSWER_IMAGE_MAX_BYTES,
    ANSWER_MAX_IMAGES,
    IMAGE_INDEX_METHOD,
    RETRIEVERS_DEFAULT,
    UPLOAD_MAX_BYTES,
)
from app.conversations import UnknownConversation
from app.db import get_conn, init_db
from app.eval import EXPECTED_KINDS, evaluate, load_questions
from app.ingest import (
    IMAGE_INDEX_METHODS,
    UnsupportedFileType,
    extract_images,
    extract_text,
    ingest_text,
    reindex_images,
)
from app.llm import (
    ANSWER_IMAGE_MEDIA_TYPES,
    ImageContext,
    MissingAPIKey,
    generate_answer,
    stream_answer,
)
from app.retrieval import (
    FUSION_PARAM_SPECS,
    UnknownReranker,
    UnknownRetriever,
    hybrid_search,
    preview,
    retriever_infos,
    search_stages,
)
from app.schema_labels import SCHEMA_LABELS
from app.schemas import (
    ChartReadRequest,
    ChartReadResponse,
    ChatRequest,
    ChatResponse,
    DocumentsResponse,
    DocumentSummariesResponse,
    ErrorResponse,
    EvalQuestion,
    EvalQuestionRequest,
    EvalQuestionsResponse,
    EvalReport,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    ProjectRequest,
    ProjectsResponse,
    PublicChatRequest,
    RetrieversResponse,
    SavedQuestionRequest,
    SavedQuestionResponse,
    SavedQuestionsResponse,
    SchemaResponse,
    ScopeResponse,
    SearchResponse,
    TopicRequest,
    TopicsResponse,
    VerifyReport,
)
from app.seed import RETRY_WAITS


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 起動時にスキーマを用意
    yield


app = FastAPI(title="RAG Inspector API", lifespan=lifespan)

logger = logging.getLogger(__name__)


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """空文字・空白だけの入力を「未指定(NULL)」に正規化する。

    project / topic は NULL を「どこにも属さない共通」の意味で使う。
    ここで正規化しないと2つの困りごとが起きる:
      - 登録側: 空欄が空文字として保存され、NULL と別物になって絞り込みから漏れる
      - 検索側: `?project=` のような空クエリが「未指定」ではなく `project=''`
        での絞り込みになり、常に0件になる
    どちらもAPI境界で潰すのが一番安全なので、入口で揃える。
    """
    if value is None:
        return None
    return value.strip() or None


def _error_payload(code: str, message: str, hint: str = "", detail: str = "") -> dict:
    """UIがそのまま表示できる形のエラー本文（ErrorResponse と同じ形）。

    ストリーミング(/chat/stream)は途中で失敗してもHTTPステータスを変えられず、
    エラーを本文の中で伝えるしかない。そのときも通常のエラー応答と同じ形に
    揃えられるよう、本文の組み立てだけを切り出してある。
    """
    return {"error": code, "message": message, "hint": hint, "detail": detail}


def _error(status: int, code: str, message: str, hint: str = "", detail: str = ""):
    """UIがそのまま表示できる形のエラー応答。"""
    return JSONResponse(
        status_code=status, content=_error_payload(code, message, hint, detail)
    )


def _stream_error(exc: Exception) -> tuple[str, str, str, str]:
    """生成中に出た例外を (code, message, hint, detail) に写す。

    ストリームを開いた後は例外ハンドラ（＝ステータス付きの応答）を通せないので、
    同じ内容を error イベントとして流すためのもの。生成中に出るのは
    Anthropic 側のエラーだけなので、そこだけを扱う。
    """
    if isinstance(exc, MissingAPIKey):
        return (
            "missing_api_key",
            "生成API（Claude）のAPIキーが未設定です。",
            f"backend/.env の {exc} を設定して再起動してください。",
            "",
        )
    if isinstance(exc, anthropic.RateLimitError):
        return (
            "anthropic_rate_limit",
            "生成API（Claude）のレート制限に達しました。少し待ってから再試行してください。",
            "",
            str(exc),
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return (
            "anthropic_auth",
            "生成API（Claude）の認証に失敗しました。",
            "backend/.env の ANTHROPIC_API_KEY を設定してください。",
            str(exc),
        )
    return (
        "anthropic_error",
        "生成API（Claude）の呼び出しに失敗しました。",
        "",
        str(exc),
    )


def _reject_empty_question(req: ChatRequest):
    """質問が空なら400。空だと無意味な検索と生成API呼び出しになるので手前で弾く。"""
    if req.question.strip():
        return None
    return _error(
        400,
        "invalid_question",
        "質問は必須です。",
        "question を入力してください。",
        "",
    )


class AdminForbidden(Exception):
    """/admin/* のトークンが合わない（未設定時は発生しない）。"""


@app.exception_handler(MissingAPIKey)
async def missing_api_key(request: Request, exc: Exception):
    """キーが空のままSDKを呼ぶ前に落とす。SDKに任せると通信前のTypeErrorになり
    AuthenticationError として扱えないため、事前検査した結果をここで返す。"""
    name = str(exc)
    which = "生成API（Claude）" if "ANTHROPIC" in name else "埋め込みAPI（Voyage）"
    extra = (
        "検索の内訳だけなら /search が使えます（Anthropicキー不要・Voyageキーは必要）。"
        if "ANTHROPIC" in name
        else ""
    )
    return _error(
        401,
        "missing_api_key",
        f"{which}のAPIキーが未設定です。",
        f"backend/.env の {name} を設定して再起動してください。{extra}",
        "",
    )


@app.exception_handler(AdminForbidden)
async def admin_forbidden(request: Request, exc: Exception):
    return _error(
        403,
        "admin_forbidden",
        "管理用APIのトークンが不正です。",
        "X-Admin-Token ヘッダに backend/.env の ADMIN_TOKEN と同じ値を付けてください。",
        "",
    )


@app.exception_handler(UnknownRetriever)
async def unknown_retriever(request: Request, exc: Exception):
    return _error(
        400,
        "unknown_retriever",
        "指定された検索手法が不正です。",
        str(exc),
        "",
    )


@app.exception_handler(UnknownConversation)
async def unknown_conversation(request: Request, exc: Exception):
    """存在しない会話IDは黙って新規作成せず404にする（履歴が繋がらない事故に気づけるように）。"""
    return _error(
        404,
        "unknown_conversation",
        "指定された会話が見つかりません。",
        "conversation_id を外すと新しい会話として始められます。",
        str(exc),
    )


@app.exception_handler(UnknownReranker)
async def unknown_reranker(request: Request, exc: Exception):
    return _error(
        400,
        "unknown_reranker",
        "指定されたリランク方式が不正です。",
        str(exc),
        "",
    )


@app.exception_handler(UnsupportedFileType)
async def unsupported_file_type(request: Request, exc: UnsupportedFileType):
    ext = exc.ext or "(拡張子なし)"
    return _error(
        415,
        "unsupported_file_type",
        f"未対応のファイル形式です: {ext}",
        "対応形式は テキスト系（.txt / .md / .csv など）と PDF / XLSX / PPTX です。",
        "",
    )


# --- 埋め込みAPI(Voyage)のエラー ---------------------------------------------
# 検索も文書登録も毎回Voyageを呼ぶため、レート制限や鍵の不備がそのまま500に
# なっていた。原因が分かる形で返す。


@app.exception_handler(voyageai.error.RateLimitError)
async def voyage_rate_limit(request: Request, exc: Exception):
    logger.warning("Voyage rate limit: %s", exc)
    return _error(
        429,
        "voyage_rate_limit",
        "埋め込みAPI（Voyage）のレート制限に達しました。少し待ってから再試行してください。",
        "支払い方法が未登録だと 3リクエスト/分 に制限されます。"
        "dashboard.voyageai.com で登録すると緩和されます（無料枠は維持）。",
        str(exc),
    )


@app.exception_handler(voyageai.error.AuthenticationError)
async def voyage_auth(request: Request, exc: Exception):
    return _error(
        401,
        "voyage_auth",
        "埋め込みAPI（Voyage）の認証に失敗しました。",
        "backend/.env の VOYAGE_API_KEY を確認してください。",
        str(exc),
    )


@app.exception_handler(voyageai.error.VoyageError)
async def voyage_other(request: Request, exc: Exception):
    logger.exception("Voyage error")
    return _error(
        502,
        "voyage_error",
        "埋め込みAPI（Voyage）の呼び出しに失敗しました。",
        "",
        str(exc),
    )


# --- 生成API(Anthropic)のエラー ----------------------------------------------
# /chat のみで発生する。キー未設定ならここに来る。


@app.exception_handler(anthropic.RateLimitError)
async def anthropic_rate_limit(request: Request, exc: Exception):
    return _error(
        429,
        "anthropic_rate_limit",
        "生成API（Claude）のレート制限に達しました。少し待ってから再試行してください。",
        "",
        str(exc),
    )


@app.exception_handler(anthropic.AuthenticationError)
async def anthropic_auth(request: Request, exc: Exception):
    return _error(
        401,
        "anthropic_auth",
        "生成API（Claude）の認証に失敗しました。回答生成にはAnthropicのAPIキーが必要です。",
        "backend/.env の ANTHROPIC_API_KEY を設定してください。"
        "検索の内訳だけなら /search が使えます（Anthropicキー不要・Voyageキーは必要）。",
        str(exc),
    )


@app.exception_handler(anthropic.APIError)
async def anthropic_other(request: Request, exc: Exception):
    logger.exception("Anthropic error")
    return _error(
        502,
        "anthropic_error",
        "生成API（Claude）の呼び出しに失敗しました。",
        "",
        str(exc),
    )


# 各エンドポイントが返しうるエラーもスキーマに載せる
# （フロントは生成された ErrorResponse 型でそのまま扱える）
_ERRORS = {
    401: {"model": ErrorResponse, "description": "APIキー未設定・認証失敗"},
    429: {"model": ErrorResponse, "description": "レート制限"},
    502: {"model": ErrorResponse, "description": "外部API呼び出し失敗"},
}


@app.exception_handler(apikeys.ApiKeyError)
async def invalid_api_key(request: Request, exc: Exception):
    """公開API(/v1)の認証失敗。どのキーが無効かは返さない（探索の手掛かりを与えない）。"""
    return _error(
        401,
        "invalid_api_key",
        "APIキーが無効です。",
        "Authorization: Bearer <APIキー> を付けてください。"
        "キーの発行は `python -m app.apikeys --create` です。",
        str(exc),
    )


@app.exception_handler(apikeys.RateLimitExceeded)
async def api_rate_limited(request: Request, exc: apikeys.RateLimitExceeded):
    return _error(
        429,
        "api_rate_limit",
        "このAPIキーのレート制限に達しました。少し待ってから再試行してください。",
        f"上限は {exc.limit} リクエスト/分です。",
        str(exc),
    )


@app.middleware("http")
async def record_api_usage_status(request: Request, call_next):
    """公開APIの利用ログに、応答のHTTPステータスを書き戻す。

    受付の記録は認証の時点で入れている（レート制限がその件数を数えるため）。
    ステータスだけは応答が決まるまで分からないので、ここで後から埋める。

    ★書き戻しの失敗は握りつぶす★
      利用ログは補助情報で、応答そのものは既に出来上がっている。ここで例外を
      通すと、DBの一時障害だけで本来成功していた /v1 の応答が 500 に化ける。
      課金・レート制限に効く「受付の記録」は認証時に済んでいるので、
      ステータス欄が NULL のまま残ってもその2つは壊れない。
    """
    response = await call_next(request)
    usage_id = getattr(request.state, "usage_id", None)
    if usage_id is not None:
        try:
            apikeys.set_status(usage_id, response.status_code)
        except Exception:
            logger.exception("利用ログのステータス書き戻しに失敗（応答はそのまま返す）")
    return response


def require_admin(request: Request) -> None:
    """管理用API(/admin/*)の入口。ADMIN_TOKEN を設定したときだけ認証を要求する。

    ★なぜ「設定したときだけ」なのか★
      このアプリはログインなし・Tailscaleで閉域という前提で、UI経路には認証の
      仕組みが無い。ここだけ必須にするとローカル開発で毎回トークンが要る割に、
      閉域内では守るものが増えない。一方 /admin/reindex-images は画像1枚ごとに
      Claude/Voyage を呼ぶので、閉域を出す構成では放置できない
      （コスト増幅・DoSの経路になる）。そこで「出すなら設定する」を選べる形にした。

    照合は secrets.compare_digest で行う（== だと一致する文字数で応答時間が
    変わり、総当たりの手がかりを与える）。
    """
    if not ADMIN_TOKEN:
        return
    given = request.headers.get("x-admin-token") or ""
    if not secrets.compare_digest(given, ADMIN_TOKEN):
        raise AdminForbidden()


def require_api_key(request: Request) -> apikeys.ApiKey:
    """/v1 の入口。認証・レート制限・利用ログをまとめて行う。

    ★リクエストに project を書かせない★
      テナントの境界はキー側にあるので、クエリで project を渡されたら
      黙って無視せず 400 で弾く。無視すると「絞ったつもりで全体を見ている」
      と誤解したまま使われうる（本文側は PublicChatRequest が extra="forbid"）。
    """
    if "project" in request.query_params:
        raise ProjectNotAllowed()
    key, usage_id = apikeys.authenticate(
        request.headers.get("authorization"), request.url.path
    )
    # 応答時にステータスを書き戻すため、この行IDをミドルウェアへ渡す
    request.state.usage_id = usage_id
    return key


class ProjectNotAllowed(ValueError):
    """/v1 で project を指定しようとした（テナントはキー側で決まる）。"""


@app.exception_handler(ProjectNotAllowed)
async def project_not_allowed(request: Request, exc: Exception):
    return _error(
        400,
        "project_not_allowed",
        "project は指定できません。",
        "検索対象のプロジェクトはAPIキーに紐づいています。"
        "さらに絞るときは topic を使ってください。",
        "",
    )


# 公開API。既存の /ingest・/search・/chat は据え置きで、公開する面だけを
# /v1 で包む（内部の実験用パラメータを外に出さないため、引数も絞ってある）。
v1 = APIRouter(
    prefix="/v1",
    tags=["public"],
    dependencies=[Depends(require_api_key)],  # ルータ配下は全て認証必須
    responses={
        400: {"model": ErrorResponse, "description": "入力不正"},
        401: {"model": ErrorResponse, "description": "APIキーが無効"},
        429: {"model": ErrorResponse, "description": "レート制限"},
        502: {"model": ErrorResponse, "description": "外部API呼び出し失敗"},
    },
)


@v1.get("/search", response_model=SearchResponse)
def v1_search(
    q: str,
    top_n: int = 4,
    topic: Optional[str] = None,
    key: apikeys.ApiKey = Depends(require_api_key),
):
    """このキーのプロジェクトの文書だけを検索する。

    検索手法や数値パラメータ（retrievers / rrf_k / bm25_k1 …）は公開しない。
    あれは挙動を観察するための実験用ノブで、外部に出すと結果の再現性を
    こちらで保証できなくなるため、既定の構成で固定して返す。
    """
    return search_stages(
        q, top_n=top_n, project=key.project, topic=_blank_to_none(topic)
    )


@v1.post("/chat", response_model=ChatResponse)
def v1_chat(req: PublicChatRequest, key: apikeys.ApiKey = Depends(require_api_key)):
    """このキーのプロジェクトの文書だけを根拠に回答する。

    ★project はリクエストから受け取らず、キーの値を使う★
    conversation_id は自分のキーで始めた会話しか続けられない（他は404）。
    """
    return _answer(
        ChatRequest(
            question=req.question,
            conversation_id=req.conversation_id,
            project=key.project,
            topic=req.topic,
        ),
        api_key_id=key.id,
    )


app.include_router(v1)


@app.get("/health", response_model=HealthResponse)
async def health():
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResponse, responses=_ERRORS)
def ingest(req: IngestRequest):
    # 文書名と本文は必須。Pydanticは空文字を通すので明示検査する
    # （空の source は空のS3キー・空文書を生む）。/eval-questions と同じ方針。
    if not req.source.strip() or not req.text.strip():
        return _error(
            400,
            "invalid_ingest",
            "文書名と本文は必須です。",
            "source と text の両方を入力してください。",
            "",
        )
    result = ingest_text(
        req.source,
        req.text,
        _blank_to_none(req.project),
        _blank_to_none(req.topic),
    )
    # replaced > 0 = 同名の既存文書を置き換えた（重複登録を防いでいる）
    return {"source": req.source, **result}


@app.post(
    "/ingest-file",
    response_model=IngestResponse,
    responses={
        **_ERRORS,
        400: {"model": ErrorResponse, "description": "入力不正"},
        413: {"model": ErrorResponse, "description": "ファイルが大きすぎる"},
        415: {"model": ErrorResponse, "description": "未対応のファイル形式"},
    },
)
async def ingest_file(
    file: UploadFile = File(..., description="登録する文書ファイル"),
    project: Optional[str] = Form(default=None, description="プロジェクト（任意）"),
    topic: Optional[str] = Form(default=None, description="トピック（任意）"),
):
    """ファイルをアップロードして取り込む（ドラッグ&ドロップ登録の受け口）。

    /ingest がテキスト貼り付け用なのに対し、こちらは multipart/form-data で
    ファイルそのものを受け取り、テキストを抽出してから同じ取り込み処理に流す。
    出典名(source)はアップロードされたファイル名をそのまま使う。

    検索・埋め込みには抽出テキストを使い（ingest_text）、原本バイナリ（PDF等）は
    そのまま S3 に保存する（storage.save_bytes）。抽出テキストを原本として
    保存すると原本ダウンロードが壊れるため、取り込みは store_original=False にし、
    原本の保存はここで明示的に行う。

    加えて文書内の画像も抽出して S3 に保存し、画像チャンクとして登録する（5-1）。
    画像を持つのは原本バイナリがあるこの経路だけなので、/ingest（テキスト貼り付け）
    には無い処理になる。
    """
    source = (file.filename or "").strip()
    if not source:
        return _error(
            400,
            "invalid_ingest",
            "ファイル名が空です。",
            "ファイル名を持つファイルをアップロードしてください。",
            "",
        )

    data = await file.read(UPLOAD_MAX_BYTES + 1)
    if len(data) > UPLOAD_MAX_BYTES:
        return _error(
            413,
            "file_too_large",
            "ファイルが大きすぎます。",
            f"上限は {UPLOAD_MAX_BYTES // (1024 * 1024)}MB です。",
            "",
        )
    if not data:
        return _error(
            400,
            "invalid_ingest",
            "ファイルの中身が空です。",
            "空でないファイルをアップロードしてください。",
            "",
        )

    # 拡張子ごとにテキストを抽出（未対応形式は UnsupportedFileType→415、
    # 文字コード不明は ValueError→ここで400にまとめる）。
    try:
        text = extract_text(source, data)
    except ValueError as exc:
        return _error(
            400,
            "invalid_ingest",
            "ファイルからテキストを取り出せませんでした。",
            str(exc),
            "",
        )
    if not text.strip():
        return _error(
            400,
            "invalid_ingest",
            "ファイルから本文が取り出せませんでした（中身が空です）。",
            "",
            "",
        )

    # 文書内の画像（PDFはページ画像・xlsx/pptxは貼られた図）も取り出す。
    # 抽出できなくても取り込みは続ける（extract_images は例外を投げない）。
    images = extract_images(source, data)

    result = ingest_text(
        source,
        text,
        _blank_to_none(project),
        _blank_to_none(topic),
        store_original=False,
        images=images,
    )
    # 原本バイナリを S3(MinIO) に保存し、出典名からダウンロードできるようにする。
    # 取り込み(DB登録)成立後に行う best-effort（S3が落ちていても登録は残す）。
    # content_type はアップロード時の MIME を優先（無ければ拡張子から推定）。
    storage.save_bytes(source, data, file.content_type)
    return {"source": source, **result}


@app.get("/schema", response_model=SchemaResponse)
def schema_dictionary():
    """テーブル・カラムの物理名と論理名（日本語名）の対応表。

    ★DBを見に行かない★
      正は app.schema_labels（Pythonの定数）で、DB側の COMMENT ON はそこからの
      写し。写しを読み返すとDB接続が要るうえ、init_db を通していないDBを指すと
      空で返ってしまう。定数をそのまま返せば、キーもDBも不要で常に同じ答になる。

    UIの表の見出しや、スキーマ定義書の生成に使う入口。
    """
    return {
        "tables": [
            {
                "name": table,
                "label": entry["label"],
                "columns": [
                    {"name": column, "label": label}
                    for column, label in entry["columns"].items()
                ],
            }
            for table, entry in SCHEMA_LABELS.items()
        ]
    }


@app.get("/retrievers", response_model=RetrieversResponse)
def retrievers_list():
    """選択可能な検索手法の一覧。UIのチェックボックス生成に使う。"""
    return {
        "available": retriever_infos(),
        "default": RETRIEVERS_DEFAULT,
        "fusion_params": FUSION_PARAM_SPECS,
    }


@app.get("/projects", response_model=ProjectsResponse)
def list_projects():
    """登録済みのプロジェクト名の一覧。UIの区分セレクタを埋めるのに使う。

    ★マスタ(projects)を引く★
      以前は documents と eval_questions の和集合を都度 DISTINCT していたが、
      それだと「文書も質問もまだ無いプロジェクト」が存在できなかった。
      文書・質問に付いた区分は保存時に app.scopes.register でマスタへ写るので、
      従来どおり「使われている区分」も漏れなく出る。
    """
    return {"projects": scopes.list_projects()}


@app.post(
    "/projects",
    response_model=ScopeResponse,
    responses={400: {"model": ErrorResponse, "description": "入力不正"}},
)
def add_project(req: ProjectRequest):
    """プロジェクトを作る。文書を入れる前に区分だけ用意しておくための入口。

    既に同じ名前があれば created=false を返す。これはエラーではなく
    「重ねなかった」という結果なので 200 のまま返す（/saved-questions と同じ）。
    """
    name = _blank_to_none(req.name)
    if name is None:
        return _error(
            400,
            "invalid_project",
            "プロジェクト名は必須です。",
            "name を入力してください。",
            "",
        )
    return {"created": scopes.create_project(name), "name": name, "project": None}


@app.get("/topics", response_model=TopicsResponse)
def list_topics(project: Optional[str] = None):
    """登録済みのトピック名の一覧。project を付けるとその配下だけに絞る。

    UIは「プロジェクトを選ぶ → そのトピックだけが候補になる」という順で使う。
    project 未指定なら全プロジェクトのトピックを返す（絞り込みなし）。
    """
    return {"topics": scopes.list_topics(_blank_to_none(project))}


@app.get("/documents", response_model=DocumentsResponse)
def list_documents(project: Optional[str] = None, topic: Optional[str] = None):
    """登録済みの文書名の一覧。project/topic を付けるとその区分の文書だけに絞る。

    ★何のために要るか★
      評価用の質問（eval_questions.expected_source）は「正解の文書名」を持つが、
      これは documents.source を指している前提の値。UIが手入力だと、実在しない
      名前でも登録できてしまい、その設問は何をやっても永久に不正解になる
      （検索で引けるはずの文書が無いので当然当たらない）。候補を返して
      選ばせるための入口。

      /projects や /topics と同じ「UIのセレクタを埋める」用途なので、返すのは
      名前と区分だけ。チャンク数や取り込み日時が要る一覧画面は別途。

    同じ source の行が複数あるDB（documents.source は UNIQUE ではない）でも
    候補が重複しないよう、source ごとに1件（新しい行）へ寄せる。
    """
    project = _blank_to_none(project)
    topic = _blank_to_none(topic)
    # 区分は id 参照なので名前はマスタへの LEFT JOIN で引く（LEFT なのは
    # 区分なし＝NULL の文書を落とさないため。list_eval_questions と同じ形）。
    clauses = []
    params: list = []
    if project is not None:
        clauses.append("p.name = %s")
        params.append(project)
    if topic is not None:
        clauses.append("t.name = %s")
        params.append(topic)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT ON (d.source) d.source, p.name, t.name FROM documents d "
            f"LEFT JOIN projects p ON p.id = d.project_id "
            f"LEFT JOIN topics t ON t.id = d.topic_id "
            f"{where} ORDER BY d.source, d.id DESC",
            params,
        ).fetchall()
    return {
        "documents": [{"source": r[0], "project": r[1], "topic": r[2]} for r in rows]
    }


# 一覧の既定件数と上限。文書は運用で増える一方なので「全部返す」は取らない。
# 既定500は、この画面で扱う規模（評価コーパス29件＋デモ用）に対して十分な余裕。
DOCUMENTS_LIMIT_DEFAULT = 500
DOCUMENTS_LIMIT_MAX = 2000


@app.get("/documents/summary", response_model=DocumentSummariesResponse)
def list_document_summaries(
    project: Optional[str] = None,
    topic: Optional[str] = None,
    limit: int = DOCUMENTS_LIMIT_DEFAULT,
):
    """文書一覧画面用。1行 = documents の1行で、チャンク数と登録日時を添える。

    ★/documents とは別エンドポイントにしてある★
      あちらは「④の正解文書セレクタを埋める」用途で、同じ source を
      DISTINCT ON で1件に潰す。こちらは管理用で、★同名の行が2つ見えること
      自体が価値★（documents.source は UNIQUE ではないので二重登録があり得る）。
      潰す/潰さないが正反対なので、同居させるとどちらかの用途が壊れる。

    ★ここで気づきたい壊れ方★
      - chunk_count が 0 … 登録したつもりで索引に載っていない
      - image_chunk_count が 0 … 図表が索引に載っていない（方式や抽出の失敗）
      - project/topic が NULL … 区分で絞った検索から丸ごと外れる
      - 同じ source が複数行 … 二重登録

    ★/projects /topics はそのまま使う（絞り込みセレクタ）★
      あの2つはマスタを引くので「文書が1件も無い区分」も候補に出る。それを
      除く絞り込みは足さない: 区分だけ先に作って文書は後から入れる、という
      使い方を ①「区分だけ登録する」で明示的に支えているので、候補から
      消すと「作ったはずの区分が無い」と読めてしまう。0件は下の空表示で言う。
    """
    project = _blank_to_none(project)
    topic = _blank_to_none(topic)
    # 上限は黙って効かせる（範囲外で 400 にはしない）。ここは表示用の件数で、
    # 呼び出し側が直せる種類の間違いではないため。
    limit = max(1, min(limit, DOCUMENTS_LIMIT_MAX))

    # 区分は id 参照なので名前はマスタへの LEFT JOIN で引く（LEFT なのは
    # 区分なし＝NULL の文書を落とさないため。list_documents と同じ形）。
    clauses = []
    params: list = []
    if project is not None:
        clauses.append("p.name = %s")
        params.append(project)
    if topic is not None:
        clauses.append("t.name = %s")
        params.append(topic)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # 打ち切ったかを知るために1件多く取る（別途 COUNT(*) を撃たずに済む）。
    params.append(limit + 1)

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT d.id, d.source, p.name, t.name, d.created_at, "
            # チャンクは LEFT JOIN なので0件の文書も行として残る。
            # COUNT(c.id) は NULL を数えないので、そのまま 0 になる。
            f"COUNT(c.id) AS chunk_count, "
            # COUNT(列) が数えるのは非NULLだけ＝image_path のある行＝画像チャンク。
            f"COUNT(c.image_path) AS image_chunk_count, "
            f"(d.content_hash IS NOT NULL) AS has_content_hash "
            f"FROM documents d "
            f"LEFT JOIN projects p ON p.id = d.project_id "
            f"LEFT JOIN topics t ON t.id = d.topic_id "
            f"LEFT JOIN chunks c ON c.document_id = d.id "
            f"{where} "
            # d.id は主キーなので、d.* は id にぶら下がるものとして GROUP BY に
            # 並べなくてよい（Postgresの関数従属）。p.name/t.name は別テーブル
            # なので明示が要る。
            f"GROUP BY d.id, p.name, t.name "
            # 新しい順。NULLS LAST は created_at が NULL の古い行を末尾に送るため
            # （既定の DESC では NULL が先頭に来て、一番新しく見えてしまう）。
            f"ORDER BY d.created_at DESC NULLS LAST, d.id DESC "
            f"LIMIT %s",
            params,
        ).fetchall()

    truncated = len(rows) > limit
    return {
        "documents": [
            {
                "id": r[0],
                "source": r[1],
                "project": r[2],
                "topic": r[3],
                "created_at": r[4],
                "chunk_count": r[5],
                "image_chunk_count": r[6],
                "has_content_hash": r[7],
            }
            for r in rows[:limit]
        ],
        "truncated": truncated,
    }


@app.post(
    "/topics",
    response_model=ScopeResponse,
    responses={400: {"model": ErrorResponse, "description": "入力不正"}},
)
def add_topic(req: TopicRequest):
    """トピックを作る。project を付けるとその配下のトピックになる。

    既に同じ組み合わせがあれば created=false（/projects と同じ約束）。
    """
    name = _blank_to_none(req.name)
    if name is None:
        return _error(
            400,
            "invalid_topic",
            "トピック名は必須です。",
            "name を入力してください。",
            "",
        )
    project = _blank_to_none(req.project)
    return {
        "created": scopes.create_topic(name, project),
        "name": name,
        "project": project,
    }


@app.get("/search", response_model=SearchResponse, responses=_ERRORS)
def search(
    q: str,
    top_n: int = 4,
    retrievers: Optional[str] = None,
    rrf_k: Optional[int] = None,
    trgm_min_similarity: Optional[float] = None,
    bm25_k1: Optional[float] = None,
    bm25_b: Optional[float] = None,
    project: Optional[str] = None,
    topic: Optional[str] = None,
):
    """検索の各段階を返す。

    Claude(Anthropic)は呼ばないのでANTHROPIC_API_KEYは不要。
    ただし質問のベクトル化に埋め込みAPIを使うためVOYAGE_API_KEYは必要。

    - GET /search?q=... … 設定の既定の手法で検索
    - GET /search?q=...&retrievers=vector,trgm,bm25 … 手法を明示指定して比較
    - GET /search?q=...&bm25_k1=2.0&bm25_b=0.3&rrf_k=10 … 定数を変えて挙動を比較
      （指定しなかった定数は既定値が使われる）
    - GET /search?q=...&project=社内規程&topic=労務 … その区分の文書だけを検索
      （指定しなかった軸は絞り込まない。BM25の統計も絞った範囲で計算される）

    各手法の順位・生スコアと、RRF融合後の寄与内訳(contributions)が返る。
    """
    names = (
        [n.strip() for n in retrievers.split(",") if n.strip()] if retrievers else None
    )
    # None のものは落として、その手法の既定値が使われるようにする
    raw = {
        "trgm": {"min_similarity": trgm_min_similarity},
        "bm25": {"k1": bm25_k1, "b": bm25_b},
    }
    params = {
        r: {k: v for k, v in vals.items() if v is not None} for r, vals in raw.items()
    }
    project = _blank_to_none(project)
    topic = _blank_to_none(topic)
    stages = search_stages(
        q,
        top_n=top_n,
        retrievers=names,
        params=params,
        rrf_k=rrf_k,
        project=project,
        topic=topic,
    )
    # ★検索が成功してから保管する★（④でまとめて検証するための質問集になる）。
    # 失敗した検索（キー未設定・手法名のtypo等）は保管しない: 例外で先に抜けるため
    # ここまで来ない。同じ区分の同じ質問は重ならない（saved_questions.save）。
    saved_questions.save(q, project, topic)
    return stages


@app.get("/files/{source:path}", responses=_ERRORS)
def download_file(source: str):
    """出典名(source)の原本を S3(MinIO) から取り出してダウンロードさせる。

    ローカルの MinIO は docker 内ホスト名(minio:9000)なのでブラウザから直接は
    署名URLで開けない。ここで backend が取得して中継することで、環境差なく
    ダウンロードできる（本番の実S3では署名URL方式に切り替えてもよい）。
    無ければ404。

    source は `:path` で受ける。将来 S3キーに `/`（サブディレクトリ/プレフィックス）を
    使えるようにしても、1セグメント制限でダウンロードできなくならないようにするため。
    """
    obj = storage.get_object(source)
    if obj is None:
        return _error(
            404,
            "file_not_found",
            f"原本が見つかりません: {source}",
            "この変更より前に登録した文書は原本が未保存です。"
            "再登録するか /admin/backfill-files を実行してください。",
            "",
        )
    body, content_type = obj
    # 日本語ファイル名も壊れないよう RFC 5987 の filename* でエンコード
    quoted = urllib.parse.quote(source)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@app.post("/admin/backfill-files", dependencies=[Depends(require_admin)])
def backfill_files():
    """この変更より前に登録済みの文書を、原本ダウンロードに対応させる後埋め。

    各文書の本文を chunks から復元して S3 に保存する（まだ無いものだけ）。
    ※短い文書は1チャンク＝原本そのものだが、長い文書はオーバーラップ分割のため
      復元が厳密でない。以降の登録は取り込み時に原本を保存するので、これは一度きり
      の移行用。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.source, c.content "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            # 画像チャンクは本文を持たない（content はラベル）ので原本の復元に混ぜない
            "WHERE c.image_path IS NULL "
            "ORDER BY d.source, c.chunk_index"
        ).fetchall()
    # 出典ごとにチャンク本文を順に連結して原本テキストを復元
    texts: dict[str, str] = {}
    for source, content in rows:
        texts[source] = texts.get(source, "") + content
    saved = storage.backfill_from_texts(list(texts.items()))
    return {"backfilled": saved, "documents": len(texts)}


@app.post(
    "/admin/reindex-images",
    responses=_ERRORS,
    dependencies=[Depends(require_admin)],
)
def reindex_images_endpoint(method: Optional[str] = None):
    """S3の原本画像から、画像チャンクの索引だけを作り直す（5-2の索引方式の比較評価用）。

    画像の索引方式（自動キャプション / マルチモーダル埋め込み）は取り込み時に
    決まるため、方式を変えて比べるには索引を作り直す必要がある。原本画像はS3に
    あるので、ファイルを上げ直さずここで差し替えられる。

    method 省略時は現在の設定(IMAGE_INDEX_METHOD)。手順は app.eval のドキュメント参照。

    ★429は待って再試行する★（他のWeb経路と違う扱い）。管理用のバッチ操作なので
    多少待たせてよく、待たずに失敗すると「索引の無い画像」が残って、以降の検索・
    評価が静かに壊れるため。戻り値の indexed が images と一致しているかを必ず見ること。
    """
    if method is not None and method not in IMAGE_INDEX_METHODS:
        return _error(
            400,
            "invalid_image_index_method",
            f"未知の画像索引方式: {method}",
            f"利用可能: {', '.join(IMAGE_INDEX_METHODS)}",
            "",
        )
    return {
        "method": method or IMAGE_INDEX_METHOD,
        **reindex_images(method, retry_waits=RETRY_WAITS),
    }


@app.post("/chart-read", response_model=ChartReadResponse, responses=_ERRORS)
def chart_read(req: ChartReadRequest):
    """文書内のチャート画像を読解する（5-4）。★売買判断は返さない★

    5-3（原本画像を根拠にした回答）をチャートに向けたもの。検索でヒットした
    画像チャンクだけを根拠にし、「今どういう状態か」を言葉にする。
    複数レポートの図表を集めて要約する用途もここに乗る。

    ★この機能を /v1（公開API）に載せないのは意図的★
      個別銘柄の売買判断を業として提供すると、日本では金融商品取引法の
      投資助言・代理業の登録が必要になる可能性が高い。社外へ売買判断を返す
      経路をそもそも作らないため、社内向けのこの経路だけに置く。
      出力側の検査も含め、制限の理由は app.charts の冒頭にまとめてある。

    画像が1件も引けなかったときは 404。「テキストだけで答えた説明」を
    チャート読解として返すと、利用者は図を読んだ結果だと受け取ってしまう。
    """
    if not req.question.strip():
        return _error(
            400,
            "invalid_question",
            "質問は必須です。",
            "question を入力してください。",
            "",
        )
    hits = hybrid_search(
        req.question,
        project=_blank_to_none(req.project),
        topic=_blank_to_none(req.topic),
    )
    # 根拠は画像だけに絞る。本文チャンクを混ぜると、チャートを読んだのか
    # 本文を読んだのか区別が付かない説明になる。
    image_hits = [h for h in hits if h.get("image_path")]
    contexts = _answer_contexts(image_hits)
    attached = [c for c in contexts if isinstance(c, ImageContext)]
    if not attached:
        return _error(
            404,
            "no_chart_found",
            "対象になる図表が見つかりませんでした。",
            "図表を含む文書を /ingest-file で登録し、"
            "IMAGE_INDEX_METHOD で索引を作ってから試してください。",
            "",
        )

    result = charts.read_charts(req.question, contexts)
    return {
        "reading": result["reading"],
        "charts_read": len(attached),
        "citations": _citations(image_hits),
        "removed": result["removed"],
        "removed_labels": result["labels"],
    }


CITATION_PREVIEW_CHARS = 200  # 引用に載せる該当箇所の長さ（検索の内訳より少し長め）


def _citations(hits: list[dict]) -> list[dict]:
    """検索でヒットしたチャンクを、回答の引用 [n] に対応させた形に整える。

    ★番号は hits の並びそのもの★（1始まり）。同じ並びを generate_answer にも
    渡しているので、回答本文の [n] とここの n が必ず一致する。
    原本URLは出典ごとに1回だけ引く（S3のhead_objectを同じ文書で何度も叩かない）。

    画像チャンクには image_url も付ける。回答生成に渡したのと同じ1枚を利用者にも
    見せるため ＝ 「この図のここが根拠」を自分の目で確かめられる（5-3）。
    """
    urls: dict[str, str | None] = {}
    citations = []
    for n, hit in enumerate(hits, start=1):
        source = hit["source"]
        if source not in urls:
            urls[source] = storage.file_url(source)
        image_path = hit.get("image_path")
        citations.append(
            {
                "n": n,
                "chunk_id": hit["id"],
                "source": source,
                "preview": preview(hit["content"], CITATION_PREVIEW_CHARS),
                "file_url": urls[source],
                # 画像チャンクなら「その画像」へのURL。文書の原本(file_url)とは別物で、
                # 何ページ目の図が根拠だったかはこちらでしか分からない。
                "image_url": storage.file_url(image_path) if image_path else None,
                "image_label": hit.get("context") if image_path else None,
            }
        )
    return citations


def _image_context(hit: dict) -> Optional[ImageContext]:
    """画像チャンクなら原本画像を S3 から取り出す。使えなければ None。

    None を返した場合、呼び出し側は言語化テキスト（キャプション等）で代替する。
    ★画像が取れないことを理由に回答を失敗させない★のが方針で、S3障害でも
    5-2 までの品質（言語化テキストで答える）には落ちるだけで済ませる。

    大きすぎる画像を弾くのは、Claude の画像1枚の上限(5MB)を超えると
    リクエストごと失敗し、回答が1文字も返らなくなるため。
    """
    image_path = hit.get("image_path")
    if not image_path:
        return None
    obj = storage.get_object(image_path)
    if obj is None:
        logger.warning("原本画像を取得できませんでした（言語化テキストで代替）: %s", image_path)
        return None
    data, media_type = obj
    if media_type not in ANSWER_IMAGE_MEDIA_TYPES:
        logger.warning(
            "回答生成に渡せない画像形式です（言語化テキストで代替）: %s (%s)",
            image_path,
            media_type,
        )
        return None
    if len(data) > ANSWER_IMAGE_MAX_BYTES:
        logger.warning(
            "画像が大きすぎるため添付しません（言語化テキストで代替）: %s (%d bytes)",
            image_path,
            len(data),
        )
        return None
    # label は「3ページ目」等の由来。中身の説明ではない（中身は画像から読ませる）
    return ImageContext(
        data=data, media_type=media_type, label=hit.get("context") or hit["source"]
    )


def _answer_contexts(hits: list[dict]) -> list:
    """回答生成に渡すコンテキストを組み立てる。並びは hits と1対1（引用番号の根拠）。

    ★画像チャンクは言語化テキストではなく原本画像を渡す（5-3）★
      キャプションは検索で見つけるための索引に格下げし、判断は毎回原本に
      対して行わせる。こうしないと「言語化した時点で書かれなかったこと」を
      後から問えない（それが2023年方式の弱点だった）。

    添付する枚数に上限を置くのは入力トークンとコストの保護。上限を超えた分は
    従来どおり言語化テキストで渡す（順位が上のものから優先して画像にする）。
    """
    contexts: list = []
    attached = 0
    for hit in hits:
        image = _image_context(hit) if attached < ANSWER_MAX_IMAGES else None
        if image is None:
            contexts.append(hit["content"])
        else:
            contexts.append(image)
            attached += 1
    return contexts


def _prepare_answer(req: ChatRequest, api_key_id: Optional[int] = None) -> dict:
    """回答生成の手前まで（会話の確定・検索・引用の組み立て・履歴の読み出し）。

    /chat と /chat/stream で共通。ここまでは生成APIを呼ばないので、キー未設定や
    検索エラーは通常の例外ハンドラで拾える（＝ストリームを開く前に失敗できる）。

    ★履歴を読むのは今回の質問を保存する前★。自分の質問が履歴に混ざらないようにする。

    project / topic を指定すると、その区分の文書だけを根拠にして答える。
    api_key_id: 公開API(/v1)から来た場合の発行キー。会話の持ち主として記録し、
      続きの質問も同じキーのものだけを許す（他テナントの履歴を読ませない）。
    """
    conversation_id = conversations.resolve(
        req.conversation_id, title=req.question, api_key_id=api_key_id
    )
    history = conversations.load_history(conversation_id)
    hits = hybrid_search(
        req.question,
        project=_blank_to_none(req.project),
        topic=_blank_to_none(req.topic),
    )
    conversations.add_message(conversation_id, conversations.USER, req.question)
    return {
        "conversation_id": conversation_id,
        "history": history,
        # 画像チャンクは原本画像そのものが入る（テキストと混在する。5-3）
        "contexts": _answer_contexts(hits),
        # 根拠として使ったチャンクの出典も返す（重複排除）
        "sources": list(dict.fromkeys(h["source"] for h in hits)),
        "citations": _citations(hits),
    }


@app.post("/chat", response_model=ChatResponse, responses=_ERRORS)
def chat(req: ChatRequest):
    """検索した上位チャンクを根拠に回答する（生成が終わってから一括で返す）。

    回答本文には [1] [2] の引用マーカーが入り、citations[n-1] がその根拠チャンク
    （id・出典・該当箇所・原本URL）になる。出典名だけを返していた頃と違い、
    利用者が「回答のこの主張はこの条文が根拠」と自分で検証できる。

    conversation_id を渡すと直近の履歴を踏まえて答える（未指定なら新しい会話）。
    逐次表示したい場合は /chat/stream を使う。
    """
    return _answer(req)


def _answer(req: ChatRequest, api_key_id: Optional[int] = None):
    """検索 → 生成 → 履歴保存。/chat と /v1/chat で共通の本体。

    違いは「誰の会話か（api_key_id）」と「どの区分を見るか（req.project）」だけで、
    検索も生成もまったく同じものを通る（公開APIのためにコアを分岐させない）。
    """
    if (invalid := _reject_empty_question(req)) is not None:
        return invalid
    prepared = _prepare_answer(req, api_key_id=api_key_id)
    answer = generate_answer(req.question, prepared["contexts"], prepared["history"])
    conversations.add_message(
        prepared["conversation_id"],
        conversations.ASSISTANT,
        answer,
        prepared["sources"],
    )
    return {
        "answer": answer,
        "conversation_id": prepared["conversation_id"],
        "sources": prepared["sources"],
        "citations": prepared["citations"],
    }


def _sse(event: str, payload: dict) -> str:
    """Server-Sent Events の1イベント。data は1行のJSONにする（改行が区切りのため）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/chat/stream", responses=_ERRORS)
def chat_stream(req: ChatRequest):
    """/chat のストリーミング版。Server-Sent Events で回答を逐次返す。

    イベントの順序と意味:
      meta  … 会話ID・出典・引用（★生成より先に確定する★ので最初に送る。
              受け取り側は本文が届く前に根拠を出せる）
      delta … 回答本文の断片。届いた順に連結すると完成した回答になる
      done  … 生成完了（ここで初めて履歴に回答を保存する）
      error … 生成中の失敗。HTTPステータスは200のまま流れているので、
              エラーは本文の中で伝えるしかない（形は通常のエラー応答と同じ）

    ★検索と会話の解決はストリームを開く前に済ませる★
      そこで失敗したら通常のエラー応答（4xx/5xx）を返せる。ストリームを開いた後は
      ステータスを変えられないため、開く前に失敗できる工程は全部先に終わらせる。
    """
    if (invalid := _reject_empty_question(req)) is not None:
        return invalid
    prepared = _prepare_answer(req)

    def events():
        yield _sse(
            "meta",
            {
                "conversation_id": prepared["conversation_id"],
                "sources": prepared["sources"],
                "citations": prepared["citations"],
            },
        )
        chunks: list[str] = []
        try:
            for text in stream_answer(
                req.question, prepared["contexts"], prepared["history"]
            ):
                chunks.append(text)
                yield _sse("delta", {"text": text})
        except Exception as exc:
            logger.exception("ストリーミング生成に失敗")
            yield _sse("error", _error_payload(*_stream_error(exc)))
            return
        answer = "".join(chunks)
        conversations.add_message(
            prepared["conversation_id"],
            conversations.ASSISTANT,
            answer,
            prepared["sources"],
        )
        yield _sse("done", {"conversation_id": prepared["conversation_id"]})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # 中継（Nginx等）にバッファされると逐次表示にならないので明示的に切る
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post(
    "/feedback",
    response_model=FeedbackResponse,
    responses={400: {"model": ErrorResponse, "description": "入力不正"}},
)
def feedback(req: FeedbackRequest):
    """回答への 👍/👎 を記録する。

    貯めたフィードバック（特に👎）は eval のQA候補に回す運用を想定。
    外部APIを呼ばないので ANTHROPIC/VOYAGE キーは不要。
    rating は +1(👍) / -1(👎) のみ。0 や欠損は「どちらでもない」を意味してしまい
    👎として誤記録されるため、符号で丸めず 400 で弾く。
    """
    if req.rating not in (1, -1):
        return _error(
            400,
            "invalid_rating",
            "rating は +1（👍）か -1（👎）のいずれかにしてください。",
            f"受け取った値: {req.rating}",
            "",
        )
    rating = req.rating
    with get_conn() as conn:
        new_id = conn.execute(
            "INSERT INTO feedback (question, answer, sources, rating, comment) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (req.question, req.answer, req.sources, rating, req.comment),
        ).fetchone()[0]
    return {"id": new_id, "rating": rating}


@app.post(
    "/eval-questions",
    response_model=EvalQuestion,
    responses={400: {"model": ErrorResponse, "description": "入力不正"}},
)
def add_eval_question(req: EvalQuestionRequest):
    """評価用の質問を1件登録する（プロジェクト・トピックごとに分けられる）。

    正解ラベル(expected_source)付きでDBに貯め、`python -m app.eval` がここから
    読んで Hit@k / MRR を測る。コードの定数を編集せずに質問を足せるようにするため
    のエンドポイント。外部APIは呼ばないのでキーは不要。

    質問と正解の文書名は評価の必須要素なので、空文字なら400で弾く（Pydanticは
    空文字を str として通してしまうため、ここで明示的に検査する）。
    """
    if not req.question.strip() or not req.expected_source.strip():
        return _error(
            400,
            "invalid_eval_question",
            "質問と正解の文書名は必須です。",
            "question と expected_source の両方を入力してください。",
            "",
        )
    if req.expected_kind not in EXPECTED_KINDS:
        return _error(
            400,
            "invalid_eval_question",
            f"未知の正解種別: {req.expected_kind}",
            f"利用可能: {', '.join(EXPECTED_KINDS)}",
            "",
        )
    project = _blank_to_none(req.project)
    topic = _blank_to_none(req.topic)
    # 空欄は NULL に倒す（＝文書単位で判定）。空文字のまま入れるとどのチャンクにも
    # 含まれる空文字で判定することになり、全問正解になってしまう。
    expected_text = _blank_to_none(req.expected_text)
    # 区分をマスタへ写して id を得る（文書がまだ無いプロジェクトでも選択肢に出るように）
    project_id, topic_id = scopes.register(project, topic)
    with get_conn() as conn:
        new_id = conn.execute(
            "INSERT INTO eval_questions "
            "(project_id, topic_id, question, expected_source, expected_kind, "
            "expected_text, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                project_id,
                topic_id,
                req.question,
                req.expected_source,
                req.expected_kind,
                expected_text,
                req.note,
            ),
        ).fetchone()[0]
    return {
        "id": new_id,
        "question": req.question,
        "expected_source": req.expected_source,
        "expected_kind": req.expected_kind,
        "expected_text": expected_text,
        "project": project,
        "topic": topic,
        "note": req.note,
    }


@app.post(
    "/saved-questions",
    response_model=SavedQuestionResponse,
    responses={400: {"model": ErrorResponse, "description": "入力不正"}},
)
def add_saved_question(req: SavedQuestionRequest):
    """質問を保管する（正解ラベル不要）。②の検索時は自動で保管される。

    既に同じ区分に同じ質問があれば saved=false を返す。これはエラーではなく
    「重ねなかった」という結果なので 200 のまま返す。
    """
    if not req.question.strip():
        return _error(
            400,
            "invalid_saved_question",
            "質問は必須です。",
            "question を入力してください。",
            "",
        )
    project = _blank_to_none(req.project)
    topic = _blank_to_none(req.topic)
    saved = saved_questions.save(req.question, project, topic)
    return {
        "saved": saved,
        "question": req.question.strip(),
        "project": project,
        "topic": topic,
    }


@app.get("/saved-questions", response_model=SavedQuestionsResponse)
def list_saved_questions(project: Optional[str] = None, topic: Optional[str] = None):
    """保管済みの質問を返す。project/topic で絞り込める（未指定の軸は絞らない）。

    UIは検証を走らせる前に「この区分に何件あるか」を出すのに使う
    （/verify は質問数だけ検索するので、先に件数が見えた方が押しやすい）。
    """
    return {
        "questions": saved_questions.load(
            _blank_to_none(project), _blank_to_none(topic)
        )
    }


@app.get("/verify", response_model=VerifyReport, responses=_ERRORS)
def verify_saved_questions(
    project: Optional[str] = None,
    topic: Optional[str] = None,
    top_k: int = 4,
):
    """保管済みの質問すべてを検索し、各質問の上位k件（RRF）をまとめて返す。

    ★質問の絞り込みと検索スコープの両方に同じ project/topic が効く★
    「その区分の質問を、その区分の文書に対して引く」を揃えるため。

    正解ラベルを持たないので採点はしない（○×やHit@kは出ない）。
    「今の設定でこの質問集を引くと何が上位に来るか」を並べて見るための機能で、
    数値で良し悪しを判定したいときは正解ラベル付きの /eval を使う。
    """
    return saved_questions.verify(
        project=_blank_to_none(project),
        topic=_blank_to_none(topic),
        top_k=top_k,
    )


@app.get("/eval-questions", response_model=EvalQuestionsResponse)
def list_eval_questions(project: Optional[str] = None, topic: Optional[str] = None):
    """登録済みの評価用質問を返す。project/topic で絞り込める。

    指定しなかった軸は絞り込まない（project だけ指定ならトピックは問わず全部返す）。
    """
    project = _blank_to_none(project)
    topic = _blank_to_none(topic)
    # 行が持つのは id 参照。名前はマスタへの LEFT JOIN で引く（app.eval.load_questions
    # と同じ形。LEFT なのは区分なし＝NULL の質問を落とさないため）。
    clauses = []
    params: list = []
    if project is not None:
        clauses.append("p.name = %s")
        params.append(project)
    if topic is not None:
        clauses.append("t.name = %s")
        params.append(topic)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT q.id, q.question, q.expected_source, p.name, t.name, q.note, "
            f"q.expected_kind, q.expected_text FROM eval_questions q "
            f"LEFT JOIN projects p ON p.id = q.project_id "
            f"LEFT JOIN topics t ON t.id = q.topic_id "
            f"{where} ORDER BY q.id",
            params,
        ).fetchall()
    return {
        "questions": [
            {
                "id": r[0],
                "question": r[1],
                "expected_source": r[2],
                "expected_kind": r[6],
                "expected_text": r[7],
                "project": r[3],
                "topic": r[4],
                "note": r[5],
            }
            for r in rows
        ]
    }


@app.get("/eval", response_model=EvalReport, responses=_ERRORS)
def run_eval(
    top_k: int = 4,
    retrievers: Optional[str] = None,
    rerank: Optional[bool] = None,
    rerank_method: Optional[str] = None,
    project: Optional[str] = None,
    topic: Optional[str] = None,
    rrf_k: Optional[int] = None,
    trgm_min_similarity: Optional[float] = None,
    bm25_k1: Optional[float] = None,
    bm25_b: Optional[float] = None,
):
    """DBの評価用質問集で検索精度(Hit@k / MRR)を測って返す。

    検索の内訳(/search)が「1問を深く見る」のに対し、こちらは「質問集全体で
    どれだけ当たるか」を集計する。project/topic で評価対象を絞れる（未指定=全件）。

    検索の数値パラメータ(rrf_k / trgm_min_similarity / bm25_k1 / bm25_b)は /search と
    同じ意味で、指定するとその値で評価する（例: k1を上げてHit@kが上がるか測る）。
    未指定なら設定の既定値。

    リランクは rerank=True のときだけ走る。方式は rerank_method で切り替える
    （voyage=専用リランクAPI / llm=プロンプト式。未指定は設定の既定）。
    Claudeを呼ぶのは rerank=True かつ方式が llm のときだけ。
    質問が0件なら n=0 の空レポートを返す（UI側で「まず質問を登録」と促す）。
    contexts など内部フィールドは response_model(EvalReport)で自動的に落ちる。
    """
    names = (
        [n.strip() for n in retrievers.split(",") if n.strip()] if retrievers else None
    )
    # None の値は落とす。さらに中身が空になった手法も落とし、全体が空なら None。
    # こうしないと report.params が {"trgm": {}, "bm25": {}} になり、「既定で評価」なのか
    # 「指定して評価」なのか区別がつかなくなる（スキーマ上 null=既定 の意味付け）。
    raw = {
        "trgm": {"min_similarity": trgm_min_similarity},
        "bm25": {"k1": bm25_k1, "b": bm25_b},
    }
    params: dict[str, dict] = {}
    for r, vals in raw.items():
        cleaned = {k: v for k, v in vals.items() if v is not None}
        if cleaned:
            params[r] = cleaned
    gold = load_questions(project=_blank_to_none(project), topic=_blank_to_none(topic))
    return evaluate(
        top_k=top_k,
        retrievers=names,
        rerank=rerank,
        gold=gold,
        params=params or None,  # 空dictは「指定なし＝既定」として None に倒す
        rrf_k=rrf_k,
        rerank_method=_blank_to_none(rerank_method),
    )
