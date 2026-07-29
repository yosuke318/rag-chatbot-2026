"""公開API(/v1)のAPIキー: 発行・検証・レート制限・利用ログ。

なぜ要るか:
  検索と回答生成は既にAPIになっているが、そのままでは「誰でも・無制限に・
  全文書に対して」叩けてしまう。外部に出すには最低限、
    誰が(認証) / どの範囲を(テナント分離) / どれだけ(レート制限)
  の3つを決める必要がある。ここはその3つだけを担当し、検索・生成の中身
  （retrieval / llm）には触らない。

設計:
  - ★平文のキーはDBに保存しない★ 発行時に一度だけ表示し、DBには sha256 だけ置く。
    照合はハッシュ同士なので、DBが漏れてもそのキーで叩けるようにはならない。
  - ★テナント分離キーは project★ キーに project を紐付け、検索範囲はキー側で決める。
    リクエストで project を指定させない（app.main の /v1 が明示的に弾く）。
  - キーの発行はHTTPではなくCLI（このモジュールの __main__）で行う。
    このアプリには管理者認証が無く、キー発行エンドポイントを開けると
    「誰でもテナントキーを作れる」＝分離が無意味になるため。

発行:
    python -m app.apikeys --create --name "営業部ツール" --project 社内規程
    python -m app.apikeys --list
    python -m app.apikeys --revoke 3
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
from dataclasses import dataclass

from app.config import API_RATE_LIMIT_PER_MIN
from app.db import get_conn

# 見ただけでこのサービスのキーだと分かる接頭辞（GitHub等の漏洩検知にも掛けやすい）
KEY_PREFIX = "ragk_"


class ApiKeyError(Exception):
    """キーが提示されていない・形式が不正・存在しない・失効している。"""


class RateLimitExceeded(Exception):
    """このキーの直近1分の上限を超えた。"""

    def __init__(self, limit: int):
        super().__init__(f"レート制限（{limit} リクエスト/分）を超えました")
        self.limit = limit


@dataclass(frozen=True)
class ApiKey:
    """検証を通ったキー。テナント(project)はここから取り、リクエストからは取らない。"""

    id: int
    name: str
    project: str
    rate_limit_per_min: int


def generate_token() -> str:
    """新しいキー文字列。秘密なので secrets（暗号論的乱数）で作る。"""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """DBに保存・照合する形。

    パスワードと違い「推測されうる低エントロピーの秘密」ではない（256bit乱数）ので、
    総当たり耐性のための遅いハッシュ(bcrypt等)は不要。毎リクエスト引くので速さを取る。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    """`Authorization: Bearer xxx` からトークンを取り出す。取れなければ None。

    スキーム名の大小は区別しない（HTTPの仕様上どちらも正）。
    """
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


# --- 発行・管理（CLI から使う） -----------------------------------------------


def create_key(
    name: str, project: str, rate_limit_per_min: int = API_RATE_LIMIT_PER_MIN
) -> tuple[str, int]:
    """キーを発行して (平文トークン, id) を返す。★平文を得られるのはこの瞬間だけ★"""
    if not name.strip() or not project.strip():
        raise ValueError("name と project は必須です（project がテナントの境界になります）")
    token = generate_token()
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO api_keys (name, key_hash, project, rate_limit_per_min) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (name.strip(), token_hash(token), project.strip(), rate_limit_per_min),
        ).fetchone()
    return token, int(row[0])


def revoke_key(key_id: int) -> bool:
    """キーを失効させる。行は消さない（利用ログを残すため）。既に失効なら False。"""
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE api_keys SET revoked_at = now() "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (key_id,),
        ).fetchone()
    return row is not None


def list_keys() -> list[dict]:
    """発行済みキーの一覧（平文は出せないので id と素性だけ）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, project, rate_limit_per_min, created_at, revoked_at "
            "FROM api_keys ORDER BY id"
        ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "project": r[2],
            "rate_limit_per_min": r[3],
            "created_at": r[4],
            "revoked_at": r[5],
        }
        for r in rows
    ]


# --- 検証・レート制限・利用ログ（リクエストごと） -----------------------------


def lookup(token: str) -> ApiKey | None:
    """トークンから有効なキーを引く。無効・失効なら None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, project, rate_limit_per_min FROM api_keys "
            "WHERE key_hash = %s AND revoked_at IS NULL",
            (token_hash(token),),
        ).fetchone()
    if row is None:
        return None
    return ApiKey(id=int(row[0]), name=row[1], project=row[2], rate_limit_per_min=int(row[3]))


def count_recent(key_id: int) -> int:
    """直近1分間にこのキーが受け付けられた本数。

    固定ウィンドウではなく「now() から遡って1分」で数える（時計の切れ目で
    上限の2倍が通るのを避けるため）。利用ログをそのまま数えるので、
    プロセスを再起動しても制限が緩まない。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) FROM api_usage "
            "WHERE api_key_id = %s AND created_at > now() - interval '1 minute'",
            (key_id,),
        ).fetchone()
    return int(row[0])


def log_request(key_id: int, path: str) -> int:
    """受け付けた事実を記録して行IDを返す（status は応答時に埋める）。"""
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO api_usage (api_key_id, path) VALUES (%s, %s) RETURNING id",
            (key_id, path),
        ).fetchone()
    return int(row[0])


def set_status(usage_id: int, status: int) -> None:
    """利用ログに応答のHTTPステータスを書き戻す。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE api_usage SET status = %s WHERE id = %s", (status, usage_id)
        )


def authenticate(authorization: str | None, path: str) -> tuple[ApiKey, int]:
    """認証 → レート制限 → 利用ログ を順に行い、(キー, 利用ログID) を返す。

    ★順番に意味がある★
      1. 認証: 誰か分からなければ数えようがない
      2. レート制限: 上限超えは検索も生成もせずに弾く（外部APIを呼ばせない）
      3. 記録: ここで1行入れることで、この本数が次のリクエストの判定に効く
    """
    token = bearer_token(authorization)
    if token is None:
        raise ApiKeyError("APIキーが必要です（Authorization: Bearer ...）")
    key = lookup(token)
    if key is None:
        raise ApiKeyError("APIキーが無効か失効しています")
    if count_recent(key.id) >= key.rate_limit_per_min:
        raise RateLimitExceeded(key.rate_limit_per_min)
    return key, log_request(key.id, path)


# --- CLI ----------------------------------------------------------------------


def _main() -> None:
    parser = argparse.ArgumentParser(description="公開API(/v1)のAPIキー管理")
    parser.add_argument("--create", action="store_true", help="キーを発行する")
    parser.add_argument("--name", type=str, default=None, help="発行先の名前")
    parser.add_argument(
        "--project", type=str, default=None, help="このキーが見られるプロジェクト"
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=API_RATE_LIMIT_PER_MIN,
        help=f"1分あたりの上限（既定 {API_RATE_LIMIT_PER_MIN}）",
    )
    parser.add_argument("--list", action="store_true", help="発行済みの一覧")
    parser.add_argument("--revoke", type=int, default=None, help="失効させるキーのID")
    args = parser.parse_args()

    if args.create:
        if not args.name or not args.project:
            parser.error("--create には --name と --project が必要です")
        token, key_id = create_key(args.name, args.project, args.rate)
        print(f"id={key_id} name={args.name} project={args.project} rate={args.rate}/分")
        print(f"APIキー: {token}")
        print("★この値を表示できるのは今だけです（DBにはハッシュしか残りません）")
        return

    if args.revoke is not None:
        ok = revoke_key(args.revoke)
        print(f"id={args.revoke} を失効しました" if ok else "対象が無いか既に失効済みです")
        return

    if args.list:
        for k in list_keys():
            state = "失効" if k["revoked_at"] else "有効"
            print(
                f"id={k['id']} [{state}] project={k['project']} "
                f"rate={k['rate_limit_per_min']}/分 name={k['name']}"
            )
        return

    parser.print_help()


if __name__ == "__main__":
    _main()
