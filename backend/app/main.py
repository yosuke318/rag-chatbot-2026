"""FastAPI エントリポイント。最小RAGループ: /ingest で入れて /chat で聞く。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from app.db import init_db
from app.ingest import ingest_text
from app.llm import generate_answer
from app.retrieval import hybrid_search, search_stages


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 起動時にスキーマを用意
    yield


app = FastAPI(title="RAG Chatbot v2", lifespan=lifespan)


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
