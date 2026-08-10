"""👎の理由を聞くテスト（PATCH /feedback/{id} と GET /feedback/reasons）。

理由が無い👎は「調べる場所を絞る」役に立たない一方、理由を必須にすると
★押しただけで去った人の👎が丸ごと消える★。この2つを両立させる作りを固定する:

  1. 👎 は押した時点で記録され、理由は後から足せる（別の操作）
  2. 理由を選ばなくても👎は残る（従来どおり）
  3. 理由は決まった選択肢だけ（数えるために持つので、表記ゆれを入れない）
  4. 選択肢の文言は1か所（サーバ）が正で、画面はそれを取りに来る

DBは触らず get_conn を差し替えて発行SQLとパラメータを見る。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import feedback  # noqa: E402
from app import main as main_module  # noqa: E402


class FakeConn:
    """UPDATE の RETURNING に更新後の行を返す偽コネクション。row=None で「該当なし」。"""

    def __init__(self, calls: list, row=(3, -1, "情報が古い", None)):
        self.calls = calls
        self.row = row

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        outer = self
        return type("R", (), {"fetchone": lambda _s: outer.row})()


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


# --- 選択肢 -------------------------------------------------------------------


def test_reasons_come_from_the_server(client):
    """画面に文言を焼かず、記録される値と同じものを配る。"""
    res = client.get("/feedback/reasons")
    assert res.status_code == 200
    assert res.json() == {"reasons": feedback.REASONS}
    # 1クリックで選べる短い並びであること（増やしすぎると読まずに先頭が押される）
    assert 2 <= len(feedback.REASONS) <= 6


# --- 後から理由を足す ----------------------------------------------------------


def _patch(client, body: dict, feedback_id: int = 3, **conn_kw):
    calls: list = []
    with patch.object(feedback, "get_conn", FakeConn(calls, **conn_kw)):
        res = client.patch(f"/feedback/{feedback_id}", json=body)
    return res, calls


def test_reason_is_attached_to_the_recorded_feedback(client):
    """👎は既に記録済みで、ここで足すのは理由だけ（評価は作り直さない）。"""
    res, calls = _patch(client, {"reason": "情報が古い"})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "id": 3,
        "rating": -1,
        "reason": "情報が古い",
        "comment": None,
    }

    sql, params = calls[0]
    assert sql.startswith("UPDATE feedback SET")
    assert params == ["情報が古い", 3]
    # 評価そのものは書き換えない（いつの時点の評価かが失われる）
    assert "rating =" not in sql


def test_untouched_fields_are_left_alone(client):
    """理由だけ選んで自由記述は書かない、が普通。片方の指定でもう片方を消さない。"""
    _, calls = _patch(client, {"reason": "読みにくい"})
    sql, _ = calls[0]
    assert "comment =" not in sql

    _, calls = _patch(client, {"comment": "第5条の日数が違う"})
    sql, params = calls[0]
    assert "reason =" not in sql
    assert params == ["第5条の日数が違う", 3]


def test_comment_can_be_cleared_with_an_empty_string(client):
    """null は「変更しない」なので、消すのは空文字。両者を同じ扱いにしない。"""
    _, calls = _patch(client, {"comment": ""})
    sql, params = calls[0]
    assert "comment =" in sql
    assert params == ["", 3]


def test_unknown_reason_is_rejected(client):
    """自由な文字列を入れない。表記ゆれが混ざると理由を数えられなくなる。"""
    res, calls = _patch(client, {"reason": "なんとなく"})
    assert res.status_code == 400
    assert res.json()["error"] == "unknown_reason"
    # 何が選べるかを画面にそのまま出せること
    assert feedback.REASONS[0] in res.json()["hint"]
    assert calls == []


def test_empty_update_is_rejected(client):
    """理由も自由記述も無い更新は書き換える先が無い（成功と紛らわしいので400）。"""
    res, calls = _patch(client, {})
    assert res.status_code == 400
    assert res.json()["error"] == "empty_feedback_update"
    assert calls == []


def test_blank_reason_is_treated_as_unspecified(client):
    """空白だけの理由は「選ばなかった」。空文字のまま記録すると数え漏れる。"""
    res, calls = _patch(client, {"reason": "   "})
    assert res.status_code == 400
    assert res.json()["error"] == "empty_feedback_update"
    assert calls == []


def test_missing_feedback_is_404(client):
    """存在しないIDへの更新は 404（黙って成功にすると理由が消えたことに気づけない）。"""
    res, _ = _patch(client, {"reason": "情報が古い"}, feedback_id=999, row=None)
    assert res.status_code == 404
    assert res.json()["error"] == "feedback_not_found"


# --- 記録側 -------------------------------------------------------------------


def test_reason_can_also_be_sent_when_recording(client):
    """最初から理由が分かっている呼び出しのために、記録時にも受け取れる。"""
    calls: list = []
    with patch.object(main_module, "get_conn", FakeConn(calls, row=(9,))):
        res = client.post(
            "/feedback",
            json={
                "question": "有給は?",
                "answer": "10日です。",
                "rating": -1,
                "reason": "情報が古い",
            },
        )
    assert res.status_code == 200, res.text

    sql, params = calls[0]
    names = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert dict(zip(names, params))["reason"] == "情報が古い"


def test_unknown_reason_is_rejected_when_recording(client):
    """記録時も選択肢だけ。ここを素通しにすると PATCH 側の検査に意味が無くなる。"""
    calls: list = []
    with patch.object(main_module, "get_conn", FakeConn(calls)):
        res = client.post(
            "/feedback",
            json={
                "question": "有給は?",
                "answer": "10日です。",
                "rating": -1,
                "reason": "なんとなく",
            },
        )
    assert res.status_code == 400
    assert res.json()["error"] == "unknown_reason"
    assert calls == []
