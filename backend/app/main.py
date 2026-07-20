"""FastAPI エントリポイント。最小RAGループ: /ingest で入れて /chat で聞く。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

import anthropic
import voyageai.error
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.db import init_db
from app.ingest import ingest_text
from app.llm import MissingAPIKey, generate_answer
from app.config import RETRIEVERS_DEFAULT
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
    result = ingest_text(req.source, req.text, req.category)
    # replaced > 0 = 同名の既存文書を置き換えた（重複登録を防いでいる）
    return {"source": req.source, **result}


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


@app.post("/chat", response_model=ChatResponse, responses=_ERRORS)
def chat(req: ChatRequest):
    hits = hybrid_search(req.question)
    answer = generate_answer(req.question, [h["content"] for h in hits])
    # 根拠として使ったチャンクの出典も返す（重複排除）
    sources = list(dict.fromkeys(h["source"] for h in hits))
    return {"answer": answer, "sources": sources}
