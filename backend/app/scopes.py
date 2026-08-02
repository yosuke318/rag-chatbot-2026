"""区分(project / topic)のマスタ。

なぜ要るか:
  以前は選択肢を「documents と eval_questions に実在する値の DISTINCT」で
  導出していた。つまり ★区分は文書か質問を入れて初めて生まれる★ ので、
  「先にプロジェクトだけ作っておく」ができず、表記ゆれ（「営業部」と「営業」）も
  黙って別区分として共存した。マスタを正にすることでどちらも解ける。

書き込み側との関係:
  文書や質問に付いた区分は register() でマスタへ写す（自動登録）。
  ここを検証（未登録の区分を弾く）にしていないのは、取り込みやseedが
  「新しいプロジェクト名をその場で付ける」使い方をしており、そこを塞ぐと
  従来の手順が通らなくなるため。マスタは「選択肢の集合」であって、
  現時点では入力の関門ではない。

正規化(project_id 参照)にしていない理由は app.db の DDL コメント参照。
"""
from __future__ import annotations

from app.db import get_conn


def register(project: str | None = None, topic: str | None = None) -> None:
    """文書・質問に付いた区分をマスタへ写す。既にあれば何もしない。

    ★これが無いと新しい区分がセレクタに出てこない★
      選択肢はマスタから引くようになったので、取り込み時に登録しておかないと
      「文書は入っているのに区分を選べない」状態になる。

    project が NULL で topic だけ、という組み合わせも通す（documents が
    2つを独立に NULL 可で持つため。topics 側は NULLS NOT DISTINCT で受ける）。
    """
    if project is None and topic is None:
        return
    with get_conn() as conn:
        if project is not None:
            conn.execute(
                "INSERT INTO projects (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (project,),
            )
        if topic is not None:
            # projects への INSERT が先。topics.project の FK を満たすため。
            conn.execute(
                "INSERT INTO topics (project, name) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (project, topic),
            )


def create_project(name: str) -> bool:
    """プロジェクトを作る。作ったら True、既にあれば False。

    文書も質問も無いプロジェクトを先に用意するための入口。重複判定はDBの
    主キーに任せる（SELECTしてからINSERTだと連打で競合する。saved_questions と同じ）。
    """
    name = name.strip()
    if not name:
        raise ValueError("プロジェクト名が空です")
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO projects (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING RETURNING name",
            (name,),
        ).fetchone()
    return row is not None


def create_topic(name: str, project: str | None = None) -> bool:
    """トピックを作る。作ったら True、既にあれば False。

    project を付けるとその配下のトピックになる（未指定 = どのプロジェクトにも
    属さないトピック）。親のプロジェクトが未登録なら先に作る: UIは
    「プロジェクトを選ぶ → トピックを足す」の順で使うので親は在るはずだが、
    APIを直接叩いたときに FK 違反で落ちるより、意図どおり作れた方が素直。
    """
    name = name.strip()
    if not name:
        raise ValueError("トピック名が空です")
    with get_conn() as conn:
        if project is not None:
            conn.execute(
                "INSERT INTO projects (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (project,),
            )
        row = conn.execute(
            "INSERT INTO topics (project, name) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING RETURNING name",
            (project, name),
        ).fetchone()
    return row is not None


def list_projects() -> list[str]:
    """登録済みのプロジェクト名。UIの区分セレクタを埋めるのに使う。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM projects ORDER BY name").fetchall()
    return [r[0] for r in rows]


def list_topics(project: str | None = None) -> list[str]:
    """登録済みのトピック名。project を付けるとその配下だけに絞る。

    未指定なら全プロジェクトのトピックを返す（絞り込みなし）。従来の導出SQLと
    同じ約束で、project を指定したときに「プロジェクトに属さないトピック」は
    含めない。
    """
    where = "" if project is None else " WHERE project = %s"
    params = [] if project is None else [project]
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT name FROM topics{where} ORDER BY name", params
        ).fetchall()
    return [r[0] for r in rows]
