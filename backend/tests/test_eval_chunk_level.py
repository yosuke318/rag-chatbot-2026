"""チャンク単位の正解判定（expected_text）のテスト。

ここが守っているのは1点:
  ★文書名だけを正解ラベルにすると、チャンク単位の改良を測れない★
  `就業規則.txt` は5チャンクあるので、文書名で判定していると「どのチャンクが
  1位か」が変わっても点が動かない。分割の変更・contextual retrieval・リランクは
  どれもまさにそこを動かす改良なので、この粒度では原理的に差が出ない
  （過去の比較評価が ±0.000 だった理由）。

  同時に、★既存の質問（expected_text なし）の意味を変えない★ことも守る。
  後方互換が壊れると、これまで蓄積した評価の数字と比べられなくなる。
"""
from __future__ import annotations

import json

import pytest

eval_mod = pytest.importorskip("app.eval")

_matches = eval_mod._matches
_rank_of = eval_mod._rank_of


def _hit(source: str, content: str = "…", image: bool = False) -> dict:
    return {
        "id": 1,
        "content": content,
        "source": source,
        "image_path": "images/x/0001.png" if image else None,
    }


# 同じ文書(就業規則.txt)の別チャンク。文書名だけでは見分けが付かない2つ。
OVERTIME = _hit("就業規則.txt", "第6条 時間外労働\n1日2時間を超える場合は、前日までに…")
HOLIDAY = _hit("就業規則.txt", "第9条 振替休日\n当該休日の属する月の翌月末までに取得…")


# ---------------------------------------------------------------------------
# チャンク単位の判定
# ---------------------------------------------------------------------------


def test_expected_text_rejects_a_sibling_chunk_of_the_same_document():
    """★これが本題★ 同じ文書の別チャンクを「正解」と認めない。

    ここが False にならない限り、チャンク品質の改良は数値に出ない。
    """
    assert _matches(OVERTIME, "就業規則.txt", "any", "1日2時間を超える場合")
    assert not _matches(HOLIDAY, "就業規則.txt", "any", "1日2時間を超える場合")


def test_expected_text_ignores_whitespace_and_newlines():
    """行の途中で改行が入っても一致させる（チャンクは折り返しや前置きが入る）。"""
    wrapped = _hit("就業規則.txt", "1日2時間を\n超える 場合は、前日までに…")

    assert _matches(wrapped, "就業規則.txt", "any", "1日2時間を超える場合")


def test_expected_text_survives_a_contextual_prefix():
    """contextual retrieval の前置きが付いたチャンクでも正解と認める。

    ★これが通らないと比較評価が成立しない★ contextual あり/なしの2構成で
    同じ質問集を測るので、前置きの有無で判定が変わってはいけない。
    """
    prefixed = _hit(
        "就業規則.txt",
        "この抜粋は就業規則の第2章（労働時間）に関する記述である。\n"
        "第6条 時間外労働\n1日2時間を超える場合は、前日までに所属長の事前承認を…",
    )

    assert _matches(prefixed, "就業規則.txt", "any", "1日2時間を超える場合")


def test_expected_text_still_requires_the_document_to_match():
    """語句だけで判定しない。似た言い回しは別の規程にも現れる。"""
    lookalike = _hit("育児介護休業規程.txt", "…1日2時間を超える場合は…")

    assert not _matches(lookalike, "就業規則.txt", "any", "1日2時間を超える場合")


def test_expected_text_combines_with_expected_kind():
    """図表の設問にチャンク語句を足しても、種類の条件は外れない。"""
    text_chunk = _hit("決算.pdf", "売上高は前年比110%となった")
    image_chunk = _hit("決算.pdf", "売上高は前年比110%となった", image=True)

    assert not _matches(text_chunk, "決算.pdf", "image", "前年比110%")
    assert _matches(image_chunk, "決算.pdf", "image", "前年比110%")


def test_rank_of_finds_the_labelled_chunk_further_down():
    """同じ文書の別チャンクが上位にいても、正解チャンクの順位を返す。

    ★文書単位との差がここに出る★ 文書名だけなら 0位（＝満点）に見えるが、
    実際に引きたかったチャンクは2位。改善の余地が数字に残る。
    """
    hits = [HOLIDAY, _hit("有給休暇.txt", "年次有給休暇は…"), OVERTIME]

    assert _rank_of(hits, "就業規則.txt", "any", "1日2時間を超える場合") == 2
    assert _rank_of(hits, "就業規則.txt", "any") == 0  # 文書単位なら1位扱い


def test_rank_of_returns_none_when_only_sibling_chunks_are_retrieved():
    assert _rank_of([HOLIDAY], "就業規則.txt", "any", "1日2時間を超える場合") is None


# ---------------------------------------------------------------------------
# 後方互換（expected_text が無い既存の質問）
# ---------------------------------------------------------------------------


def test_without_expected_text_the_judgement_is_unchanged():
    """既存の質問は従来どおり文書単位。どのチャンクでも正解。"""
    assert _matches(OVERTIME, "就業規則.txt", "any")
    assert _matches(HOLIDAY, "就業規則.txt", "any")
    assert _matches(OVERTIME, "就業規則.txt", "any", None)


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_expected_text_falls_back_to_document_level(blank):
    """★空文字で全問正解にしない★

    空白のみの語句をそのまま使うと、どのチャンクにも含まれる「空文字」で
    判定することになり、無条件で正解になってしまう（UIの空欄が入口）。
    """
    assert eval_mod._clean_expected_text(blank) is None
    assert _matches(HOLIDAY, "就業規則.txt", "any", blank)


def test_clean_expected_text_trims_but_keeps_the_phrase():
    assert eval_mod._clean_expected_text("  1日2時間を超える場合 ") == "1日2時間を超える場合"
    assert eval_mod._clean_expected_text(None) is None


# ---------------------------------------------------------------------------
# レポートの粒度表示（混在した質問集を読み違えないための情報）
# ---------------------------------------------------------------------------


def test_report_labels_each_question_with_its_granularity(monkeypatch):
    gold = [
        {"question": "残業の事前承認は？", "expected_source": "就業規則.txt",
         "expected_text": "1日2時間を超える場合"},
        {"question": "振替休日は？", "expected_source": "就業規則.txt"},
    ]
    monkeypatch.setattr(eval_mod, "hybrid_search", lambda q, **kw: [OVERTIME])
    monkeypatch.setattr(eval_mod, "resolve_retrievers", lambda n: ["bm25"])

    report = eval_mod.evaluate(top_k=4, retrievers=["bm25"], gold=gold)

    assert [r["match_granularity"] for r in report["results"]] == ["chunk", "document"]
    assert report["results"][0]["expected_text"] == "1日2時間を超える場合"
    assert report["results"][1]["expected_text"] is None
    # 粒度別の内訳。両方1位を引けているので、ここでは数字は同じ
    assert report["by_granularity"]["chunk"]["n"] == 1
    assert report["by_granularity"]["document"]["n"] == 1


def test_report_granularity_separates_a_chunk_level_miss(monkeypatch):
    """★平均に混ぜない★ 文書単位の設問は当たりやすいので、混ぜた Hit@k を
    「チャンクを引けた率」と読むと過大評価になる。"""
    gold = [
        {"question": "残業の事前承認は？", "expected_source": "就業規則.txt",
         "expected_text": "1日2時間を超える場合"},
        {"question": "振替休日は？", "expected_source": "就業規則.txt"},
    ]
    # 引けたのは第9条のチャンクだけ ＝ チャンク単位の設問は外れ、文書単位は当たり
    monkeypatch.setattr(eval_mod, "hybrid_search", lambda q, **kw: [HOLIDAY])
    monkeypatch.setattr(eval_mod, "resolve_retrievers", lambda n: ["bm25"])

    report = eval_mod.evaluate(top_k=4, retrievers=["bm25"], gold=gold)

    assert report["hit_at_k"] == 0.5  # 全体平均では半分当たっているように見える
    assert report["by_granularity"]["chunk"]["hit_at_k"] == 0.0
    assert report["by_granularity"]["document"]["hit_at_k"] == 1.0


def test_print_report_shows_the_granularity_of_each_judgement(capsys):
    report = {
        "n": 2,
        "top_k": 4,
        "retrievers": ["bm25"],
        "rerank": None,
        "hit_at_k": 0.5,
        "mrr": 0.5,
        "by_granularity": {
            "chunk": {"n": 1, "hit_at_k": 0.0, "mrr": 0.0},
            "document": {"n": 1, "hit_at_k": 1.0, "mrr": 1.0},
        },
        "results": [
            {
                "question": "残業の事前承認は？",
                "expected_source": "就業規則.txt",
                "expected_text": "1日2時間を超える場合",
                "match_granularity": "chunk",
                "hit": False,
                "rank": None,
                "retrieved": ["就業規則.txt"],
                "retrieved_kinds": ["text"],
                "contexts": ["…"],
            },
        ],
    }

    eval_mod._print_report(report)
    out = capsys.readouterr().out

    assert "判定の粒度別" in out
    assert "チャンク単位" in out and "文書単位" in out
    # ×なのに文書単位の設問と同じ扱いで読まれないよう、語句を行に出す
    assert "正解語句: 「1日2時間を超える場合」" in out


# ---------------------------------------------------------------------------
# fixture / seed
# ---------------------------------------------------------------------------


def test_seed_inserts_expected_text(monkeypatch, tmp_path):
    """fixture の expected_text がDBまで届く（空欄は NULL に倒す）。"""
    fixture = tmp_path / "eval_questions.json"
    fixture.write_text(
        json.dumps(
            [
                {"question": "Q1", "expected_source": "a.txt", "expected_text": " 語句 "},
                {"question": "Q2", "expected_source": "a.txt", "expected_text": ""},
                {"question": "Q3", "expected_source": "a.txt"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_mod, "SEED_QUESTIONS_PATH", fixture)
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

    monkeypatch.setattr(eval_mod, "get_conn", FakeConn)

    assert eval_mod.seed_questions() == 3
    # (project, topic, question, expected_source, expected_kind, expected_text, note)
    assert [p[5] for p in inserted] == ["語句", None, None]


def test_backfill_labels_questions_already_in_the_db(monkeypatch, tmp_path):
    """★既に seed 済みのDBを取り残さない★

    seed_questions は質問本文で重複判定するので、fixture に語句を足しただけでは
    既存行に届かない（0件追加で終わる）。NULL の行だけを埋める。
    """
    fixture = tmp_path / "eval_questions.json"
    fixture.write_text(
        json.dumps(
            [
                {"question": "Q1", "expected_source": "a.txt", "expected_text": "語句"},
                {"question": "Q2", "expected_source": "a.txt"},  # 語句なしは触らない
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_mod, "SEED_QUESTIONS_PATH", fixture)
    statements = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            statements.append((sql, params))
            return type("Cur", (), {"rowcount": 1})()

    monkeypatch.setattr(eval_mod, "get_conn", FakeConn)

    assert eval_mod.backfill_expected_text() == 1  # 語句を持つ Q1 の1件だけ
    sql, params = statements[0]
    assert len(statements) == 1, "語句の無い質問にもUPDATEを投げている"
    # ★人が貼ったラベルを上書きしない★ NULL の行だけを対象にする
    assert "expected_text IS NULL" in sql
    assert params == ("語句", "Q1")
