"""project / topic での検索スコープ絞り込みのテスト（YOSUKE-20）。

確かめること:
  - 3つの検索関数それぞれが WHERE に区分の条件を足し、値を正しい順で渡すこと
  - ★BM25は統計(N/avgdl/IDF)を作る doc CTE の側で絞ること★
    （最後だけ絞ると、他区分を含むコーパスで付いたスコアを並べ替えることになる）
  - 未指定の軸は条件を作らないこと（＝全体検索のまま）
  - /search・/chat が受け取った区分をそのまま検索へ渡すこと（空文字は未指定）

DBには繋がず、get_conn を偽物に差し替えて「投げたSQLと値」を検査する。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

retrieval = pytest.importorskip("app.retrieval")

_scope_sql = retrieval._scope_sql


class FakeConn:
    """conn.execute(sql, params).fetchall() だけを満たす偽コネクション。

    投げられたSQLと値を calls に記録する。with 文で使うので __enter__/__exit__ も持つ。
    """

    def __init__(self, calls: list, rows: list):
        self.calls = calls
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return type("R", (), {"fetchall": lambda _self: self.rows})()


@pytest.fixture
def sql_spy(monkeypatch):
    """検索関数が投げたSQLを記録する。戻り値は (sql, params) のリスト。"""
    calls: list = []
    # 列の並びは (id, content, source, image_path, context, 指標)。
    # image_path は「本文チャンクか画像チャンクか」を評価側が見分けるため、
    # context は画像チャンクの由来（「3ページ目」等）を回答生成に渡すため。
    rows = [(1, "本文", "有給休暇.txt", None, None, 0.1)]
    monkeypatch.setattr(retrieval, "get_conn", lambda: FakeConn(calls, rows))
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [0.1, 0.2, 0.3])
    return calls


# --- 句の組み立て -------------------------------------------------------------


def test_scope_sql_without_scope_is_empty():
    assert _scope_sql(None, None) == ("", [])


# 行が持つのは id 参照（documents.project_id / topic_id）だが、APIの境界は名前の
# ままなので、_scope_sql はサブクエリで名前を id に引く。
#   project: 名前ユニーク → スカラサブクエリで =（無い名前は NULL = 常に偽 = 0件）
#   topic  : 同名が複数プロジェクトに在り得る → IN で全部拾う
PROJECT_CLAUSE = " AND d.project_id = (SELECT id FROM projects WHERE name = %s)"
TOPIC_CLAUSE = " AND d.topic_id IN (SELECT id FROM topics WHERE name = %s)"


def test_scope_sql_project_only():
    sql, values = _scope_sql("社内規程", None)
    assert sql == PROJECT_CLAUSE
    assert values == ["社内規程"]


def test_scope_sql_topic_only():
    sql, values = _scope_sql(None, "労務")
    assert sql == TOPIC_CLAUSE
    assert values == ["労務"]


def test_scope_sql_both_axes():
    sql, values = _scope_sql("社内規程", "労務")
    assert sql == PROJECT_CLAUSE + TOPIC_CLAUSE
    assert values == ["社内規程", "労務"]


# --- ベクトル検索 -------------------------------------------------------------


def test_vector_search_filters_and_orders_params(sql_spy):
    retrieval.vector_search("有給は何日?", k=5, project="社内規程", topic="労務")

    sql, params = sql_spy[0]
    assert PROJECT_CLAUSE in sql and TOPIC_CLAUSE in sql
    # 値の並びは SQL 中の %s の並びと一致していないと別の条件になる:
    #   SELECT の距離計算 → WHERE の区分 → ORDER BY の距離計算 → LIMIT
    assert params == ([0.1, 0.2, 0.3], "社内規程", "労務", [0.1, 0.2, 0.3], 5)


def test_vector_search_without_scope_has_no_filter(sql_spy):
    retrieval.vector_search("有給は何日?", k=5)

    sql, params = sql_spy[0]
    assert "d.project" not in sql and "d.topic" not in sql
    assert params == ([0.1, 0.2, 0.3], [0.1, 0.2, 0.3], 5)


# --- 字面検索（トライグラム） -------------------------------------------------


def test_lexical_search_filters_and_orders_params(sql_spy):
    retrieval.lexical_search("有給休暇", k=5, project="社内規程", topic="労務")

    sql, params = sql_spy[0]
    assert PROJECT_CLAUSE in sql and TOPIC_CLAUSE in sql
    # 名詞列 → 名詞列・閾値(WHERE) → 区分 → 名詞列(ORDER BY) → LIMIT
    assert params[3] == "社内規程" and params[4] == "労務"
    assert params[-1] == 5


def test_lexical_search_without_scope_has_no_filter(sql_spy):
    retrieval.lexical_search("有給休暇", k=5)

    sql, params = sql_spy[0]
    assert "d.project" not in sql and "d.topic" not in sql
    assert len(params) == 5  # 名詞列×3・閾値・LIMIT


# --- BM25 ---------------------------------------------------------------------


def test_bm25_scope_applies_before_corpus_stats(sql_spy):
    """★区分の条件は統計を作る doc CTE に入る★（N/avgdl/IDF がその区分内で出る）。"""
    retrieval.bm25_search("有給休暇", k=5, project="社内規程", topic="労務")

    sql, _params = sql_spy[0]
    assert sql.index(PROJECT_CLAUSE) < sql.index("stats AS")
    assert sql.index(TOPIC_CLAUSE) < sql.index("stats AS")


def test_bm25_params_order_puts_scope_before_constants(sql_spy):
    retrieval.bm25_search("有給休暇", k=5, project="社内規程", topic="労務")

    _sql, params = sql_spy[0]
    # 名詞の配列(q CTE) → 区分(doc CTE) → k1,k1,b,b(scored) → LIMIT
    assert isinstance(params[0], list)
    assert params[1] == "社内規程" and params[2] == "労務"
    assert params[-1] == 5


def test_bm25_without_scope_has_no_filter(sql_spy):
    retrieval.bm25_search("有給休暇", k=5)

    sql, params = sql_spy[0]
    assert "d.project" not in sql and "d.topic" not in sql
    assert params[1:] == (retrieval.BM25_K1, retrieval.BM25_K1,
                          retrieval.BM25_B, retrieval.BM25_B, 5)


# --- 融合側が各手法へ渡すか ---------------------------------------------------


@pytest.fixture
def retriever_spy(monkeypatch):
    """RETRIEVERS の中身を記録用に差し替える。戻り値は手法名→受け取ったkwargs。"""
    seen: dict[str, dict] = {}

    def make(name):
        def fake(
            question,
            params=None,
            query_vec=None,
            image_query_vec=None,
            project=None,
            topic=None,
        ):
            seen[name] = {"project": project, "topic": topic}
            return [{"id": 1, "content": "本文", "source": "有給休暇.txt",
                     "cosine_similarity": 0.9, "trgm_similarity": 0.5,
                     "bm25_score": 1.2}]

        return fake

    for name in list(retrieval.RETRIEVERS):
        monkeypatch.setitem(retrieval.RETRIEVERS, name, make(name))
    return seen


def test_hybrid_search_passes_scope_to_every_retriever(retriever_spy):
    retrieval.hybrid_search(
        "有給は何日?",
        rerank=False,
        retrievers=["vector", "trgm", "bm25"],
        project="社内規程",
        topic="労務",
    )

    assert retriever_spy == {
        "vector": {"project": "社内規程", "topic": "労務"},
        "trgm": {"project": "社内規程", "topic": "労務"},
        "bm25": {"project": "社内規程", "topic": "労務"},
    }


def test_search_stages_passes_scope_to_every_retriever(retriever_spy):
    retrieval.search_stages(
        "有給は何日?", retrievers=["vector", "bm25"], project="社内規程"
    )

    assert retriever_spy == {
        "vector": {"project": "社内規程", "topic": None},
        "bm25": {"project": "社内規程", "topic": None},
    }


# --- API境界 ------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app import main as main_module

    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def test_search_endpoint_forwards_scope(client):
    from app import main as main_module

    empty = {"question": "q", "retrievers": [], "available_retrievers": [],
             "applied_params": {"rrf_k": 60, "retrievers": {}},
             "lexical_min_similarity": 0.0, "stages": [], "fused": []}
    with patch.object(main_module, "search_stages", return_value=empty) as m, \
         patch("app.saved_questions.save"):  # 質問の自動保管(DB)はここでは関心外
        res = client.get("/search?q=有給&project=社内規程&topic=労務")

    assert res.status_code == 200
    assert m.call_args.kwargs["project"] == "社内規程"
    assert m.call_args.kwargs["topic"] == "労務"


def test_search_endpoint_treats_blank_scope_as_unspecified(client):
    from app import main as main_module

    empty = {"question": "q", "retrievers": [], "available_retrievers": [],
             "applied_params": {"rrf_k": 60, "retrievers": {}},
             "lexical_min_similarity": 0.0, "stages": [], "fused": []}
    with patch.object(main_module, "search_stages", return_value=empty) as m, \
         patch("app.saved_questions.save"):
        client.get("/search?q=有給&project=&topic=%20%20")

    assert m.call_args.kwargs["project"] is None
    assert m.call_args.kwargs["topic"] is None


# /projects・/topics（＝区分の選択肢）のテストは test_scopes.py にある。
# 選択肢の出どころが「文書と質問の DISTINCT 導出」からマスタへ移ったため
# （YOSUKE-35）、ここではなくマスタ側のテストで見る。このファイルが受け持つのは
# 「選んだ区分が検索の WHERE に効くか」。


def test_chat_forwards_scope_to_search(client, monkeypatch):
    from app import conversations, storage
    from app import main as main_module

    seen: dict = {}

    def fake_search(question, **kwargs):
        seen.update(kwargs)
        return [{"id": 1, "content": "本文", "source": "有給休暇.txt"}]

    monkeypatch.setattr(main_module, "hybrid_search", fake_search)
    monkeypatch.setattr(main_module, "generate_answer", lambda *a, **kw: "回答 [1]")
    monkeypatch.setattr(storage, "file_url", lambda source: None)
    monkeypatch.setattr(
        conversations, "resolve", lambda cid, title=None, api_key_id=None: cid or 1
    )
    monkeypatch.setattr(conversations, "load_history", lambda cid: [])
    monkeypatch.setattr(conversations, "add_message", lambda *a, **kw: 1)

    res = client.post(
        "/chat",
        json={"question": "有給は何日?", "project": " 社内規程 ", "topic": ""},
    )

    assert res.status_code == 200
    # 前後の空白は落とし、空文字は「未指定」にしてから検索へ渡す
    assert seen["project"] == "社内規程"
    assert seen["topic"] is None
