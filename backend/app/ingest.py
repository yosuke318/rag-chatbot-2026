"""取り込み: テキスト → チャンク分割 → 文脈付与 → 埋め込み → pgvector へ保存。

分割は app.chunking（見出し・条文の構造で切る）、
文脈付与は app.llm.generate_chunk_contexts（contextual retrieval）に任せ、
ここは「その2つを繋いでDBに入れる」役に徹する。
再取り込みは content_hash で差分検知し、内容が変わっていなければ
埋め込み・文脈生成のAPI呼び出しごと省く（content_hash 関数を参照）。
"""
from __future__ import annotations

import hashlib
import os

from app import parsers, storage
from app.chunking import Chunk, split_chunks
from app.config import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    CHUNK_OVERLAP,
    CHUNKING_VERSION,
    EMBED_MODEL,
    USE_CONTEXTUAL_CHUNKING,
)
from app.db import get_conn
from app.keywords import noun_text
from app.llm import embed_texts, generate_chunk_contexts


class UnsupportedFileType(Exception):
    """まだテキスト抽出に対応していない拡張子。呼び出し側は415で返す。"""

    def __init__(self, ext: str):
        self.ext = ext
        super().__init__(ext)


# テキスト系はここでデコード、バイナリ文書(PDF/XLSX/PPTX)は parsers 側で抽出。
# 拡張子なしは「プレーンテキスト」とみなす。
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ""}


def _decode_text(data: bytes) -> str:
    """テキストファイルのバイト列を文字列にする。

    UTF-8 を第一に、日本語のレガシーファイル向けに cp932 を代替として試す。
    """
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("テキストとして読み取れませんでした（文字コード不明）")


def extract_text(filename: str, data: bytes) -> str:
    """アップロードされたファイルのバイト列から本文テキストを取り出す。

    - テキスト系(.txt/.md/.csv 等): デコードする
    - PDF/XLSX/PPTX: parsers のパーサで抽出する
    - それ以外: UnsupportedFileType（呼び出し側で415）

    解析はできたが本文が空（例: スキャンPDF）の場合は空文字を返し、
    「本文が取り出せなかった」判断は呼び出し側に委ねる。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in TEXT_EXTENSIONS:
        return _decode_text(data)
    parser = parsers.PARSERS.get(ext)
    if parser is None:
        raise UnsupportedFileType(ext)
    return parser(data)


def build_contexts(
    text: str, chunks: list[Chunk], contextual: bool | None = None
) -> list[str]:
    """各チャンクに前置する文脈を決める。チャンクと同じ長さのリストを返す。

    contextual=True なら Claude に文書全体を読ませて位置づけを書かせ、
    False なら見出しの階層（「第2章 休暇 > 第5条 年次有給休暇」）で代用する。
    Claude 側が失敗した（空が返った）チャンクも見出しにフォールバックするので、
    APIキー未設定やレート制限で取り込み自体が止まることはない。
    """
    use_llm = USE_CONTEXTUAL_CHUNKING if contextual is None else contextual
    generated = (
        generate_chunk_contexts(text, [c.text for c in chunks])
        if use_llm
        else [""] * len(chunks)
    )
    return [g.strip() or c.heading for g, c in zip(generated, chunks)]


def _embed_source(context: str, content: str) -> str:
    """埋め込み・字面検索に渡すテキスト（文脈を本文の前に置いたもの）。"""
    return f"{context}\n\n{content}" if context else content


def content_hash(text: str, contextual: bool) -> str:
    """再取り込みで作り直しが要るかを判定するキー（documents.content_hash）。

    本文だけでなく「埋め込み結果を左右する入力」をすべて混ぜる。本文だけで
    判定すると、設定を変えて取り込み直したのに古い埋め込みが残る:
      - contextual: app.compare は同じ文書を False/True で入れ直して比較するので、
        本文だけのハッシュだと2回目がスキップされ比較が成立しない
      - EMBED_MODEL: モデルを差し替えたらベクトル空間ごと変わる
      - チャンクのサイズ設定と CHUNKING_VERSION: 切り方が変われば中身も変わる

    各要素は「長さ→中身」の順で流し込む。区切り文字で連結すると、本文にその
    区切りが混ざったときに項目の境目が動き、別々の入力が同じ並びに化けうる
    （今の項目は本文以外が固定値なので実際には作れないが、項目を足したときに
    その前提が崩れるのを避ける）。本文を丸ごとコピーせずに済む利点もある。
    """
    digest = hashlib.sha256()
    for part in (
        text,
        str(contextual),
        EMBED_MODEL,
        CHUNKING_VERSION,
        str(CHUNK_MAX_CHARS),
        str(CHUNK_MIN_CHARS),
        str(CHUNK_OVERLAP),
    ):
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return digest.hexdigest()


def ingest_text(
    source: str,
    text: str,
    project: str | None = None,
    topic: str | None = None,
    store_original: bool = True,
    contextual: bool | None = None,
    embed_retry_waits: list[int] | None = None,
) -> dict:
    """1つの文書を取り込む（upsert）。

    戻り値は {"chunks_created", "replaced", "skipped"}。
    skipped=True なら内容が変わっていないので何も作り直しておらず、
    chunks_created は「今DBにある既存チャンク数」を表す。

    同じ source の文書が既にあれば削除してから入れ直す。
    （紐づく chunks は ON DELETE CASCADE で一緒に消える）
    再取り込みで同名文書が二重に積み上がり、検索結果が重複するのを防ぐ。

    ただし content_hash が既存と一致する場合は入れ直さない。埋め込みAPIも
    Claude（文脈生成）も呼ばずに即戻る（コストとレート制限の節約）。
    区分(project/topic)だけが変わっていた場合は、埋め込みは使い回して
    documents の行だけ更新する。

    store_original: 原本テキストを S3 に保存するか。テキスト貼り付け登録では
      本文＝原本なので True。ファイルアップロード(/ingest-file)では原本は
      元のバイナリ（PDF等）であって抽出テキストではないため False を渡し、
      原本バイナリの保存は呼び出し側(#4 の save_bytes)に任せる。

    contextual: チャンクへの文脈付与に Claude を使うか。None なら設定
      (USE_CONTEXTUAL_CHUNKING)に従う。eval で有無を比較するための引数。

    embed_retry_waits: 埋め込みAPIが 429 を返したときに待つ秒数の並び
      （None = 再試行しない）。文脈生成はこの前に済ませてあるので、
      待って再試行しても Claude を呼び直さない。
    """
    use_contextual = USE_CONTEXTUAL_CHUNKING if contextual is None else contextual
    new_hash = content_hash(text, use_contextual)

    # 差分検知。分割より先に済ませ、変化が無ければ以降の処理ごと省く。
    # 接続はここで一度閉じる（この後の埋め込みAPIは秒単位で待つことがあり、
    # その間コネクションを掴んだままにしない）。
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, content_hash, project, topic FROM documents WHERE source = %s",
            (source,),
        ).fetchone()
        # content_hash が NULL の行＝この機能より前に入った文書。作り直して値を入れる。
        unchanged = existing is not None and existing[1] == new_hash
        if unchanged:
            document_id, _, current_project, current_topic = existing
            # 本文は同じで区分だけ変えた場合（登録し直しでの分類修正）。
            # 埋め込みは使い回せるので documents の行だけ更新する。
            if (current_project, current_topic) != (project, topic):
                conn.execute(
                    "UPDATE documents SET project = %s, topic = %s WHERE id = %s",
                    (project, topic, document_id),
                )
            # チャンク数はスキップ時の戻り値にしか要らないので、ここでだけ数える
            # （作り直す場合は数えても捨てるだけなので、上のSELECTには含めない）。
            chunk_count = conn.execute(
                "SELECT count(*) FROM chunks WHERE document_id = %s", (document_id,)
            ).fetchone()[0]

    if unchanged:
        # 原本の保存だけは続ける。DBに文書があってもS3側だけ欠けている状態
        # （S3障害中に取り込んだ等）を、再取り込みで直せるようにするため。
        if store_original:
            storage.save_text(source, text)
        return {"chunks_created": chunk_count, "replaced": 0, "skipped": True}

    chunks = split_chunks(text)
    if not chunks:
        return {"chunks_created": 0, "replaced": 0, "skipped": False}

    contexts = build_contexts(text, chunks, use_contextual)
    # 埋め込むのは「文脈 + 本文」。本文だけを埋め込むと、断片のままの
    # チャンクが質問のベクトルに当たらない（それが contextual retrieval の狙い）。
    embeddings = embed_texts(
        [_embed_source(ctx, c.text) for ctx, c in zip(contexts, chunks)],
        input_type="document",
        retry_waits=embed_retry_waits,
    )

    with get_conn() as conn:
        # 削除と再登録は一括で（途中で失敗しても文書が消えたままにならない）
        with conn.transaction():
            replaced = conn.execute(
                "DELETE FROM documents WHERE source = %s", (source,)
            ).rowcount

            document_id = conn.execute(
                "INSERT INTO documents (source, project, topic, content_hash) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (source, project, topic, new_hash),
            ).fetchone()[0]

            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks "
                    "(document_id, chunk_index, content, context, "
                    " content_nouns, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (
                            document_id,
                            i,
                            # content は原文のまま（回答生成にはこれを渡す）
                            chunk.text,
                            contexts[i],
                            # content_nouns: 字面検索用に名詞だけ抜き出したもの。
                            # 埋め込みと同じ「文脈+本文」から取り、BM25/trgm でも
                            # 文脈語（章名・条名）で当たるようにする
                            noun_text(_embed_source(contexts[i], chunk.text)),
                            embeddings[i],
                        )
                        for i, chunk in enumerate(chunks)
                    ],
                )

    # 原本を S3(MinIO) にも保存し、出典名からダウンロードできるようにする。
    # DBコミットの後に行う（S3が落ちていても取り込み自体は成立させる。best-effort）。
    if store_original:
        storage.save_text(source, text)

    return {"chunks_created": len(chunks), "replaced": replaced, "skipped": False}
