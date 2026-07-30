"""--corpus がデモ用と評価コーパスを取り違えないことのテスト（YOSUKE-29）。

★ここが静かに壊れると結論を間違える★
  文書だけコーパスに切り替わって質問がデモ用のまま、あるいは文書のディレクトリだけ
  差し替わって区分のマニフェストが seed_docs のまま——といったずれは、例外にならず
  「全問圏外」や「質問が見つかりません」という実測値と見分けの付かない結果になる。
  3つ（文書・区分・質問集）が必ず揃って切り替わることを固定する。

DB・埋め込みAPI・Claude は触らずモックする。
"""
from __future__ import annotations

import pytest

seed_mod = pytest.importorskip("app.seed")
eval_mod = pytest.importorskip("app.eval")
compare_mod = pytest.importorskip("app.compare")


@pytest.fixture
def ingested(monkeypatch):
    """app.seed の取り込みを差し替え、(source, project, topic) を記録する。"""
    calls: list[tuple[str, str | None, str | None]] = []

    def fake_ingest(source, text, project=None, topic=None, **kw):
        calls.append((source, project, topic))
        return {"chunks_created": 1, "replaced": 0, "skipped": False}

    monkeypatch.setattr(seed_mod, "ingest_text", fake_ingest)
    monkeypatch.setattr(seed_mod, "init_db", lambda: None)
    return calls


# ---------------------------------------------------------------------------
# app.seed
# ---------------------------------------------------------------------------


def test_seed_defaults_to_the_demo_documents(ingested, monkeypatch):
    monkeypatch.setattr("sys.argv", ["app.seed"])

    seed_mod.main()

    sources = [c[0] for c in ingested]
    assert sources, "seed_docs から1件も取り込んでいない"
    assert "就業規則（本社）.txt" not in sources  # コーパス側の文書


def test_seed_corpus_ingests_the_corpus_with_its_own_scopes(ingested, monkeypatch):
    """★区分も一緒に切り替わること★

    project が付かないと、--corpus の評価が project で絞った時点で0件になる。
    """
    monkeypatch.setattr("sys.argv", ["app.seed", "--corpus"])

    seed_mod.main()

    assert len(ingested) >= 20
    projects = {c[1] for c in ingested}
    assert projects == {seed_mod.CORPUS_PROJECT}
    assert all(c[2] for c in ingested), "topic が付いていない文書がある"


def test_seed_accepts_a_single_file_name(ingested, monkeypatch):
    """既存の使い方（ファイル名を並べる）を壊していない。"""
    monkeypatch.setattr("sys.argv", ["app.seed", "就業規則.txt"])

    seed_mod.main()

    assert [c[0] for c in ingested] == ["就業規則.txt"]


# ---------------------------------------------------------------------------
# app.eval --seed
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(monkeypatch):
    """eval の質問投入を差し替え、渡された fixture を記録する。"""
    seen: dict[str, list] = {}

    def fake_seed_questions(questions=None):
        seen["seeded"] = questions
        return len(questions or [])

    def fake_backfill(questions=None):
        seen["backfilled"] = questions
        return 0

    monkeypatch.setattr(eval_mod, "seed_questions", fake_seed_questions)
    monkeypatch.setattr(eval_mod, "backfill_expected_text", fake_backfill)
    monkeypatch.setattr(eval_mod, "load_questions", lambda **kw: [])
    return seen


def test_eval_seed_corpus_loads_the_corpus_questions(seeded, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["app.eval", "--seed", "--corpus"])

    eval_mod.main()

    assert len(seeded["seeded"]) >= 30
    # ★投入とラベルの後埋めが同じ質問集を見ること★
    # 別々だと、コーパスを投入したのにデモ用の語句で backfill する事故が起きる。
    assert seeded["backfilled"] == seeded["seeded"]
    assert all(q.get("expected_text") for q in seeded["seeded"])


def test_eval_seed_defaults_to_the_demo_fixture(seeded, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["app.eval", "--seed"])

    eval_mod.main()

    assert seeded["seeded"] == eval_mod.load_seed_questions()


# ---------------------------------------------------------------------------
# app.eval / app.compare の絞り込み
# ---------------------------------------------------------------------------


def test_eval_corpus_filters_by_the_corpus_project(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("sys.argv", ["app.eval", "--corpus", "--retrievers", "bm25"])
    monkeypatch.setattr(
        eval_mod, "load_questions", lambda project=None, topic=None: captured.setdefault(
            "scope", (project, topic)
        ) and []
    )

    eval_mod.main()

    assert captured["scope"] == (seed_mod.CORPUS_PROJECT, None)


def test_eval_explicit_project_wins_over_corpus(monkeypatch):
    """--project を明示したらそちらを尊重する（コーパスの一部だけ見たい場合）。"""
    captured: dict = {}
    monkeypatch.setattr(
        "sys.argv", ["app.eval", "--corpus", "--project", "社内規程"]
    )
    monkeypatch.setattr(
        eval_mod, "load_questions", lambda project=None, topic=None: captured.setdefault(
            "scope", (project, topic)
        ) and []
    )

    eval_mod.main()

    assert captured["scope"] == ("社内規程", None)


def test_compare_corpus_switches_documents_scopes_and_questions(monkeypatch):
    captured: dict = {}

    def fake_compare(top_k=4, retrievers=None, gold=None, seed_dir=None, scopes_path=None):
        captured.update(
            {"gold": gold, "seed_dir": seed_dir, "scopes_path": scopes_path}
        )
        return {"gold": gold, "runs": {}}

    monkeypatch.setattr("sys.argv", ["app.compare", "--corpus"])
    monkeypatch.setattr(compare_mod, "init_db", lambda: None)
    monkeypatch.setattr(compare_mod, "compare", fake_compare)
    monkeypatch.setattr(
        compare_mod,
        "load_questions",
        lambda project=None, topic=None: (
            captured.setdefault("scope", (project, topic)),
            [{"question": "Q", "expected_source": "a.txt"}],
        )[1],
    )

    compare_mod.main()

    assert captured["scope"] == (seed_mod.CORPUS_PROJECT, None)
    assert captured["seed_dir"] == seed_mod.CORPUS_DIR
    assert captured["scopes_path"] == seed_mod.CORPUS_SCOPES_PATH


def test_compare_defaults_to_the_demo_documents(monkeypatch):
    captured: dict = {}

    def fake_compare(top_k=4, retrievers=None, gold=None, seed_dir=None, scopes_path=None):
        captured.update({"seed_dir": seed_dir, "scopes_path": scopes_path})
        return {"gold": gold, "runs": {}}

    monkeypatch.setattr("sys.argv", ["app.compare"])
    monkeypatch.setattr(compare_mod, "init_db", lambda: None)
    monkeypatch.setattr(compare_mod, "compare", fake_compare)
    monkeypatch.setattr(
        compare_mod, "load_questions", lambda project=None, topic=None: [{"question": "Q"}]
    )

    compare_mod.main()

    # None = reingest 側の既定（seed_docs と seed_data/documents.json）
    assert captured == {"seed_dir": None, "scopes_path": None}


def test_compare_stops_when_the_corpus_questions_are_missing(monkeypatch, capsys):
    """★空の質問集で走らせない★

    投入を忘れたまま走らせると、数百チャンクを2回取り込んだ末に「N=0」が出る
    （時間とAPIの費用を払って何も分からない）。先に止めて手順を示す。
    """
    monkeypatch.setattr("sys.argv", ["app.compare", "--corpus"])
    monkeypatch.setattr(compare_mod, "init_db", lambda: None)
    monkeypatch.setattr(compare_mod, "load_questions", lambda **kw: [])
    monkeypatch.setattr(
        compare_mod, "compare", lambda **kw: pytest.fail("質問が0件なのに比較を開始した")
    )

    compare_mod.main()

    out = capsys.readouterr().out
    assert "python -m app.eval --seed --corpus" in out
