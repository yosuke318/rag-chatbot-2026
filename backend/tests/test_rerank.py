"""リランク（Voyage rerank-2 / プロンプト式）のテスト。

外部APIは呼ばずにモックする。確認するのは:
  - Voyage リランクの呼び出し形と、返ってきた順位の解釈（index の並び）
  - レート制限(429)の再試行
  - 方式(voyage / llm)の切り替えと、未知の方式を早い段階で弾くこと
  - 番号を漏らした候補を末尾に補う安全網（プロンプト式で実際に起きる）
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

voyageai = pytest.importorskip("voyageai")
retrieval = pytest.importorskip("app.retrieval")

from app import llm  # noqa: E402
from app.config import RERANK_MODEL  # noqa: E402

rerank_candidates = retrieval.rerank_candidates
resolve_rerank_method = retrieval.resolve_rerank_method
UnknownReranker = retrieval.UnknownReranker


class _Result:
    """voyageai の RerankingResult（index / document / relevance_score）の代役。"""

    def __init__(self, index: int, score: float):
        self.index = index
        self.relevance_score = score


class _Reranking:
    def __init__(self, *pairs: tuple[int, float]):
        # 本物のAPIは relevance_score の降順で返す
        self.results = [_Result(i, s) for i, s in pairs]


# --- llm.voyage_rerank -------------------------------------------------------


def test_voyage_rerank_returns_index_order():
    """APIが返した results の index をそのまま並べた順位を返す。"""
    with patch.object(llm, "_voyage") as client:
        client.rerank.return_value = _Reranking((2, 0.91), (0, 0.42), (1, 0.03))
        order = llm.voyage_rerank("有給は何日?", ["A", "B", "C"])

    assert order == [2, 0, 1]
    args, kwargs = client.rerank.call_args
    assert args[0] == "有給は何日?"      # query
    assert args[1] == ["A", "B", "C"]    # documents
    assert kwargs["model"] == RERANK_MODEL


def test_voyage_rerank_empty_passages_skips_api():
    with patch.object(llm, "_voyage") as client:
        assert llm.voyage_rerank("質問", []) == []
    client.rerank.assert_not_called()


def test_voyage_rerank_retries_on_rate_limit():
    """429 は retry_waits の秒数だけ待って再試行する（バッチ経路用）。"""
    with patch.object(llm, "_voyage") as client, patch.object(llm.time, "sleep") as sleep:
        client.rerank.side_effect = [
            voyageai.error.RateLimitError("rate limited"),
            _Reranking((1, 0.8), (0, 0.2)),
        ]
        assert llm.voyage_rerank("質問", ["A", "B"], retry_waits=[7]) == [1, 0]

    assert client.rerank.call_count == 2
    sleep.assert_called_once_with(7)


def test_voyage_rerank_raises_when_no_retry_left():
    """既定(retry_waits=None)は待たずに 429 を投げる（Web経路は即429を返す）。"""
    with patch.object(llm, "_voyage") as client:
        client.rerank.side_effect = voyageai.error.RateLimitError("rate limited")
        with pytest.raises(voyageai.error.RateLimitError):
            llm.voyage_rerank("質問", ["A"])


# --- 方式の解決 --------------------------------------------------------------


def test_resolve_rerank_method_none_uses_setting(monkeypatch):
    monkeypatch.setattr(retrieval, "RERANK_METHOD", "llm")
    assert resolve_rerank_method(None) == "llm"


def test_resolve_rerank_method_normalizes_case_and_space():
    assert resolve_rerank_method(" Voyage ") == "voyage"


def test_resolve_rerank_method_unknown_raises():
    with pytest.raises(UnknownReranker):
        resolve_rerank_method("rerank-2")  # モデル名は方式名ではない


def test_registry_has_both_methods():
    assert set(retrieval.RERANKERS) == {"voyage", "llm"}


# --- rerank_candidates -------------------------------------------------------


CANDIDATES = [
    {"id": 10, "content": "A", "source": "a.txt"},
    {"id": 11, "content": "B", "source": "b.txt"},
    {"id": 12, "content": "C", "source": "c.txt"},
]


def test_rerank_candidates_empty_returns_empty_without_calling_api(monkeypatch):
    """候補が空ならリランクAPIを呼ばずに [] を返す。"""
    monkeypatch.setitem(
        retrieval.RERANKERS, "voyage", lambda q, p, w=None: pytest.fail("呼ばれてはいけない")
    )
    assert rerank_candidates("何か質問", [], method="voyage") == []


def test_rerank_candidates_reorders_and_truncates(monkeypatch):
    monkeypatch.setitem(retrieval.RERANKERS, "voyage", lambda q, p, w=None: [2, 0, 1])
    out = rerank_candidates("質問", CANDIDATES, top_n=2, method="voyage")
    assert [c["id"] for c in out] == [12, 10]


def test_rerank_candidates_dispatches_by_method(monkeypatch):
    """method で呼ばれる実装が変わる（もう片方は呼ばれない）。"""
    monkeypatch.setitem(retrieval.RERANKERS, "voyage", lambda q, p, w=None: [0, 1, 2])
    monkeypatch.setitem(retrieval.RERANKERS, "llm", lambda q, p, w=None: [2, 1, 0])

    voyage_out = rerank_candidates("質問", CANDIDATES, method="voyage")
    llm_out = rerank_candidates("質問", CANDIDATES, method="llm")

    assert [c["id"] for c in voyage_out] == [10, 11, 12]
    assert [c["id"] for c in llm_out] == [12, 11, 10]


def test_rerank_candidates_uses_setting_when_method_omitted(monkeypatch):
    monkeypatch.setattr(retrieval, "RERANK_METHOD", "llm")
    monkeypatch.setitem(retrieval.RERANKERS, "llm", lambda q, p, w=None: [1, 0, 2])
    assert [c["id"] for c in rerank_candidates("質問", CANDIDATES)] == [11, 10, 12]


def test_rerank_candidates_appends_missing_indexes(monkeypatch):
    """番号を漏らした候補は末尾に補う（件数が減らないための安全網）。"""
    monkeypatch.setitem(retrieval.RERANKERS, "llm", lambda q, p, w=None: [2])
    out = rerank_candidates("質問", CANDIDATES, method="llm")
    assert [c["id"] for c in out] == [12, 10, 11]


def test_rerank_candidates_forwards_retry_waits(monkeypatch):
    """429の待ち時間はそのまま方式へ渡す（バッチ経路が待てるように）。"""
    seen = []
    monkeypatch.setitem(
        retrieval.RERANKERS, "voyage", lambda q, p, w=None: seen.append(w) or [0, 1, 2]
    )
    rerank_candidates("質問", CANDIDATES, method="voyage", retry_waits=[20, 40])
    assert seen == [[20, 40]]


def test_prompt_rerank_ignores_retry_waits(monkeypatch):
    """プロンプト式は待ち時間を受け取っても使わない（SDKが内部で再試行するため）。"""
    calls = []
    monkeypatch.setattr(
        retrieval, "rank_by_relevance", lambda q, p: calls.append((q, p)) or [0]
    )
    assert retrieval._prompt_rerank("質問", ["A"], retry_waits=[20]) == [0]
    assert calls == [("質問", ["A"])]


def test_rerank_candidates_unknown_method_raises():
    with pytest.raises(UnknownReranker):
        rerank_candidates("質問", CANDIDATES, method="does-not-exist")


def test_hybrid_search_validates_method_before_searching(monkeypatch):
    """方式名のtypoは検索(DB/埋め込みAPI)を走らせる前に弾く。"""
    monkeypatch.setitem(
        retrieval.RETRIEVERS, "vector", lambda *a, **kw: pytest.fail("検索前に落ちるべき")
    )
    with pytest.raises(UnknownReranker):
        retrieval.hybrid_search(
            "質問", rerank=True, rerank_method="typo", retrievers=["vector"]
        )


# --- /eval エンドポイント -----------------------------------------------------

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402

EMPTY_REPORT = {
    "n": 0, "top_k": 4, "retrievers": None, "rerank": True, "rerank_method": "llm",
    "rrf_k": None, "params": None, "hit_at_k": 0.0, "mrr": 0.0, "results": [],
}

# ここで見たいのは rerank_method の受け渡しだけだが、質問が0件だと
# GET /eval は evaluate を呼ぶ前に 404 を返す（test_eval_empty.py 参照）ので、
# 「1件はある」状態にしておく必要がある。
GOLD = [{"question": "有給は何日？", "expected_source": "有給休暇.txt"}]


@pytest.fixture(scope="module")
def client():
    """DBに触らない TestClient（init_db を差し替えて起動する）。"""
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def test_eval_passes_rerank_method_through(client):
    with patch.object(main_module, "load_questions", return_value=GOLD), \
         patch.object(main_module, "evaluate", return_value=EMPTY_REPORT) as m:
        res = client.get("/eval?rerank=true&rerank_method=llm")

    assert res.status_code == 200
    assert res.json()["rerank_method"] == "llm"
    assert m.call_args.kwargs["rerank_method"] == "llm"


def test_eval_blank_rerank_method_means_default(client):
    """`?rerank_method=` は「未指定＝設定の既定」（空文字で方式を探しにいかない）。"""
    with patch.object(main_module, "load_questions", return_value=GOLD), \
         patch.object(main_module, "evaluate", return_value={**EMPTY_REPORT,
                                                            "rerank_method": None}) as m:
        res = client.get("/eval?rerank=true&rerank_method=")

    assert res.status_code == 200
    assert m.call_args.kwargs["rerank_method"] is None


def test_eval_unknown_rerank_method_returns_400(client):
    """未知の方式は 500 ではなく、UIがそのまま出せる 400 で返す。"""
    with patch.object(main_module, "load_questions", return_value=GOLD), \
         patch.object(main_module, "evaluate",
                      side_effect=UnknownReranker("未知のリランク方式: typo")):
        res = client.get("/eval?rerank=true&rerank_method=typo")

    assert res.status_code == 400
    assert res.json()["error"] == "unknown_reranker"
