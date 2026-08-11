"""👎の行から原因を調べに行くための口のテスト（GET /chunks・GET /conversations/{id}）。

👎 は「何が起きたか」を教えてくれない。検索が外したのか、生成が外したのか、
そもそも文書に答えが無かったのかを分けるには、★そのとき渡したチャンク★と
★どういう流れで聞かれたか★を後から読めることが要る。ここで固定するのは:

  1. チャンクは記録した並び（＝順位）のまま返る（並べ替えると順位が消える）
  2. 一部のチャンクが消えていても、残りは読める（文書を消しても評価は残る作り）
  3. 会話は途中を切らずに全文返る（生成に載せる直近N件とは別物）

DBは触らず get_conn を差し替える。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import conversations, retrieval  # noqa: E402
from app import main as main_module  # noqa: E402

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)


class FakeConn:
    """fetchall に固定の行、fetchone に one を返す偽コネクション。"""

    def __init__(self, calls: list, rows=None, one=(1,)):
        self.calls = calls
        self.rows = rows or []
        self.one = one

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        outer = self
        return type(
            "R",
            (),
            {"fetchone": lambda _s: outer.one, "fetchall": lambda _s: outer.rows},
        )()


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


# --- そのとき渡したチャンク ----------------------------------------------------

# DBは id 順で返す。記録した並び（203 が1位、101 が2位）とはわざとずらしてある。
CHUNK_ROWS = [
    (101, "第5条 年次有給休暇…", "有給休暇.txt", 3, None, None),
    (203, "第30条 経費は…", "経費精算.txt", 7, None, "第4章 経費"),
]


def test_chunks_keep_the_recorded_order(client):
    """★並びがそのまま順位★ ID順に並べ替えると「何位に居たか」が消える。"""
    calls: list = []
    with patch.object(retrieval, "get_conn", FakeConn(calls, rows=CHUNK_ROWS)):
        res = client.get("/chunks?ids=203,101")
    assert res.status_code == 200, res.text

    chunks = res.json()["chunks"]
    assert [c["id"] for c in chunks] == [203, 101]
    assert chunks[0]["source"] == "経費精算.txt"
    assert chunks[0]["chunk_index"] == 7
    assert chunks[0]["context"] == "第4章 経費"
    assert chunks[1]["context"] is None


def test_missing_chunks_are_skipped(client):
    """消えたチャンクがあっても残りは読める（404 にすると調査ごと止まる）。"""
    calls: list = []
    with patch.object(retrieval, "get_conn", FakeConn(calls, rows=CHUNK_ROWS)):
        res = client.get("/chunks?ids=203,999,101")
    assert res.status_code == 200
    assert [c["id"] for c in res.json()["chunks"]] == [203, 101]


def test_empty_ids_do_not_hit_the_db(client):
    calls: list = []
    with patch.object(retrieval, "get_conn", FakeConn(calls)):
        res = client.get("/chunks?ids=")
    assert res.status_code == 200
    assert res.json() == {"chunks": []}
    assert calls == []


def test_non_numeric_ids_are_rejected(client):
    """数値以外は 400。空の結果で返すと「チャンクが消えた」と読めてしまう。"""
    calls: list = []
    with patch.object(retrieval, "get_conn", FakeConn(calls)):
        res = client.get("/chunks?ids=101,abc")
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_chunk_ids"
    assert calls == []


# --- 元の会話 -----------------------------------------------------------------

MESSAGE_ROWS = [
    (11, "user", "有給は?", [], NOW),
    (12, "assistant", "10日です。[1]", ["有給休暇.txt"], NOW),
    (13, "user", "その繰り越しは?", [], NOW),
]


def test_conversation_is_returned_whole_and_in_order(client):
    """途中を切らない。流れが読めないと「なぜその質問になったか」が分からない。"""
    calls: list = []
    with patch.object(conversations, "get_conn", FakeConn(calls, rows=MESSAGE_ROWS)):
        res = client.get("/conversations/5")
    assert res.status_code == 200, res.text

    body = res.json()
    assert body["conversation_id"] == 5
    assert [m["id"] for m in body["messages"]] == [11, 12, 13]
    assert body["messages"][1]["sources"] == ["有給休暇.txt"]
    # 生成に載せる直近N件（load_history）と違い、件数で切らない
    sql, _ = calls[1]
    assert "LIMIT" not in sql
    assert "ORDER BY id" in sql


def test_missing_conversation_is_404(client):
    """存在しない会話は 404（空の会話と区別できないと、消えたのか空なのか分からない）。"""
    calls: list = []
    with patch.object(conversations, "get_conn", FakeConn(calls, one=None)):
        res = client.get("/conversations/999")
    assert res.status_code == 404
    assert res.json()["error"] == "conversation_not_found"


def test_empty_conversation_is_not_404(client):
    """発言0件の会話は「存在するが空」。404 にすると作っただけの会話が見られない。"""
    calls: list = []
    with patch.object(conversations, "get_conn", FakeConn(calls, rows=[], one=(1,))):
        res = client.get("/conversations/5")
    assert res.status_code == 200
    assert res.json()["messages"] == []
