"""評価質問の初期データ(fixture)読み込みのユニットテスト。

質問の正はDBで、このJSONは `--seed` で流し込む初期データ。
ファイルが壊れていると seed が黙って空になるので、形と中身をここで守る。
"""
import json

from app import eval as eval_module
from app.eval import SEED_QUESTIONS_PATH, load_seed_questions

REQUIRED_KEYS = {"question", "expected_source"}


def test_fixture_file_exists_and_is_not_empty():
    assert SEED_QUESTIONS_PATH.exists(), f"fixture が無い: {SEED_QUESTIONS_PATH}"
    assert load_seed_questions(), "fixture が空"


def test_every_question_has_a_label_and_no_duplicates():
    """seed は質問本文で重複判定するので、本文が重複していると1件しか入らない。"""
    questions = load_seed_questions()
    for item in questions:
        assert REQUIRED_KEYS <= item.keys(), f"必須キー不足: {item}"
        assert item["question"].strip()
        assert item["expected_source"].strip()

    bodies = [q["question"] for q in questions]
    assert len(bodies) == len(set(bodies)), "質問本文が重複している"


def test_expected_sources_exist_in_seed_docs():
    """正解ラベルが seed_docs に無いと、seed 直後の評価が必ず外れる。"""
    seed_docs = SEED_QUESTIONS_PATH.parent.parent / "seed_docs"
    available = {f.name for f in seed_docs.iterdir()} if seed_docs.exists() else set()
    for item in load_seed_questions():
        assert item["expected_source"] in available, item["expected_source"]


def test_missing_fixture_returns_empty_list(tmp_path):
    """ファイルが無くても例外にせず空を返す（seed がスキップされるだけ）。"""
    assert load_seed_questions(tmp_path / "nope.json") == []


def test_seed_questions_defaults_to_fixture(monkeypatch, tmp_path):
    """seed_questions() は引数なしで fixture を読み、1件ずつINSERTする。"""
    fixture = tmp_path / "eval_questions.json"
    fixture.write_text(
        json.dumps([{"question": "Q1", "expected_source": "a.txt"}]), encoding="utf-8"
    )
    monkeypatch.setattr(eval_module, "SEED_QUESTIONS_PATH", fixture)

    inserted = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            if sql.startswith("SELECT"):
                return type("R", (), {"fetchone": lambda self: None})()
            inserted.append(params)
            return None

    monkeypatch.setattr(eval_module, "get_conn", FakeConn)
    assert eval_module.seed_questions() == 1
    assert inserted[0][2] == "Q1"  # (project, topic, question, ...)
