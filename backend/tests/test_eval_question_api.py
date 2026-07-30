"""/eval-questions が expected_text（チャンク単位の正解ラベル）を通すテスト。

ラベルはUIとfixtureの2経路から入る。API境界で落ちると、UIから登録した質問だけ
黙って文書単位の判定に戻る（数字は出るので気付きにくい）ので、ここで固定する。
DBには繋がず、app.main.get_conn を差し替えて発行SQLとパラメータを見る。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


class _FakeConn:
    """INSERT のパラメータを記録し、SELECT には行を返す最小のコネクション。"""

    def __init__(self, calls: list, rows: list):
        self.calls = calls
        self.rows = rows

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        rows = self.rows
        return type(
            "R",
            (),
            {
                "fetchone": lambda self: (42,),
                "fetchall": lambda self: rows,
            },
        )()


def _post(client, body: dict) -> tuple[dict, list]:
    calls: list = []
    with patch.object(main_module, "get_conn", _FakeConn(calls, [])):
        res = client.post("/eval-questions", json=body)
    assert res.status_code == 200, res.text
    return res.json(), calls


def test_post_stores_and_returns_expected_text(client):
    body, calls = _post(
        client,
        {
            "question": "残業で事前承認が必要になるのは何時間から？",
            "expected_source": "就業規則.txt",
            "expected_text": "1日2時間を超える場合",
        },
    )

    sql, params = calls[0]
    assert "expected_text" in sql
    assert "1日2時間を超える場合" in params
    assert body["expected_text"] == "1日2時間を超える場合"


def test_post_without_expected_text_stays_document_level(client):
    """省略できる（＝既存の登録経路をそのまま使える）。"""
    body, calls = _post(
        client,
        {"question": "有給は何日？", "expected_source": "有給休暇.txt"},
    )

    assert body["expected_text"] is None
    assert None in calls[0][1]


@pytest.mark.parametrize("blank", ["", "   "])
def test_post_normalizes_a_blank_expected_text_to_null(client, blank):
    """★空文字を保存しない★

    空文字はどのチャンクにも含まれるので、そのまま判定に使うと全問正解になる。
    UIの空欄がそのまま届く経路なので、API境界で NULL に倒す。
    """
    body, _ = _post(
        client,
        {
            "question": "有給は何日？",
            "expected_source": "有給休暇.txt",
            "expected_text": blank,
        },
    )

    assert body["expected_text"] is None


def test_get_returns_expected_text(client):
    # (id, question, expected_source, project, topic, note, expected_kind, expected_text)
    row = (1, "残業は？", "就業規則.txt", None, None, "第6条", "any", "1日2時間を超える場合")
    with patch.object(main_module, "get_conn", _FakeConn([], [row])):
        res = client.get("/eval-questions")

    assert res.status_code == 200
    assert res.json()["questions"][0]["expected_text"] == "1日2時間を超える場合"
