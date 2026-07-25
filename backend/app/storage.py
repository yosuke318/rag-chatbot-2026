"""原本ファイルの保存・取得（S3互換ストレージ / ローカルは MinIO）。

なぜDBと別に持つか:
  検索・埋め込みには「テキスト」があれば足りるが、利用者に根拠を確かめてもらうには
  「登録した原本そのもの」をダウンロードできると良い。原本はDBに載せず S3 に置き、
  出典名からダウンロードできるようにする（設計書の「原本はS3」に対応）。

方針:
  - S3が未設定（S3_ENDPOINT_URL / S3_BUCKET が無い）ならスキップする。S3なしでも
    取り込み・検索は動くようにして、原本ダウンロードだけが無効になるようにする。
  - キーは出典名（documents.source）そのもの。UTF-8のファイル名もS3キーに使える。
"""
from __future__ import annotations

import logging
import mimetypes
from functools import lru_cache

from app.config import S3_BUCKET, S3_ENDPOINT_URL

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """原本ストレージが使える設定になっているか。"""
    return bool(S3_ENDPOINT_URL and S3_BUCKET)


@lru_cache(maxsize=1)
def _client():
    """boto3のS3クライアント（1度だけ生成）。認証情報は環境変数から自動で読む。"""
    import boto3  # 遅延import：S3を使わない構成では依存を読み込まない

    return boto3.client("s3", endpoint_url=S3_ENDPOINT_URL)


def _content_type(source: str) -> str:
    """拡張子からMIMEタイプを推定（不明はテキスト扱い）。"""
    guessed, _ = mimetypes.guess_type(source)
    return guessed or "text/plain; charset=utf-8"


def save_bytes(source: str, data: bytes, content_type: str | None = None) -> bool:
    """原本のバイト列を S3 に保存する（best-effort）。保存できたら True。

    ファイルアップロード(/ingest-file)用。PDF/XLSX/PPTX などの原本を「そのまま」
    保存し、出典名からダウンロードしたとき元のファイルとして開けるようにする。
    content_type はアップロード時の MIME（file.content_type）を優先し、無ければ
    拡張子から推定する（ダウンロード時に正しく開けるようにするため）。

    S3未設定なら何もせず False。S3が落ちている等で失敗しても取り込み自体は
    成立させたいので、例外は握って警告ログにとどめる（save_text と同じ方針）。
    """
    if not is_enabled():
        return False
    try:
        _client().put_object(
            Bucket=S3_BUCKET,
            Key=source,
            Body=data,
            ContentType=content_type or _content_type(source),
        )
        return True
    except Exception:
        logger.warning("原本のS3保存に失敗しました（取り込みは継続）: %s", source)
        return False


def save_text(source: str, text: str) -> bool:
    """登録した原本テキストを S3 に保存する（best-effort）。保存できたら True。

    テキスト貼り付け登録(/ingest)用。原本＝本文テキストなので UTF-8 で保存する。
    バイナリ原本（PDF等）は save_bytes を使う。
    """
    return save_bytes(source, text.encode("utf-8"), _content_type(source))


def exists(source: str) -> bool:
    """原本がS3に存在するか。本文をダウンロードせず head_object で確認する。"""
    if not is_enabled():
        return False
    try:
        _client().head_object(Bucket=S3_BUCKET, Key=source)
        return True
    except Exception:
        return False


def get_object(source: str) -> tuple[bytes, str] | None:
    """原本の (バイト列, MIMEタイプ) を返す。無ければ None。

    S3未設定・キー不在・取得失敗はすべて None にまとめ、呼び出し側は404にできる。
    """
    if not is_enabled():
        return None
    try:
        resp = _client().get_object(Bucket=S3_BUCKET, Key=source)
    except Exception:
        return None
    body = resp["Body"].read()
    content_type = resp.get("ContentType") or _content_type(source)
    return body, content_type


def backfill_from_texts(items: list[tuple[str, str]]) -> int:
    """(source, text) の一覧を、まだS3に無いものだけ保存する。保存した件数を返す。

    この変更より前に登録された文書を、原本ダウンロードに対応させるための後埋め。
    """
    if not is_enabled():
        return 0
    saved = 0
    for source, text in items:
        # 存在確認は head_object（本文を落とさない）。無いものだけ保存し、
        # 実際に保存できたものだけ数える。
        if not exists(source) and save_text(source, text):
            saved += 1
    return saved
