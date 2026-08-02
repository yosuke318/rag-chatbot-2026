"""区分(project / topic)のマスタ。

なぜ要るか:
  以前は選択肢を「documents と eval_questions に実在する値の DISTINCT」で
  導出していた。つまり ★区分は文書か質問を入れて初めて生まれる★ ので、
  「先にプロジェクトだけ作っておく」ができず、表記ゆれ（「営業部」と「営業」）も
  黙って別区分として共存した。マスタを正にすることでどちらも解ける。

id と名前の役割分担:
  DBの中では id で参照する（documents.project_id 等）。リネームは
  projects.name の UPDATE 1発で全テーブルに効く。
  ★APIの境界は名前のまま★ UIは名前で選び、URLにも名前が入る。id を外に
  出すと、フロントは名前→idの解決を毎回挟むことになり、手で叩くAPIも
  読めなくなる。名前→idの解決はこのモジュールに集約する。

書き込み側との関係:
  文書や質問に付いた区分は register() でマスタへ写し、返った id を行に入れる。
  ここを検証（未登録の区分を弾く）にしていないのは、取り込みやseedが
  「新しいプロジェクト名をその場で付ける」使い方をしており、そこを塞ぐと
  従来の手順が通らなくなるため。マスタは「選択肢の集合」であって、
  現時点では入力の関門ではない。
"""
from __future__ import annotations

from app.db import get_conn


def _project_id(conn, name: str) -> int:
    """プロジェクト名を id に引く。無ければ作る。

    INSERT を先に打って重複判定はDBに任せる（SELECTしてからINSERTだと連打で
    二重に入る。saved_questions と同じ理由）。ON CONFLICT のときは RETURNING が
    空になるので、その場合だけ SELECT で引き直す。
    """
    row = conn.execute(
        "INSERT INTO projects (name) VALUES (%s) "
        "ON CONFLICT (name) DO NOTHING RETURNING id",
        (name,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM projects WHERE name = %s", (name,)
        ).fetchone()
    return row[0]


def _topic_id(conn, name: str, project_id: int | None) -> int:
    """トピック名を id に引く。無ければ作る。同名でもプロジェクトが違えば別行。

    IS NOT DISTINCT FROM は「NULL 同士も同じと見なす =」。project_id が NULL
    （プロジェクトに属さないトピック）でも1行に定まるようにする。
    """
    row = conn.execute(
        "INSERT INTO topics (project_id, name) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING RETURNING id",
        (project_id, name),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM topics "
            "WHERE project_id IS NOT DISTINCT FROM %s AND name = %s",
            (project_id, name),
        ).fetchone()
    return row[0]


def register(
    project: str | None = None, topic: str | None = None
) -> tuple[int | None, int | None]:
    """文書・質問に付いた区分をマスタへ写し、(project_id, topic_id) を返す。

    ★書き込み側はこの id を行に入れる★（documents.project_id 等）。
    名前のまま行に持たせない（重複保持に戻ってしまう）。
    未指定(None)の軸は id も None（=「どこにも属さない共通」のまま）。
    """
    if project is None and topic is None:
        return None, None
    with get_conn() as conn:
        project_id = None if project is None else _project_id(conn, project)
        topic_id = None if topic is None else _topic_id(conn, topic, project_id)
    return project_id, topic_id


def create_project(name: str) -> bool:
    """プロジェクトを作る。作ったら True、既にあれば False。

    文書も質問も無いプロジェクトを先に用意するための入口。重複判定はDBの
    ユニーク制約に任せる（SELECTしてからINSERTだと連打で競合する）。
    """
    name = name.strip()
    if not name:
        raise ValueError("プロジェクト名が空です")
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO projects (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING RETURNING id",
            (name,),
        ).fetchone()
    return row is not None


def create_topic(name: str, project: str | None = None) -> bool:
    """トピックを作る。作ったら True、既にあれば False。

    project を付けるとその配下のトピックになる（未指定 = どのプロジェクトにも
    属さないトピック）。親のプロジェクトが未登録なら先に作る: UIは
    「プロジェクトを選ぶ → トピックを足す」の順で使うので親は在るはずだが、
    APIを直接叩いたときに存在しない親で落ちるより、意図どおり作れた方が素直。
    """
    name = name.strip()
    if not name:
        raise ValueError("トピック名が空です")
    with get_conn() as conn:
        project_id = None if project is None else _project_id(conn, project)
        row = conn.execute(
            "INSERT INTO topics (project_id, name) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (project_id, name),
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
    含めない。同名トピックが複数プロジェクトに在るので、全体一覧は DISTINCT。
    """
    with get_conn() as conn:
        if project is None:
            rows = conn.execute(
                "SELECT DISTINCT name FROM topics ORDER BY name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.name FROM topics t "
                "JOIN projects p ON p.id = t.project_id "
                "WHERE p.name = %s ORDER BY t.name",
                (project,),
            ).fetchall()
    return [r[0] for r in rows]
