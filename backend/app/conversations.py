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


def create(title: str | None = None) -> int:
    """会話を1件作ってIDを返す。"""
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO conversations (title) VALUES (%s) RETURNING id",
            (title,),
        ).fetchone()
    return int(row[0])


def exists(conversation_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = %s", (conversation_id,)
        ).fetchone()
    return row is not None


def resolve(conversation_id: int | None, title: str | None = None) -> int:
    """会話IDを確定する。未指定なら新規作成、存在しないIDならエラー。

    「黙って新しい会話を作る」ようにすると、IDのtypoで履歴が繋がらないまま
    会話が増え続け、原因に気づけない。存在しないIDは明示的に弾く。
    """
    if conversation_id is None:
        return create(title)
    if not exists(conversation_id):
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
