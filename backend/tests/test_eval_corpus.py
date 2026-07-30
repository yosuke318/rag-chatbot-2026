"""評価専用コーパス（eval_corpus）の健全性テスト（YOSUKE-29）。

このコーパスは「改善の余地が数字に残る評価セット」であることに価値がある。
壊れ方が静かなので、次の3つをここで固定する:

  1. 正解ラベル(expected_text)が★ちょうど1チャンク★に一致すること
     0件なら正しく引けても必ず×になり、2件以上なら「どれを引いても正解」＝
     文書単位の判定に戻る（YOSUKE-28 で潰した問題がここで復活する）。
  2. 文書とマニフェスト(documents.json)と質問集の project が揃っていること
     区分がずれると、評価が project で絞った時点で対象0件になる。
  3. 規模が保たれていること（数百チャンク）。小さくすると Hit@k が天井に戻る。

分割ロジック(app.chunking)を変えるとここが落ちる ＝ ラベルの貼り直しが必要だという
サイン。語句で持っているので、チャンクIDと違って多くの変更には耐える。
"""
from __future__ import annotations

import json

import pytest

eval_mod = pytest.importorskip("app.eval")
seed_mod = pytest.importorskip("app.seed")

from app.chunking import chunk_text  # noqa: E402

CORPUS_DIR = seed_mod.CORPUS_DIR
QUESTIONS_PATH = eval_mod.CORPUS_QUESTIONS_PATH
SCOPES_PATH = seed_mod.CORPUS_SCOPES_PATH

# 「数百チャンク」の下限。これを割ると上位4件に入るのが簡単になり、
# 指標が飽和して改良の効果が測れなくなる（このコーパスを作った理由が消える）。
MIN_CHUNKS = 200
# 検定が意味を持つ最低限の設問数（eval.MIN_QUESTIONS_FOR_TEST）に対して余裕を持たせる
MIN_QUESTIONS = 30


def _squash(text: str) -> str:
    return "".join(text.split())


@pytest.fixture(scope="module")
def docs() -> dict[str, list[str]]:
    return {
        path.name: [_squash(c) for c in chunk_text(path.read_text(encoding="utf-8"))]
        for path in CORPUS_DIR.glob("*.txt")
    }


@pytest.fixture(scope="module")
def questions() -> list[dict]:
    return eval_mod.load_seed_questions(QUESTIONS_PATH)


@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    return json.loads(SCOPES_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 規模
# ---------------------------------------------------------------------------


def test_corpus_is_large_enough_not_to_saturate(docs):
    total = sum(len(chunks) for chunks in docs.values())

    assert len(docs) >= 20, f"文書が少なすぎる: {len(docs)}"
    assert total >= MIN_CHUNKS, f"チャンクが少なすぎる: {total} < {MIN_CHUNKS}"


def test_enough_questions_for_a_meaningful_test(questions):
    assert len(questions) >= MIN_QUESTIONS
    assert len(questions) > eval_mod.MIN_QUESTIONS_FOR_TEST


def test_question_bodies_are_unique(questions):
    """seed は質問本文で重複判定するので、重複していると1件しか入らない。"""
    bodies = [q["question"] for q in questions]

    assert len(bodies) == len(set(bodies))


# ---------------------------------------------------------------------------
# ラベルの粒度
# ---------------------------------------------------------------------------


def test_every_question_is_labelled_at_chunk_level(questions):
    """★このコーパスの全問がチャンク単位★ 文書単位の設問を混ぜない。

    混ざると、当たりやすい設問が平均を押し上げて飽和が戻る。
    """
    for item in questions:
        assert item.get("expected_text"), f"expected_text が無い: {item['question']}"


def test_expected_text_matches_exactly_one_chunk(docs, questions):
    for item in questions:
        source = item["expected_source"]
        assert source in docs, f"コーパスに無い文書を指している: {source}"
        found = [c for c in docs[source] if _squash(item["expected_text"]) in c]
        assert len(found) == 1, (
            f"{source} で「{item['expected_text']}」が {len(found)} チャンクに一致"
            "（1でなければならない）"
        )


def test_questions_avoid_naming_the_article(questions):
    """条文名を質問文に書かない（書くと字面検索だけで当たり、検索の実力を測れない）。"""
    for item in questions:
        assert "第" not in item["question"] or "条" not in item["question"], item[
            "question"
        ]


# ---------------------------------------------------------------------------
# 区分（project / topic）の整合
# ---------------------------------------------------------------------------


def test_manifest_covers_exactly_the_corpus_files(docs, manifest):
    """マニフェストから漏れた文書は区分なしで入り、project で絞った評価から外れる。"""
    listed = {item["source"] for item in manifest}

    assert listed == set(docs)


def test_manifest_and_questions_share_one_project(manifest, questions):
    """★文書と質問の project が一致していること★

    ずれると `--corpus` の評価が「質問が見つかりません」または全問圏外になる。
    """
    assert {item["project"] for item in manifest} == {seed_mod.CORPUS_PROJECT}
    assert {q.get("project") for q in questions} == {seed_mod.CORPUS_PROJECT}


def test_questions_are_not_narrowed_by_topic(questions):
    """★質問に topic を付けない★

    付けると検索対象がそのトピックの文書だけになり、他トピックの紛らわしい文書
    （語彙の似た規程）が対象から外れて、取り違えを測れなくなる。
    """
    assert all(q.get("topic") is None for q in questions)


def test_every_document_has_a_topic(manifest):
    """文書側には topic を付ける（区分ごとの評価もできるようにするため）。"""
    assert all(item.get("topic") for item in manifest)


# ---------------------------------------------------------------------------
# 意図した紛らわしさが残っているか
# ---------------------------------------------------------------------------


def test_corpus_contains_lexically_similar_document_pairs(docs):
    """語彙の似た文書が複数あること＝取り違えが起きうる状態。

    ここが崩れると、質問が「その文書しか候補にない」ので簡単になる。
    """
    for a, b in (
        ("就業規則（本社）.txt", "就業規則（工場）.txt"),
        ("育児休業規程.txt", "介護休業規程.txt"),
        ("国内出張旅費規程.txt", "海外出張旅費規程.txt"),
        ("ハラスメント防止規程.txt", "内部通報規程.txt"),
    ):
        assert a in docs and b in docs, f"紛らわしさを担う対が欠けている: {a} / {b}"


def test_same_phrase_appears_in_more_than_one_document(docs):
    """★文書名まで見ないと正解にならない語句がある★

    「年5日まで休暇を取得できる」は育児と介護の両方にある。expected_text だけで
    判定していると誤って正解と認めるので、_matches は文書名の一致も要求している。
    """
    phrase = _squash("年5日まで休暇を取得できる")
    hits = [name for name, chunks in docs.items() if any(phrase in c for c in chunks)]

    assert len(hits) >= 2, hits
