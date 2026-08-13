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
import urllib.parse
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
        logger.warning("原本のS3保存に失敗しました（取り込みは継続）: %s", source, exc_info=True)
        return False


def save_text(source: str, text: str) -> bool:
    """登録した原本テキストを S3 に保存する（best-effort）。保存できたら True。

    テキスト貼り付け登録(/ingest)用。原本＝本文テキストなので UTF-8 で保存する。
    バイナリ原本（PDF等）は save_bytes を使う。
    """
    return save_bytes(source, text.encode("utf-8"), _content_type(source))


# 文書内画像のキーに付けるプレフィックス。原本ファイル（キー = 出典名そのもの）と
# 名前空間を分け、「登録した文書の一覧」と「そこから取り出した画像」を混ぜない。
IMAGE_KEY_PREFIX = "images/"


def image_key(source: str, index: int, ext: str) -> str:
    """文書内画像のS3キー。`images/<出典名>/0001.png` の形。

    index を0埋めするのは、キーの辞書順とページ順を一致させるため
    （S3のリスト表示で 10 が 2 より前に来るのを防ぐ）。
    """
    return f"{IMAGE_KEY_PREFIX}{source}/{index:04d}{ext}"


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


SIGNED_URL_TTL = 300  # 署名URLの有効期限（秒）。根拠を開くだけなので短くてよい


def file_url(source: str) -> str | None:
    """原本を開くURL。S3に原本が無ければ None。

    根拠（引用）から原本そのものへ飛べるようにするためのURL。返す形は環境で変わる:

      - 実S3（S3_ENDPOINT_URL 未設定）… ★署名URL★を返す。ブラウザから直接
        S3を叩けるので backend を経由しない（大きいPDFを中継しなくて済む）。
      - ローカルのMinIO（S3_ENDPOINT_URL 設定済み）… backend中継の /files/... を返す。
        署名URLのホストが docker 内の名前(minio:9000)になり、ブラウザから
        名前解決できないため。ここで環境差を吸収し、UIは受け取ったURLを開くだけにする。

    存在確認(head_object)を挟むのは、原本が無い文書（この機能より前に登録した
    もの）に対して「開けないリンク」を出さないため。
    """
    if not exists(source):
        return None
    if S3_ENDPOINT_URL:
        return f"/files/{urllib.parse.quote(source)}"
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": source},
            ExpiresIn=SIGNED_URL_TTL,
        )
    except Exception:
        logger.warning("署名URLの発行に失敗しました: %s", source, exc_info=True)
        return None


def delete_objects(keys: list[str]) -> int:
    """S3のオブジェクトを消す（best-effort）。消せた件数を返す。

    文書を削除したときに、原本と文書内画像をS3に残さないための入口。
    DBから行が消えているのにS3にだけ原本が残っていても、それを指す経路
    （/files/<出典名>）がもう無いので取り出せず、容量だけを使い続ける。

    ★DBの削除とは別トランザクション★ S3にはトランザクションが無いので、
    DBの削除を確定させてからここを呼ぶ。順序を逆にすると「S3は消えたのに
    DBの削除が失敗した」＝原本の無い文書、という直しにくい状態になる。
    こちらの失敗は消し残し（次の削除や後始末で回収できる）にとどまる。

    S3未設定なら何もせず0。個別の失敗は警告ログにとどめ、残りの削除は続ける。
    """
    if not is_enabled() or not keys:
        return 0
    deleted = 0
    for key in keys:
        try:
            _client().delete_object(Bucket=S3_BUCKET, Key=key)
            deleted += 1
        except Exception:
            logger.warning("S3オブジェクトの削除に失敗しました: %s", key, exc_info=True)
    return deleted


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
