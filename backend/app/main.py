"""FastAPI エントリポイント。最小RAGループ: /ingest で入れて /chat で聞く。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

import anthropic
import voyageai.error
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import init_db
from app.ingest import ingest_text
from app.llm import MissingAPIKey, generate_answer
from app.retrieval import hybrid_search, search_stages


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
        "検索だけなら /search（Claude不要）が使えます。"
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
        "検索だけなら /search（Claude不要）が使えます。",
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


class IngestRequest(BaseModel):
    source: str            # 文書名（例: "就業規則.txt"）
    text: str              # 本文
    category: Optional[str] = None


class ChatRequest(BaseModel):
    question: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/ingest")
def ingest(req: IngestRequest):
    result = ingest_text(req.source, req.text, req.category)
    # replaced > 0 = 同名の既存文書を置き換えた（重複登録を防いでいる）
    return {"source": req.source, **result}


@app.get("/search")
def search(q: str, top_n: int = 4):
    """検索の各段階を返す（Claudeを呼ばない = Anthropicキー不要）。

    例: GET /search?q=有給は入社何ヶ月で何日？
    ベクトル/字面それぞれの順位と、RRF融合後のスコアが見える。
    """
    return search_stages(q, top_n=top_n)


@app.post("/chat")
def chat(req: ChatRequest):
    hits = hybrid_search(req.question)
    answer = generate_answer(req.question, [h["content"] for h in hits])
    # 根拠として使ったチャンクの出典も返す（重複排除）
    sources = list(dict.fromkeys(h["source"] for h in hits))
    return {"answer": answer, "sources": sources}
