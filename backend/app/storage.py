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

import mimetypes
from functools import lru_cache

from app.config import S3_BUCKET, S3_ENDPOINT_URL


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


def save_text(source: str, text: str) -> None:
    """登録した原本テキストを S3 に保存する（best-effort）。

    S3未設定なら何もしない。将来PDF等のバイナリを扱うときは save_bytes を足す。
    """
    if not is_enabled():
        return
    _client().put_object(
        Bucket=S3_BUCKET,
        Key=source,
        Body=text.encode("utf-8"),
        ContentType=_content_type(source),
    )


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
        if get_object(source) is None:
            save_text(source, text)
            saved += 1
    return saved
