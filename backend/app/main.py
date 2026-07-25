"""FastAPI エントリポイント。最小RAGループ: /ingest で入れて /chat で聞く。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

import urllib.parse

import anthropic
import voyageai.error
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from app.db import get_conn, init_db
from app.eval import evaluate, load_questions
from app.ingest import UnsupportedFileType, extract_text, ingest_text
from app import storage
from app.llm import MissingAPIKey, generate_answer
from app.config import RETRIEVERS_DEFAULT, UPLOAD_MAX_BYTES
from app.retrieval import (
    UnknownRetriever,
    hybrid_search,
    FUSION_PARAM_SPECS,
    retriever_infos,
    search_stages,
)
from app.schemas import (
    ChatRequest,
    ChatResponse,
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
    RetrieversResponse,
    SearchResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 起動時にスキーマを用意
    yield


app = FastAPI(title="RAG Inspector API", lifespan=lifespan)

logger = logging.getLogger(__name__)


def _error(status: int, code: str, message: str, hint: str = "", detail: str = ""):
    """UIがそのまま表示できる形のエラー応答。"""
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message, "hint": hint, "detail": detail},
    )


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


@app.exception_handler(UnknownRetriever)
async def unknown_retriever(request: Request, exc: Exception):
    return _error(
        400,
        "unknown_retriever",
        "指定された検索手法が不正です。",
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
    result = ingest_text(req.source, req.text, req.category)
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
    category: Optional[str] = Form(default=None, description="分類（任意）"),
):
    """ファイルをアップロードして取り込む（ドラッグ&ドロップ登録の受け口）。

    /ingest がテキスト貼り付け用なのに対し、こちらは multipart/form-data で
    ファイルそのものを受け取り、テキストを抽出してから同じ取り込み処理に流す。
    出典名(source)はアップロードされたファイル名をそのまま使う。

    検索・埋め込みには抽出テキストを使い（ingest_text）、原本バイナリ（PDF等）は
    そのまま S3 に保存する（storage.save_bytes）。抽出テキストを原本として
    保存すると原本ダウンロードが壊れるため、取り込みは store_original=False にし、
    原本の保存はここで明示的に行う。
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

    result = ingest_text(source, text, category, store_original=False)
    # 原本バイナリを S3(MinIO) に保存し、出典名からダウンロードできるようにする。
    # 取り込み(DB登録)成立後に行う best-effort（S3が落ちていても登録は残す）。
    # content_type はアップロード時の MIME を優先（無ければ拡張子から推定）。
    storage.save_bytes(source, data, file.content_type)
    return {"source": source, **result}


@app.get("/retrievers", response_model=RetrieversResponse)
def retrievers_list():
    """選択可能な検索手法の一覧。UIのチェックボックス生成に使う。"""
    return {
        "available": retriever_infos(),
        "default": RETRIEVERS_DEFAULT,
        "fusion_params": FUSION_PARAM_SPECS,
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
):
    """検索の各段階を返す。

    Claude(Anthropic)は呼ばないのでANTHROPIC_API_KEYは不要。
    ただし質問のベクトル化に埋め込みAPIを使うためVOYAGE_API_KEYは必要。

    - GET /search?q=... … 設定の既定の手法で検索
    - GET /search?q=...&retrievers=vector,trgm,bm25 … 手法を明示指定して比較
    - GET /search?q=...&bm25_k1=2.0&bm25_b=0.3&rrf_k=10 … 定数を変えて挙動を比較
      （指定しなかった定数は既定値が使われる）

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
    return search_stages(
        q, top_n=top_n, retrievers=names, params=params, rrf_k=rrf_k
    )


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


@app.post("/admin/backfill-files")
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
            "ORDER BY d.source, c.chunk_index"
        ).fetchall()
    # 出典ごとにチャンク本文を順に連結して原本テキストを復元
    texts: dict[str, str] = {}
    for source, content in rows:
        texts[source] = texts.get(source, "") + content
    saved = storage.backfill_from_texts(list(texts.items()))
    return {"backfilled": saved, "documents": len(texts)}


@app.post("/chat", response_model=ChatResponse, responses=_ERRORS)
def chat(req: ChatRequest):
    # 質問は必須。空だと無意味な検索とLLM呼び出しになるので手前で弾く。
    if not req.question.strip():
        return _error(
            400,
            "invalid_question",
            "質問は必須です。",
            "question を入力してください。",
            "",
        )
    hits = hybrid_search(req.question)
    answer = generate_answer(req.question, [h["content"] for h in hits])
    # 根拠として使ったチャンクの出典も返す（重複排除）
    sources = list(dict.fromkeys(h["source"] for h in hits))
    return {"answer": answer, "sources": sources}


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
    """評価用の質問を1件登録する（会社・部署ごとに分けられる）。

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
    with get_conn() as conn:
        new_id = conn.execute(
            "INSERT INTO eval_questions "
            "(company, department, question, expected_source, note) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (req.company, req.department, req.question, req.expected_source, req.note),
        ).fetchone()[0]
    return {
        "id": new_id,
        "question": req.question,
        "expected_source": req.expected_source,
        "company": req.company,
        "department": req.department,
        "note": req.note,
    }


@app.get("/eval-questions", response_model=EvalQuestionsResponse)
def list_eval_questions(
    company: Optional[str] = None, department: Optional[str] = None
):
    """登録済みの評価用質問を返す。company/department で絞り込める。

    指定しなかった軸は絞り込まない（company だけ指定なら部署は問わず全部返す）。
    """
    clauses = []
    params: list = []
    if company is not None:
        clauses.append("company = %s")
        params.append(company)
    if department is not None:
        clauses.append("department = %s")
        params.append(department)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, question, expected_source, company, department, note "
            f"FROM eval_questions {where} ORDER BY id",
            params,
        ).fetchall()
    return {
        "questions": [
            {
                "id": r[0],
                "question": r[1],
                "expected_source": r[2],
                "company": r[3],
                "department": r[4],
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
    company: Optional[str] = None,
    department: Optional[str] = None,
    rrf_k: Optional[int] = None,
    trgm_min_similarity: Optional[float] = None,
    bm25_k1: Optional[float] = None,
    bm25_b: Optional[float] = None,
):
    """DBの評価用質問集で検索精度(Hit@k / MRR)を測って返す。

    検索の内訳(/search)が「1問を深く見る」のに対し、こちらは「質問集全体で
    どれだけ当たるか」を集計する。company/department で評価対象を絞れる。

    検索の数値パラメータ(rrf_k / trgm_min_similarity / bm25_k1 / bm25_b)は /search と
    同じ意味で、指定するとその値で評価する（例: k1を上げてHit@kが上がるか測る）。
    未指定なら設定の既定値。

    Claudeは rerank=True のときだけ呼ぶ（検索評価そのものは Voyage のみ）。
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
    gold = load_questions(company=company, department=department)
    return evaluate(
        top_k=top_k,
        retrievers=names,
        rerank=rerank,
        gold=gold,
        params=params or None,  # 空dictは「指定なし＝既定」として None に倒す
        rrf_k=rrf_k,
    )
