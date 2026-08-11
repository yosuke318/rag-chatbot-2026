"""質問の自動保管と④のRRF検証のテスト。

確かめること:
  - ②で検索すると、そのときの区分と一緒に質問が保管されること
  - 同じ区分の同じ質問は積み上がらないこと（重複はDBのユニーク索引に任せる）
  - /verify が「質問の絞り込み」と「検索スコープ」の両方に同じ区分を効かせること
  - 質問のベクトル化がまとめて1回であること（1問1回だとVoyageの3RPMで完走しない）
  - 評価(/eval)も質問と同じ区分の文書だけを対象にすること

DBには繋がず、get_conn と検索を差し替えて検証する。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import eval as eval_module  # noqa: E402
from app import main as main_module  # noqa: E402
from app import saved_questions  # noqa: E402

EMPTY_STAGES = {
    "question": "q",
    "retrievers": [],
    "available_retrievers": [],
    "applied_params": {"rrf_k": 60, "retrievers": {}},
    "lexical_min_similarity": 0.0,
    "stages": [],
    "fused": [],
}


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


# --- 保管 ---------------------------------------------------------------------


def test_save_uses_on_conflict_do_nothing(monkeypatch):
    """★重複判定はDBに任せる★ SELECTしてからINSERTだと連打で二重に入る。"""
    calls: list = []
    monkeypatch.setattr(
        saved_questions, "get_conn", lambda: FakeConn(calls, one=(1,))
    )
    # 区分のマスタ登録は別の関心事（test_scopes.py で見る）なので固定idを返す
    monkeypatch.setattr(saved_questions.scopes, "register", lambda p, t: (7, 8))
    assert saved_questions.save("有給は?", "社内規程", "労務") is True

    sql, params = calls[0]
    assert "ON CONFLICT (project_id, topic_id, question) DO NOTHING" in sql
    # 行に入るのは名前ではなくマスタの id
    assert params == (7, 8, "有給は?")


def test_save_reports_false_when_already_stored(monkeypatch):
    """既にある質問は RETURNING が空＝保管しなかった、として False。"""
    monkeypatch.setattr(
        saved_questions, "get_conn", lambda: FakeConn([], one=None)
    )
    assert saved_questions.save("有給は?", None, None) is False


def test_save_ignores_blank_question(monkeypatch):
    monkeypatch.setattr(
        saved_questions,
        "get_conn",
        lambda: pytest.fail("空の質問でDBを触ってはいけない"),
    )
    assert saved_questions.save("   ") is False


def test_search_stores_question_with_selected_scope(client):
    """②で検索したら、そのときの区分と一緒に保管される。"""
    with patch.object(main_module, "search_stages", return_value=EMPTY_STAGES), \
         patch("app.saved_questions.save") as save:
        res = client.get("/search?q=有給は何日?&project=社内規程&topic=労務")

    assert res.status_code == 200
    assert save.call_args.args == ("有給は何日?", "社内規程", "労務")


def test_search_stores_blank_scope_as_none(client):
    """区分未選択のときは NULL で保管する（空文字と混ざらないように）。"""
    with patch.object(main_module, "search_stages", return_value=EMPTY_STAGES), \
         patch("app.saved_questions.save") as save:
        client.get("/search?q=有給は何日?&project=&topic=")

    assert save.call_args.args == ("有給は何日?", None, None)


def test_search_does_not_store_when_search_fails(client):
    """検索が失敗したら保管しない（typoや設定ミスの質問を溜めない）。"""
    from app.retrieval import UnknownRetriever

    with patch.object(
        main_module, "search_stages", side_effect=UnknownRetriever("未知の検索手法: x")
    ), patch("app.saved_questions.save") as save:
        res = client.get("/search?q=有給&retrievers=x")

    assert res.status_code == 400
    assert save.call_count == 0


def test_saved_questions_endpoint_filters_by_scope(client):
    with patch.object(saved_questions, "load", return_value=[]) as load:
        res = client.get("/saved-questions?project=社内規程&topic=")

    assert res.status_code == 200
    assert load.call_args.args == ("社内規程", None)


def test_post_saved_question_reports_duplicate_as_saved_false(client):
    with patch.object(saved_questions, "save", return_value=False):
        res = client.post("/saved-questions", json={"question": "有給は?"})

    # 重複はエラーではない（200のまま saved=false で伝える）
    assert res.status_code == 200
    assert res.json()["saved"] is False


def test_post_saved_question_rejects_blank(client):
    res = client.post("/saved-questions", json={"question": "  "})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_saved_question"


# --- 検証（/verify） ----------------------------------------------------------


@pytest.fixture
def verify_spy(monkeypatch):
    """search_stages と embed_texts を記録用に差し替える。"""
    seen: dict = {"searches": [], "embedded": []}

    def fake_stages(question, **kwargs):
        seen["searches"].append({"question": question, **kwargs})
        return {**EMPTY_STAGES, "question": question}

    def fake_embed(texts, input_type=None, retry_waits=None):
        seen["embedded"].append(list(texts))
        return [[0.1] for _ in texts]

    monkeypatch.setattr(saved_questions, "search_stages", fake_stages)
    monkeypatch.setattr(saved_questions, "embed_texts", fake_embed)
    return seen


def test_verify_scopes_both_questions_and_documents(verify_spy, monkeypatch):
    """★質問の絞り込みと検索スコープに同じ区分が効く★"""
    loaded: dict = {}

    def fake_load(project=None, topic=None):
        loaded.update({"project": project, "topic": topic})
        return [
            {"id": 1, "question": "有給は?", "project": "社内規程", "topic": "労務"},
            {"id": 2, "question": "経費は?", "project": "社内規程", "topic": "労務"},
        ]

    monkeypatch.setattr(saved_questions, "load", fake_load)
    report = saved_questions.verify(project="社内規程", topic="労務", top_k=4)

    # 質問側の絞り込み
    assert loaded == {"project": "社内規程", "topic": "労務"}
    # 文書側（検索スコープ）も同じ区分
    for call in verify_spy["searches"]:
        assert call["project"] == "社内規程" and call["topic"] == "労務"
        assert call["top_n"] == 4
    assert report["n"] == 2
    assert [r["question"] for r in report["results"]] == ["有給は?", "経費は?"]


def test_verify_embeds_all_questions_in_one_call(verify_spy, monkeypatch):
    """1問ずつ埋め込むとVoyage無料枠(3 RPM)で4問目に当たるのでまとめて1回。"""
    monkeypatch.setattr(
        saved_questions,
        "load",
        lambda p=None, t=None: [
            {"id": i, "question": f"質問{i}", "project": None, "topic": None}
            for i in range(5)
        ],
    )
    saved_questions.verify()

    assert len(verify_spy["embedded"]) == 1  # 呼び出しは1回
    assert verify_spy["embedded"][0] == [f"質問{i}" for i in range(5)]


def test_verify_skips_embedding_without_vector_retriever(verify_spy, monkeypatch):
    """trgm/bm25 だけの構成なら埋め込みAPIを呼ばない。"""
    monkeypatch.setattr(saved_questions, "resolve_retrievers", lambda names: ["bm25"])
    monkeypatch.setattr(
        saved_questions,
        "load",
        lambda p=None, t=None: [
            {"id": 1, "question": "有給は?", "project": None, "topic": None}
        ],
    )
    saved_questions.verify()

    assert verify_spy["embedded"] == []


def test_verify_with_no_saved_questions_returns_empty_report(verify_spy, monkeypatch):
    monkeypatch.setattr(saved_questions, "load", lambda p=None, t=None: [])
    report = saved_questions.verify(project="社内規程")

    assert report == {
        "n": 0,
        "top_k": saved_questions.TOP_K,
        "project": "社内規程",
        "topic": None,
        "results": [],
    }
    assert verify_spy["embedded"] == []


def test_verify_endpoint_normalizes_blank_scope(client):
    with patch.object(
        saved_questions,
        "verify",
        return_value={"n": 0, "top_k": 4, "project": None, "topic": None, "results": []},
    ) as v:
        res = client.get("/verify?project=&topic=%20&top_k=4")

    assert res.status_code == 200
    assert v.call_args.kwargs == {"project": None, "topic": None, "top_k": 4}


# --- 評価も質問と同じ区分の文書を見る -----------------------------------------


def test_eval_searches_within_each_questions_scope(monkeypatch):
    """「社内規程の質問」を全文書から探すと他プロジェクトが上位を埋めてしまう。"""
    seen: list = []

    def fake_search(question, **kwargs):
        seen.append({"project": kwargs.get("project"), "topic": kwargs.get("topic")})
        return []

    monkeypatch.setattr(eval_module, "hybrid_search", fake_search)
    gold = [
        {"question": "有給は?", "expected_source": "有給休暇.txt",
         "project": "社内規程", "topic": "労務"},
        {"question": "共通の質問", "expected_source": "x.txt",
         "project": None, "topic": None},
    ]
    eval_module.evaluate(gold=gold, retrievers=["bm25"])

    assert seen == [
        {"project": "社内規程", "topic": "労務"},
        {"project": None, "topic": None},  # 区分なしの質問は従来どおり全文書
    ]
