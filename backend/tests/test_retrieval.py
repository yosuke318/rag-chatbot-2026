"""app.retrieval の純ロジック（RRF融合・手法解決・整形）のテスト。

これらの関数は DB も外部APIも呼ばない。ただしモジュール import 時に
app.db(psycopg) / app.llm(anthropic, voyageai) を読み込むため、それらの
ネイティブ依存が入っていない環境（例: ローカルの arm64 で psycopg が x86 ビルド）
では import に失敗する。その場合は skip する（本番/コンテナでは実行される）。
"""
import pytest

retrieval = pytest.importorskip("app.retrieval")

reciprocal_rank_fusion = retrieval.reciprocal_rank_fusion
_rrf_scores = retrieval._rrf_scores
resolve_retrievers = retrieval.resolve_retrievers
UnknownRetriever = retrieval.UnknownRetriever
default_params = retrieval.default_params
retriever_infos = retrieval.retriever_infos
preview = retrieval.preview


# --- RRF 融合 ---------------------------------------------------------------


def test_rrf_single_list_preserves_order():
    lst = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [x["id"] for x in reciprocal_rank_fusion([lst])] == [1, 2, 3]


def test_rrf_item_in_both_lists_outranks_single_list_items():
    # id=2 は両リストの上位に出るので、片方だけに出る id=1/id=3 より上に来る
    a = [{"id": 1}, {"id": 2}]
    b = [{"id": 2}, {"id": 3}]
    assert [x["id"] for x in reciprocal_rank_fusion([a, b])][0] == 2


def test_rrf_scores_math_and_rank_map():
    a = [{"id": 1}, {"id": 2}]
    b = [{"id": 2}, {"id": 3}]
    scored = _rrf_scores([a, b], k=60)
    by_id = {item["id"]: (score, ranks) for item, score, ranks in scored}

    # id=2: リスト0で2位(rank1) + リスト1で1位(rank0) = 1/62 + 1/61
    assert by_id[2][0] == pytest.approx(1 / 62 + 1 / 61)
    assert by_id[2][1] == {0: 1, 1: 0}
    # id=1: リスト0のみ 1位(rank0) = 1/61
    assert by_id[1][0] == pytest.approx(1 / 61)
    assert by_id[1][1] == {0: 0}
    # id=3: リスト1のみ 2位(rank1) = 1/62
    assert by_id[3][0] == pytest.approx(1 / 62)


def test_rrf_empty_input_returns_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


# --- 手法名の解決 -----------------------------------------------------------


def test_resolve_dedups_and_preserves_order():
    assert resolve_retrievers(["vector", "vector", "trgm"]) == ["vector", "trgm"]
    assert resolve_retrievers(["bm25", "vector"]) == ["bm25", "vector"]


def test_resolve_none_uses_known_nonempty_default():
    resolved = resolve_retrievers(None)
    assert resolved  # 空ではない
    assert all(n in retrieval.RETRIEVERS for n in resolved)


def test_resolve_empty_selection_raises():
    with pytest.raises(UnknownRetriever):
        resolve_retrievers([])


def test_resolve_unknown_name_raises():
    with pytest.raises(UnknownRetriever):
        resolve_retrievers(["vector", "does-not-exist"])


# --- パラメータ既定・メタ情報 -----------------------------------------------


def test_default_params_shapes():
    assert default_params("vector") == {}          # 調整可能な定数なし
    assert set(default_params("bm25")) == {"k1", "b"}
    assert default_params("unknown") == {}


def test_retriever_infos_lists_all_methods():
    infos = retriever_infos()
    names = {i["name"] for i in infos}
    assert names == {"vector", "trgm", "bm25"}
    for info in infos:
        assert {"name", "label", "metric_label", "params"} <= info.keys()


# --- プレビュー整形 ---------------------------------------------------------
# リランク（方式の切り替え・安全網）は test_rerank.py 側にある


def test_preview_collapses_whitespace():
    assert preview("a  b\n c\t d") == "a b c d"


def test_preview_truncates_with_ellipsis():
    out = preview("x" * 100, n=10)
    assert out == "x" * 10 + "…"


def test_preview_short_text_untouched():
    assert preview("短い本文") == "短い本文"
