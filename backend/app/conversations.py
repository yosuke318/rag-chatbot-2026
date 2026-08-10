"""会話履歴の読み書き（conversations / messages）。

なぜ要るか:
  「有給は何日？」→「その繰り越しの上限は？」のような続きの質問は、直前の
  やり取りが無いと何を指しているのか分からない。回答生成に直近の履歴を
  混ぜられるよう、会話単位で発言を保存する。

方針:
  - 検索（どのチャンクを引くか）には履歴を使わず、質問文だけで引く。
    履歴を混ぜた検索は「前の話題に引きずられて別の文書を引く」副作用があり、
    まずは生成側にだけ効かせて挙動を追えるようにする（質問の書き換えは次段）。
  - 保存するのは本文と出典まで。チャンク単位の引用は回答ごとに作り直せるので
    履歴には持たせない（保存形式を増やさない）。
"""
from __future__ import annotations

from app.config import HISTORY_MESSAGES
from app.db import get_conn

USER = "user"
ASSISTANT = "assistant"


class UnknownConversation(ValueError):
    """指定された会話IDが存在しない。"""


def create(title: str | None = None, api_key_id: int | None = None) -> int:
    """会話を1件作ってIDを返す。

    api_key_id: 公開API(/v1)から始まった会話はその発行キーを持ち主にする。
      None = 画面(UI)から始めた会話。
    """
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO conversations (title, api_key_id) VALUES (%s, %s) RETURNING id",
            (title, api_key_id),
        ).fetchone()
    return int(row[0])


def exists(conversation_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
    return row is not None


def owned_by(conversation_id: int, api_key_id: int | None) -> bool:
    """その会話が指定の持ち主のものか。存在しなければ False。

    ★公開APIのテナント分離で効く★ これが無いと、APIの利用者が他人の
    conversation_id を渡すだけで別テナントの履歴を読み出せてしまう
    （履歴は回答生成にそのまま載るため、中身が漏れる）。
    UI からの呼び出しは api_key_id=None 同士で照合される。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = %s AND api_key_id IS NOT DISTINCT FROM %s",
            (conversation_id, api_key_id),
        ).fetchone()
    return row is not None


def resolve(
    conversation_id: int | None,
    title: str | None = None,
    api_key_id: int | None = None,
) -> int:
    """会話IDを確定する。未指定なら新規作成、存在しない・持ち主違いならエラー。

    「黙って新しい会話を作る」ようにすると、IDのtypoで履歴が繋がらないまま
    会話が増え続け、原因に気づけない。存在しないIDは明示的に弾く。

    持ち主違いも「見つかりません」に倒す（403にすると「そのIDは存在する」と
    教えることになり、他テナントのID探索の手掛かりになるため）。
    """
    if conversation_id is None:
        return create(title, api_key_id)
    if not owned_by(conversation_id, api_key_id):
        raise UnknownConversation(f"会話が見つかりません: {conversation_id}")
    return conversation_id


def add_message(
    conversation_id: int, role: str, content: str, sources: list[str] | None = None
) -> int:
    """発言を1件追加してIDを返す。"""
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, sources) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (conversation_id, role, content, sources or []),
        ).fetchone()
    return int(row[0])


def load_all(conversation_id: int) -> list[dict] | None:
    """会話の発言を全部、古い順で返す。会話が無ければ None。

    ★load_history とは用途が違う★
      あちらは生成に載せる直近N件（コストと文脈の折り合い）。こちらは人が
      読むための全文で、👎の行から「どういう流れでその質問が出たのか」を
      辿るのに使う。途中を切ると、その流れが読めなくなる。

    0件の会話（作られただけ）と存在しない会話を区別するため、空リストではなく
    None を返し分ける。
    """
    if not exists(conversation_id):
        return None
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, sources, created_at FROM messages "
            "WHERE conversation_id = %s ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "role": r[1],
            "content": r[2],
            "sources": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


def load_history(conversation_id: int, limit: int = HISTORY_MESSAGES) -> list[dict]:
    """直近の発言を古い順で返す（[{"role", "content"}, ...]）。

    ★新しい方から limit 件を取り、返すときに古い順へ戻す★
      会話が長くなるほど古い発言は効かなくなる一方、入力トークンは増え続ける。
      直近だけに絞って、コストと文脈のバランスを取る。
      並びを古い順に戻すのは、Claudeへ渡すメッセージ列が時系列である必要があるため。
    """
    if limit <= 0:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (conversation_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
