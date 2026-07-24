"""取り込み: テキスト → チャンク分割 → 埋め込み → pgvector へ保存。

最小版は「プレーンテキストを受け取る」ところから。
PDF/docx 解析・contextual retrieval・差分検知は設計書の次段（TODO）。
"""
from __future__ import annotations

import os

from app.config import CHUNK_OVERLAP, CHUNK_SIZE
from app.db import get_conn
from app.keywords import noun_text
from app.llm import embed_texts
from app import storage


class UnsupportedFileType(Exception):
    """まだテキスト抽出に対応していない拡張子。呼び出し側は415で返す。"""

    def __init__(self, ext: str):
        self.ext = ext
        super().__init__(ext)


# 今はテキスト系のみ対応。PDF/XLSX/PPTX のパーサは次段(#3)で足す。
# 拡張子なしは「プレーンテキスト」とみなす。
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ""}


def extract_text(filename: str, data: bytes) -> str:
    """アップロードされたファイルのバイト列から本文テキストを取り出す。

    現状はテキスト系ファイルのデコードのみ。UTF-8 を第一に、日本語のレガシー
    ファイル向けに cp932 を代替として試す。どちらでも読めなければ ValueError。
    PDF/XLSX/PPTX などは UnsupportedFileType を投げ、次段のパーサ層(#3)で対応する。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in TEXT_EXTENSIONS:
        raise UnsupportedFileType(ext)
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("テキストとして読み取れませんでした（文字コード不明）")


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


def ingest_text(
    source: str,
    text: str,
    category: str | None = None,
    store_original: bool = True,
) -> dict:
    """1つの文書を取り込む（upsert）。{"chunks_created", "replaced"} を返す。

    同じ source の文書が既にあれば削除してから入れ直す。
    （紐づく chunks は ON DELETE CASCADE で一緒に消える）
    再取り込みで同名文書が二重に積み上がり、検索結果が重複するのを防ぐ。

    store_original: 原本テキストを S3 に保存するか。テキスト貼り付け登録では
      本文＝原本なので True。ファイルアップロード(/ingest-file)では原本は
      元のバイナリ（PDF等）であって抽出テキストではないため False を渡し、
      原本バイナリの保存は呼び出し側(#4 の save_bytes)に任せる。

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
                    "INSERT INTO chunks "
                    "(document_id, chunk_index, content, content_nouns, embedding) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [
                        # content_nouns: 字面検索用に名詞だけ抜き出したもの
                        (document_id, i, chunk, noun_text(chunk), embeddings[i])
                        for i, chunk in enumerate(chunks)
                    ],
                )

    # 原本を S3(MinIO) にも保存し、出典名からダウンロードできるようにする。
    # DBコミットの後に行う（S3が落ちていても取り込み自体は成立させる。best-effort）。
    if store_original:
        storage.save_text(source, text)

    return {"chunks_created": len(chunks), "replaced": replaced}
