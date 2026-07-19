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


def ingest_text(source: str, text: str, category: str | None = None) -> dict:
    """1つの文書を取り込む（upsert）。{"chunks_created", "replaced"} を返す。

    同じ source の文書が既にあれば削除してから入れ直す。
    （紐づく chunks は ON DELETE CASCADE で一緒に消える）
    再取り込みで同名文書が二重に積み上がり、検索結果が重複するのを防ぐ。

    ※本来は設計書どおり content_hash で差分検知し、内容が変わっていなければ
      埋め込みAPIの呼び出し自体を省くべき。ここでは常に入れ直す簡易版。
    """
    chunks = chunk_text(text)
    if not chunks:
        return {"chunks_created": 0, "replaced": 0}

    embeddings = embed_texts(chunks, input_type="document")

    with get_conn() as conn:
        # 削除と再登録は一括で（途中で失敗しても文書が消えたままにならない）
        with conn.transaction():
            replaced = conn.execute(
                "DELETE FROM documents WHERE source = %s", (source,)
            ).rowcount

            document_id = conn.execute(
                "INSERT INTO documents (source, category) VALUES (%s, %s) RETURNING id",
                (source, category),
            ).fetchone()[0]

            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
                    "VALUES (%s, %s, %s, %s)",
                    [
                        (document_id, i, chunk, embeddings[i])
                        for i, chunk in enumerate(chunks)
                    ],
                )

    return {"chunks_created": len(chunks), "replaced": replaced}
