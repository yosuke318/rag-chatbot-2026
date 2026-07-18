"""取り込み: テキスト → チャンク分割 → 埋め込み → pgvector へ保存。

最小版は「プレーンテキストを受け取る」ところから。
PDF/docx 解析・contextual retrieval・差分検知は設計書の次段（TODO）。
"""
from __future__ import annotations

from app.config import CHUNK_OVERLAP, CHUNK_SIZE
from app.db import get_conn
from app.llm import embed_texts


def chunk_text(text: str) -> list[str]:
    """文字数ベースの素朴なオーバーラップ分割。"""
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def ingest_text(source: str, text: str, category: str | None = None) -> int:
    """1つの文書を取り込み、作成したチャンク数を返す。"""
    chunks = chunk_text(text)
    if not chunks:
        return 0

    embeddings = embed_texts(chunks, input_type="document")

    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO documents (source, category) VALUES (%s, %s) RETURNING id",
            (source, category),
        ).fetchone()
        document_id = row[0]

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (document_id, i, chunk, embeddings[i])
                    for i, chunk in enumerate(chunks)
                ],
            )
    return len(chunks)
