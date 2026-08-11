"""👎を評価用質問に昇格させるテスト（POST /feedback/{id}/promote）。

貯めた👎は、評価データセットに入って初めて「次に同じことが起きたら気づける」に
変わる。ただし ★入れ方を間違えると指標そのものが壊れる★ ので、ここで固定するのは
主に「入れてはいけないものが入らないこと」:

  1. 正解ラベル(expected_source)の無い質問は入らない（直接登録と同じ検査）
  2. 同じフィードバックからは1件しか作れない（二重に数えられない）
  3. 昇格と印付けが不可分（評価用質問だけ増えて印が付かない、が起きない）
  4. 送られてきた値をそのまま使う（フィードバックの出典を正解に流用しない）
  5. 昇格できない呼び出しは何も書かない（区分マスタも増やさない）

DBは触らず get_conn を差し替えて発行SQLとパラメータを見る
（test_feedback_list.py / test_feedback_reason.py と同じやり方）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import feedback  # noqa: E402
from app import main as main_module  # noqa: E402

# 昇格の最小の中身。question と expected_source は評価の必須要素。
BODY = {
    "question": "残業で事前承認が必要になるのは何時間から？",
    "expected_source": "就業規則.txt",
}


# 昇格が通るときのSQLの並び。行の状態を見てから書く。
#   1回目 … 昇格できる行か（app.feedback.promotion_state）。(None,)=行はある・未昇格
#   2回目 … 昇格そのもの（登録＋印付けの1文）。返るのは出来た質問のID
PROMOTABLE = ((None,), (42,))


class FakeConn:
    """発行SQLを記録し、rows を execute の呼ばれた順に返す偽コネクション。

    行の状態を見る SELECT と昇格の1文を撃ち分けるので、呼び出しごとに違う答えを
    返せるようにしてある（rows を撃ち切った後は None＝該当なし）。
    """

    def __init__(self, calls: list, rows=PROMOTABLE):
        self.calls = calls
        self.rows = list(rows)

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        row = self.rows[len(self.calls) - 1] if len(self.calls) <= len(self.rows) else None
        return type("R", (), {"fetchone": lambda _s: row})()


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def _promote(client, body: dict, feedback_id: int = 3, rows=PROMOTABLE):
    """区分のマスタ登録は別の関心事なので固定idを返す（test_saved_questions と同じ）。

    呼ばれたかどうかは見たいので、素の lambda ではなく記録する偽物を渡す
    （昇格できない呼び出しでマスタを増やさないことを確かめるため）。
    """
    calls: list = []
    registered: list = []

    def fake_register(project, topic):
        registered.append((project, topic))
        return (7, 8)

    with patch.object(feedback, "get_conn", FakeConn(calls, rows)):
        with patch.object(main_module.scopes, "register", fake_register):
            res = client.post(f"/feedback/{feedback_id}/promote", json=body)
    res.registered = registered  # type: ignore[attr-defined]
    return res, calls


def _promote_sql(calls: list) -> tuple[str, list]:
    """昇格そのものの1文（状態を見る SELECT ではない方）。"""
    return next(c for c in calls if "INSERT INTO eval_questions" in c[0])


# --- 正常系 -------------------------------------------------------------------


def test_promote_creates_the_eval_question_and_marks_the_feedback(client):
    """評価用質問を作り、元の👎に印を付けること。両方が1回のSQLで起きる。

    ★2文に分けない★
      印を付ける前に落ちると「質問だけ増えて元の👎は未昇格」が残り、次に押すと
      同じ質問がもう1件できる。CTEにまとめて不可分にしてある。
    """
    res, calls = _promote(client, BODY)

    assert res.status_code == 200, res.text
    sql, params = _promote_sql(calls)
    assert "UPDATE feedback" in sql and "promoted_eval_question_id" in sql
    # 登録と印付けが同じ1文に入っていること（分かれていれば別のSQLになる）
    assert sum("INSERT INTO eval_questions" in c[0] for c in calls) == 1
    assert sum("UPDATE feedback" in c[0] for c in calls) == 1
    # 印を付ける相手は「未昇格の行」だけ。ここが抜けると二重に昇格できる。
    assert "promoted_eval_question_id IS NULL" in sql
    assert params[0] == 3  # 昇格元のフィードバックID
    assert "就業規則.txt" in params


def test_promote_returns_the_new_question_with_its_source(client):
    """出来た質問と昇格元を返すこと（画面はこれだけで行に印を付けられる）。"""
    res, _ = _promote(client, {**BODY, "note": "第6条"})

    assert res.json() == {
        "id": 42,
        "feedback_id": 3,
        "question": BODY["question"],
        "expected_source": "就業規則.txt",
        # 省略時は文書単位の判定（直接登録したときと同じ既定）
        "expected_kind": "any",
        "expected_text": None,
        "project": None,
        "topic": None,
        "note": "第6条",
    }


def test_promote_stores_the_scope_as_master_ids(client):
    """区分は名前ではなくマスタの id で入ること（他の書き込み側と同じ）。"""
    _, calls = _promote(client, {**BODY, "project": "社内規程", "topic": "労務"})

    _, params = _promote_sql(calls)
    assert params[1] == 7 and params[2] == 8


def test_promote_keeps_chunk_level_labels(client):
    """チャンク単位の正解ラベルも通ること。

    ★👎の昇格でこそ効く★
      「その文書は引けていたが、当たっていないチャンクを渡していた」という👎は、
      文書単位のラベルにすると最初から正解扱いになり、直したかどうかが数字に出ない。
    """
    res, calls = _promote(
        client,
        {**BODY, "expected_kind": "image", "expected_text": "1日2時間を超える場合"},
    )

    _, params = _promote_sql(calls)
    assert "image" in params and "1日2時間を超える場合" in params
    assert res.json()["expected_kind"] == "image"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_expected_text_falls_back_to_document_level(client, blank):
    """空欄は NULL に倒す。空文字はどのチャンクにも含まれるので全問正解になる。"""
    res, _ = _promote(client, {**BODY, "expected_text": blank})

    assert res.json()["expected_text"] is None


# --- 入れてはいけないものを入れない --------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"question": "残業は？", "expected_source": ""},
        {"question": "残業は？", "expected_source": "   "},
        {"question": "", "expected_source": "就業規則.txt"},
    ],
)
def test_promotion_without_a_correct_answer_is_rejected(client, body):
    """正解の無い質問は昇格させない。★ここが緩いと指標が壊れる★

    eval_questions は expected_source NOT NULL（正解必須）の設計。そもそも文書に
    答えが無い質問を混ぜると、引けなくて当然のものを不正解として数え続けることに
    なり、Hit@k / MRR がその分だけ下がったまま戻らない。
    """
    res, calls = _promote(client, body)

    assert res.status_code == 400
    assert calls == [], "弾いた入力でDBを触らない"


def test_unknown_expected_kind_is_rejected(client):
    """正解種別は決まった値だけ（直接登録と同じ検査を通ること）。"""
    res, calls = _promote(client, {**BODY, "expected_kind": "figure"})

    assert res.status_code == 400
    assert calls == []


def test_second_promotion_is_refused_and_points_at_the_existing_question(client):
    """2度目は 409。既に出来ている質問のIDを添える。

    ★「見つかりません」で片付けない★
      2度押ししただけの人が、存在するIDを疑うことになる。同じ👎から2件作ると、
      その1問だけが評価データセットで二重に数えられる。
    """
    res, calls = _promote(client, BODY, rows=((55,),))

    assert res.status_code == 409
    assert res.json()["error"] == "already_promoted"
    assert "55" in res.json()["detail"]
    # 状態を見た時点で分かるので、書き込みには進まない
    assert len(calls) == 1


def test_promoting_a_missing_feedback_is_404(client):
    """行が無いときは404（昇格済みと区別する）。"""
    res, calls = _promote(client, BODY, rows=(None,))

    assert res.status_code == 404
    assert res.json()["error"] == "feedback_not_found"
    assert len(calls) == 1


def test_a_promotion_that_loses_a_race_is_still_refused(client):
    """状態を見た後に他から昇格されても、二重にはならず 409 になること。

    ★事前の確認は書き込みを止める役ではない★
      1回に抑えているのは昇格SQLの条件（未昇格の行だけ）。事前の確認は、
      書くものが無い呼び出しでマスタを触らせないためのもので、見てから書くまでの
      間に状態が変わる余地は残る。そこを通り抜けても結論が変わらないことを固定する。
    """
    # 見たときは未昇格 → 昇格SQLは0件（他が先に入れた）→ 引き直すと昇格済み
    res, calls = _promote(client, BODY, rows=((None,), None, (55,)))

    assert res.status_code == 409
    assert "55" in res.json()["detail"]
    assert len(calls) == 3


def test_a_failed_promotion_does_not_create_scopes(client):
    """昇格できない呼び出しで区分マスタを増やさないこと。

    ★scopes.register は渡された名前を作る★
      順番を逆にすると、存在しないIDを叩かれただけで区分が増える（昇格は失敗
      するのに、選択肢にだけ知らない名前が残る）。
    """
    res, _ = _promote(
        client,
        {**BODY, "project": "打ち間違えた区分", "topic": "労務"},
        rows=(None,),
    )

    assert res.status_code == 404
    assert res.registered == [], "書くものが無い呼び出しではマスタを触らない"


def test_a_successful_promotion_registers_the_scope(client):
    """通る呼び出しでは今までどおりマスタへ写すこと（確認を足して壊していない）。"""
    res, _ = _promote(client, {**BODY, "project": "社内規程", "topic": "労務"})

    assert res.status_code == 200
    assert res.registered == [("社内規程", "労務")]


def test_promotion_does_not_reuse_the_feedbacks_own_sources(client):
    """正解はリクエストの値をそのまま使い、元の行から埋め直さないこと。

    👎の出典は「そのとき挙がった文書」で、正解は「本当はどれを引くべきだったか」。
    その2つが違うことこそ👎の中身なので、サーバが元の行を読んで埋めてはいけない
    （＝間違った出典が正解ラベルとして固定される）。
    """
    _, calls = _promote(client, {**BODY, "expected_source": "有給休暇.txt"})

    sql, params = _promote_sql(calls)
    assert "有給休暇.txt" in params
    # 元の行から値を読み出すSELECTは撃たない（1文に閉じている）
    assert "SELECT f.sources" not in sql
