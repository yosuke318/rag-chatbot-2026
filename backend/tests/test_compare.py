"""app.compare（contextual の有無のA/B測定）のユニットテスト。

DB・埋め込みAPI・Claude は触らずモックする。ここで守りたいのは
「比較が公平であること」＝ 質問ベクトルと検索条件が両構成で同一であること。
"""
import pytest

from app import compare as compare_module

GOLD = [
    {"question": "Q1", "expected_source": "a.txt"},
    {"question": "Q2", "expected_source": "b.txt"},
]


@pytest.fixture
def harness(monkeypatch):
    """取り込みと評価を差し替え、呼ばれ方を記録する。"""
    calls: dict[str, list] = {"reingest": [], "evaluate": [], "embed": []}

    def fake_reingest(contextual, seed_dir=None):
        calls["reingest"].append(contextual)
        return 5 if contextual else 4

    def fake_embed(texts, input_type="document", retry_waits=None):
        calls["embed"].append(list(texts))
        return [[1.0], [2.0]]

    def fake_evaluate(top_k=4, retrievers=None, gold=None, query_vecs=None, **kw):
        calls["evaluate"].append(
            {"top_k": top_k, "retrievers": retrievers, "query_vecs": query_vecs}
        )
        # 直前の reingest が contextual=True だったかで結果を変える
        contextual = calls["reingest"][-1]
        ranks = [0, 0] if contextual else [1, None]
        return {
            "n": len(gold),
            "top_k": top_k,
            "hit_at_k": 1.0 if contextual else 0.5,
            "mrr": 1.0 if contextual else 0.5,
            "results": [
                {"question": g["question"], "rank": r} for g, r in zip(gold, ranks)
            ],
        }

    monkeypatch.setattr(compare_module, "reingest", fake_reingest)
    monkeypatch.setattr(compare_module, "embed_texts", fake_embed)
    monkeypatch.setattr(compare_module, "evaluate", fake_evaluate)
    monkeypatch.setattr(compare_module, "resolve_retrievers", lambda n: ["vector"])
    return calls


def test_runs_both_configurations(harness):
    outcome = compare_module.compare(gold=GOLD)

    assert set(harness["reingest"]) == {False, True}
    assert set(outcome["runs"]) == {False, True}
    assert outcome["runs"][True]["hit_at_k"] == 1.0
    assert outcome["runs"][False]["hit_at_k"] == 0.5


def test_embeds_questions_once_and_reuses_them(harness):
    """★比較の公平性★ 両構成に同一の質問ベクトルを渡す（埋め込みも1回だけ）。"""
    compare_module.compare(gold=GOLD)

    assert len(harness["embed"]) == 1, "構成ごとに質問を埋め込み直している"
    assert harness["embed"][0] == ["Q1", "Q2"]

    vecs = [c["query_vecs"] for c in harness["evaluate"]]
    assert vecs[0] == vecs[1] == [[1.0], [2.0]]


def test_uses_identical_search_settings_for_both(harness):
    """検索手法・top_k が構成間でずれない（ずれたら差の原因が特定できない）。"""
    compare_module.compare(top_k=7, retrievers=["vector", "bm25"], gold=GOLD)

    settings = [(c["top_k"], c["retrievers"]) for c in harness["evaluate"]]
    assert settings[0] == settings[1] == (7, ["vector", "bm25"])


def test_last_run_matches_the_configured_default(harness, monkeypatch):
    """実行後のDBが設定と食い違わないよう、設定と同じ構成を最後に回す。"""
    monkeypatch.setattr(compare_module, "USE_CONTEXTUAL_CHUNKING", True)
    compare_module.compare(gold=GOLD)
    assert harness["reingest"][-1] is True

    harness["reingest"].clear()
    monkeypatch.setattr(compare_module, "USE_CONTEXTUAL_CHUNKING", False)
    compare_module.compare(gold=GOLD)
    assert harness["reingest"][-1] is False


def test_skips_embedding_without_vector_search(harness, monkeypatch):
    monkeypatch.setattr(compare_module, "resolve_retrievers", lambda n: ["bm25"])
    compare_module.compare(gold=GOLD, retrievers=["bm25"])

    assert harness["embed"] == []
    assert all(c["query_vecs"] is None for c in harness["evaluate"])


def test_no_questions_does_not_touch_the_db(harness):
    outcome = compare_module.compare(gold=[])

    assert outcome == {"gold": [], "runs": {}}
    assert harness["reingest"] == [] and harness["embed"] == []


def test_print_comparison_marks_improvements(harness, capsys):
    compare_module.print_comparison(compare_module.compare(gold=GOLD))
    out = capsys.readouterr().out

    assert "▲ 改善  2位 → 1位  Q1" in out
    assert "▲ 改善  圏外 → 1位  Q2" in out
    assert "+0.500" in out  # Hit@k の差


def test_print_comparison_without_questions(capsys):
    compare_module.print_comparison({"gold": [], "runs": {}})
    assert "評価用の質問がありません" in capsys.readouterr().out


def test_reingest_passes_contextual_and_retry_waits(monkeypatch, tmp_path):
    """取り込み直しでは contextual を明示し、レート制限の待ちも渡す。"""
    (tmp_path / "a.txt").write_text("本文", encoding="utf-8")
    (tmp_path / "b.txt").write_text("本文2", encoding="utf-8")
    captured = []

    def fake_ingest(
        source, text, project=None, topic=None, contextual=None, embed_retry_waits=None
    ):
        captured.append((source, contextual, embed_retry_waits))
        return {"chunks_created": 3, "replaced": 1}

    monkeypatch.setattr(compare_module, "ingest_text", fake_ingest)
    monkeypatch.setattr(compare_module, "load_scopes", dict)
    monkeypatch.setattr(compare_module, "RETRY_WAITS", [20])

    assert compare_module.reingest(contextual=True, seed_dir=tmp_path) == 6
    assert captured == [("a.txt", True, [20]), ("b.txt", True, [20])]


def test_reingest_keeps_document_scope(monkeypatch, tmp_path):
    """★区分を消さない★ 取り込み直しは削除→再登録なので、documents.json の
    project/topic を渡さないと `task seed` で付けた区分が NULL で上書きされる。"""
    (tmp_path / "a.txt").write_text("本文", encoding="utf-8")
    (tmp_path / "z.txt").write_text("本文", encoding="utf-8")  # マニフェスト未掲載
    captured = {}

    def fake_ingest(
        source, text, project=None, topic=None, contextual=None, embed_retry_waits=None
    ):
        captured[source] = (project, topic)
        return {"chunks_created": 1, "replaced": 1}

    monkeypatch.setattr(compare_module, "ingest_text", fake_ingest)
    monkeypatch.setattr(
        compare_module,
        "load_scopes",
        lambda: {"a.txt": {"project": "社内規程", "topic": "労務"}},
    )

    compare_module.reingest(contextual=False, seed_dir=tmp_path)

    assert captured["a.txt"] == ("社内規程", "労務")
    assert captured["z.txt"] == (None, None)  # 未掲載は元から区分なし
