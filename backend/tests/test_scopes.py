"""区分(project / topic)マスタのテスト（YOSUKE-35）。

確かめること:
  - 選択肢がマスタ(projects / topics)から引かれること（以前は文書と質問の
    DISTINCT を都度導出しており、文書も質問も無い区分が存在できなかった）
  - 文書も質問も無いプロジェクトを作れること
  - 文書・質問を保存すると、その区分がマスタへ自動登録されること
    （ここが抜けると「文書は入ったのに区分を選べない」状態になる）
  - 重複はエラーではなく created=false で返すこと

DBには繋がず、get_conn を差し替えて発行SQLを検証する（他のテストと同じ方針）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402
from app import saved_questions, scopes  # noqa: E402


class FakeConn:
    """conn.execute(sql, params).fetchone()/fetchall() を満たす偽コネクション。"""

    def __init__(self, calls: list, one=None, all_rows=None):
        self.calls = calls
        self.one = one
        self.all_rows = all_rows or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        outer = self
        return type(
            "R",
            (),
            {
                "fetchone": lambda _s: outer.one,
                "fetchall": lambda _s: outer.all_rows,
            },
        )()


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


# --- 自動登録（register） -----------------------------------------------------


def test_register_inserts_project_before_topic(monkeypatch):
    """★projects が先★ topics.project_id は projects(id) を参照するので、
    プロジェクトの id を得てからでないと配下のトピックを入れられない。"""
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, one=(1,)))
    assert scopes.register("社内規程", "労務") == (1, 1)

    assert len(calls) == 2
    assert "INSERT INTO projects" in calls[0][0] and calls[0][1] == ("社内規程",)
    # トピックには親プロジェクトの id を渡す（名前ではなく）
    assert "INSERT INTO topics" in calls[1][0] and calls[1][1] == (1, "労務")


def test_register_allows_topic_without_project(monkeypatch):
    """documents は project と topic を独立に NULL 可で持つ（topic だけの文書が
    作れる）ので、マスタ側もその組み合わせを受けられないと取りこぼす。"""
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, one=(5,)))
    assert scopes.register(None, "労務") == (None, 5)

    assert len(calls) == 1
    assert "INSERT INTO topics" in calls[0][0] and calls[0][1] == (None, "労務")


def test_register_does_not_touch_db_without_scope(monkeypatch):
    """区分なし（共通）の保存でDBを触らない。"""
    monkeypatch.setattr(
        scopes, "get_conn", lambda: pytest.fail("区分が無いのにDBを触ってはいけない")
    )
    assert scopes.register(None, None) == (None, None)


def test_register_looks_up_id_when_scope_already_exists(monkeypatch):
    """既にある区分は重ねず（ON CONFLICT DO NOTHING）、id は SELECT で引き直す。

    INSERT を先に打つのは連打時の競合をDBに任せるため。ON CONFLICT のときは
    RETURNING が空になるので、SELECT で既存行の id を得る。
    """
    calls: list = []

    class ConflictConn(FakeConn):
        # INSERT は競合（RETURNING なし）、SELECT は既存 id を返す
        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            outer = self
            head = sql.strip().split()[0].upper()
            return type(
                "R",
                (),
                {
                    "fetchone": lambda _s: (9,) if head == "SELECT" else None,
                    "fetchall": lambda _s: outer.all_rows,
                },
            )()

    monkeypatch.setattr(scopes, "get_conn", lambda: ConflictConn(calls))
    assert scopes.register("社内規程", None) == (9, None)

    assert "ON CONFLICT (name) DO NOTHING" in calls[0][0]
    assert "SELECT id FROM projects" in calls[1][0]


# --- 作成（create_project / create_topic） ------------------------------------


def test_create_project_reports_true_when_inserted(monkeypatch):
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, one=("社内規程",)))
    assert scopes.create_project("社内規程") is True
    assert calls[0][1] == ("社内規程",)


def test_create_project_reports_false_when_already_exists(monkeypatch):
    """RETURNING が空＝作らなかった。エラーにはしない。"""
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn([], one=None))
    assert scopes.create_project("社内規程") is False


def test_create_project_trims_name(monkeypatch):
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, one=("社内規程",)))
    scopes.create_project("  社内規程  ")
    assert calls[0][1] == ("社内規程",)


def test_create_project_rejects_blank(monkeypatch):
    monkeypatch.setattr(
        scopes, "get_conn", lambda: pytest.fail("空名でDBを触ってはいけない")
    )
    with pytest.raises(ValueError):
        scopes.create_project("   ")


def test_create_topic_creates_parent_project_first(monkeypatch):
    """親が未登録でも存在しない親で落ちるのではなく、意図どおり作れるようにする。"""
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, one=(1,)))
    assert scopes.create_topic("労務", "社内規程") is True

    assert "INSERT INTO projects" in calls[0][0]
    # トピックには親プロジェクトの id を渡す（名前ではなく）
    assert "INSERT INTO topics" in calls[1][0] and calls[1][1] == (1, "労務")


def test_create_topic_without_project(monkeypatch):
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, one=(1,)))
    scopes.create_topic("労務")

    assert len(calls) == 1  # 親を作らない
    assert calls[0][1] == (None, "労務")


# --- 一覧（マスタを引く） -----------------------------------------------------


def test_list_projects_reads_master(monkeypatch):
    """★導出ではなくマスタ★ documents/eval_questions を見に行かない。"""
    calls: list = []
    monkeypatch.setattr(
        scopes, "get_conn", lambda: FakeConn(calls, all_rows=[("社内規程",), ("営業",)])
    )
    assert scopes.list_projects() == ["社内規程", "営業"]

    sql = calls[0][0]
    assert "FROM projects" in sql
    assert "documents" not in sql and "eval_questions" not in sql


def test_list_topics_filters_by_project(monkeypatch):
    """絞り込みは親プロジェクトの名前を JOIN で引いて行う。"""
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, all_rows=[("労務",)]))
    assert scopes.list_topics("社内規程") == ["労務"]
    sql, params = calls[0]
    assert "JOIN projects" in sql and "WHERE p.name = %s" in sql
    assert params == ("社内規程",)


def test_list_topics_without_project_does_not_filter(monkeypatch):
    calls: list = []
    monkeypatch.setattr(scopes, "get_conn", lambda: FakeConn(calls, all_rows=[]))
    scopes.list_topics()
    assert "WHERE" not in calls[0][0]


# --- エンドポイント -----------------------------------------------------------


def test_get_projects_returns_master(client):
    with patch.object(scopes, "list_projects", return_value=["社内規程"]):
        res = client.get("/projects")

    assert res.status_code == 200
    assert res.json() == {"projects": ["社内規程"]}


def test_get_topics_normalizes_blank_project(client):
    """?project= は「未指定」であって project='' での絞り込みではない。"""
    with patch.object(scopes, "list_topics", return_value=[]) as list_topics:
        res = client.get("/topics?project=%20")

    assert res.status_code == 200
    assert list_topics.call_args.args == (None,)


def test_post_project_creates_empty_project(client):
    """★文書も質問も無いプロジェクトを作れる★ これが導出方式では出来なかった。"""
    with patch.object(scopes, "create_project", return_value=True) as create:
        res = client.post("/projects", json={"name": "新規事業"})

    assert res.status_code == 200
    assert res.json() == {"created": True, "name": "新規事業", "project": None}
    assert create.call_args.args == ("新規事業",)


def test_post_project_reports_duplicate_as_created_false(client):
    with patch.object(scopes, "create_project", return_value=False):
        res = client.post("/projects", json={"name": "社内規程"})

    # 重複はエラーではない（200のまま created=false で伝える）
    assert res.status_code == 200
    assert res.json()["created"] is False


def test_post_project_rejects_blank(client):
    res = client.post("/projects", json={"name": "  "})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_project"


def test_post_topic_passes_parent_project(client):
    with patch.object(scopes, "create_topic", return_value=True) as create:
        res = client.post("/topics", json={"name": "労務", "project": "社内規程"})

    assert res.status_code == 200
    assert res.json() == {"created": True, "name": "労務", "project": "社内規程"}
    assert create.call_args.args == ("労務", "社内規程")


def test_post_topic_normalizes_blank_project(client):
    with patch.object(scopes, "create_topic", return_value=True) as create:
        client.post("/topics", json={"name": "労務", "project": ""})

    assert create.call_args.args == ("労務", None)


def test_post_topic_rejects_blank_name(client):
    res = client.post("/topics", json={"name": " "})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_topic"


# --- 保存時の自動登録 ---------------------------------------------------------


def test_saving_question_registers_its_scope(monkeypatch):
    """②で新しい区分を指定して検索したら、その区分がセレクタに残る。"""
    seen: list = []

    def fake_register(p, t):
        seen.append((p, t))
        return (1, 2)

    monkeypatch.setattr(saved_questions.scopes, "register", fake_register)
    monkeypatch.setattr(saved_questions, "get_conn", lambda: FakeConn([], one=(1,)))
    saved_questions.save("有給は?", "社内規程", "労務")

    assert seen == [("社内規程", "労務")]


def test_posting_eval_question_registers_its_scope(client):
    """質問だけ登録したプロジェクトも④の評価対象として選べる必要がある。"""
    calls: list = []
    with patch.object(main_module, "get_conn", lambda: FakeConn(calls, one=(1,))), \
         patch.object(scopes, "register", return_value=(1, 2)) as register:
        res = client.post(
            "/eval-questions",
            json={
                "question": "有給は?",
                "expected_source": "有給休暇.txt",
                "project": "社内規程",
                "topic": "労務",
            },
        )

    assert res.status_code == 200
    assert register.call_args.args == ("社内規程", "労務")
    # 行に入るのは register が返した id（名前ではなく）
    sql, params = calls[0]
    assert "INSERT INTO eval_questions" in sql and "project_id" in sql
    assert params[0] == 1 and params[1] == 2
