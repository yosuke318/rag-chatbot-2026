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


def test_expected_texts_land_in_exactly_one_chunk():
    """★チャンク単位のラベルが機能する状態を保つ★

    expected_text は「正解チャンクに必ず含まれる語句」なので、次の2つが要る:
      - 1つ以上のチャンクに含まれる  … 0件なら、正しく引けても必ず×になる
      - 2つ以上に含まれない          … 複数に散ると、どれを引いても正解になり
                                       文書単位の判定に戻ってしまう

    分割ロジック(app.chunking)を変えたときにここが落ちる ＝ ラベルの貼り直しが
    必要だというサイン。語句で持っているので、IDと違って多くの変更には耐える。
    """
    from app.chunking import chunk_text

    seed_docs = SEED_QUESTIONS_PATH.parent.parent / "seed_docs"
    squash = lambda s: "".join(s.split())  # noqa: E731  改行や折り返しを無視して比べる
    chunks = {
        f.name: [squash(c) for c in chunk_text(f.read_text(encoding="utf-8"))]
        for f in seed_docs.iterdir()
        if f.suffix == ".txt"
    }

    labelled = [q for q in load_seed_questions() if q.get("expected_text")]
    assert labelled, "チャンク単位のラベルを持つ質問が1件も無い（文書単位のまま）"
    for item in labelled:
        found = [
            c for c in chunks[item["expected_source"]] if squash(item["expected_text"]) in c
        ]
        assert len(found) == 1, (
            f"{item['expected_source']} で「{item['expected_text']}」が "
            f"{len(found)} チャンクに一致（1でなければならない）"
        )


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
