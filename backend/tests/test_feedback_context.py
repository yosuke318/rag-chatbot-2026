"""フィードバックに文脈を残す（8-1）のテスト。

👍/👎 の本体は「値そのもの」ではなく★どういう条件で出た回答への評価か★で、
そこが欠けると「この設定変更で👎が減った」「👎のとき正解は何位に居たのか」を
後から追えない。ここで固定するのは次の2つ:

  1. サーバが条件を返すこと（/chat・/chat/stream）
     検索手法・top_k・リランカーはリクエストで受け取らず設定の既定で動くので、
     クライアントは知りようがない。使った側が返さないと記録できない。
  2. 返した条件がそのまま記録されること（POST /feedback）
     ＋ 送らなかった場合も 200 で通ること（古いクライアントの👎を捨てない）

DBは触らず、app.main.get_conn を差し替えて発行SQLとパラメータを見る
（test_documents_summary_api.py と同じやり方）。
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import conversations, storage  # noqa: E402
from app import main as main_module  # noqa: E402

HITS = [
    {"id": 101, "content": "第5条 年次有給休暇は10日を付与する。", "source": "有給休暇.txt"},
    {"id": 203, "content": "第30条 経費は翌月5日までに申請する。", "source": "経費精算.txt"},
]


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


# --- POST /feedback -----------------------------------------------------------


class _FakeConn:
    """発行SQLを記録し、INSERT には固定のIDを返す最小のコネクション。"""

    def __init__(self, calls: list):
        self.calls = calls

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return type("R", (), {"fetchone": lambda self: (7,)})()


def _post(client, body: dict) -> tuple[dict, list]:
    calls: list = []
    with patch.object(main_module, "get_conn", _FakeConn(calls)):
        res = client.post("/feedback", json=body)
    assert res.status_code == 200, res.text
    return res.json(), calls


def _inserted(calls: list) -> dict:
    """INSERT の列名 → 渡した値。列の並びとプレースホルダのズレもここで露見する。"""
    sql, params = calls[0]
    columns = sql.split("(", 1)[1].split(")", 1)[0]
    names = [c.strip() for c in columns.split(",")]
    assert len(names) == len(params), f"列 {len(names)} 個に対し値 {len(params)} 個"
    return dict(zip(names, params))


def test_records_the_context_of_the_answer(client):
    body, calls = _post(
        client,
        {
            "question": "有給は?",
            "answer": "10日です。[1]",
            "sources": ["有給休暇.txt"],
            "rating": -1,
            "conversation_id": 5,
            "message_id": 42,
            "retriever": "vector,trgm",
            "top_k": 4,
            "reranked": False,
            "chunk_ids": [101, 203],
            "latency_ms": 1234,
        },
    )

    assert body == {"id": 7, "rating": -1}
    assert _inserted(calls) == {
        "question": "有給は?",
        "answer": "10日です。[1]",
        "sources": ["有給休暇.txt"],
        "rating": -1,
        "comment": None,
        "conversation_id": 5,
        "message_id": 42,
        "retriever": "vector,trgm",
        "top_k": 4,
        "reranked": False,
        # 並びがそのまま順位。ここを集合や順不同で持つと「👎のとき正解が何位に
        # 居たか」が分からなくなり、このカラムを足した意味が消える。
        "chunk_ids": [101, 203],
        "latency_ms": 1234,
    }


def test_context_is_optional(client):
    """文脈を送らない（この機能より前の）リクエストでも 200 で記録される。"""
    body, calls = _post(
        client,
        {"question": "有給は?", "answer": "10日です。", "rating": 1},
    )

    assert body == {"id": 7, "rating": 1}
    row = _inserted(calls)
    # 未記録は NULL。0 や "" で埋めると「そう記録された」と読めてしまう。
    for column in ("conversation_id", "message_id", "retriever", "top_k",
                   "reranked", "latency_ms"):
        assert row[column] is None, column
    # chunk_ids だけは空配列（sources と同じ扱い。NULLと空の二重の空を作らない）
    assert row["chunk_ids"] == []


@pytest.mark.parametrize("rating", [0, 2, -3])
def test_invalid_rating_is_still_rejected(client, rating):
    """文脈カラムを足しても rating の検査は素通しにならない（DBにも触らない）。"""
    calls: list = []
    with patch.object(main_module, "get_conn", _FakeConn(calls)):
        res = client.post(
            "/feedback",
            json={"question": "有給は?", "answer": "10日です。", "rating": rating},
        )
    assert res.status_code == 400
    assert calls == []


# --- /chat・/chat/stream が条件を返すこと --------------------------------------


@pytest.fixture
def stubs(monkeypatch):
    """検索・生成・S3・会話DBを差し替える。実際に hybrid_search へ渡った引数も返す。"""
    passed: dict = {}

    def fake_search(question, **kw):
        passed.update(kw)
        return HITS

    monkeypatch.setattr(main_module, "hybrid_search", fake_search)
    monkeypatch.setattr(storage, "file_url", lambda source: None)
    monkeypatch.setattr(
        conversations, "resolve", lambda cid, title=None, api_key_id=None: cid or 5
    )
    monkeypatch.setattr(conversations, "load_history", lambda cid: [])
    # 発言IDは 11, 12, … と順に振る（質問→回答の順で呼ばれる）
    ids = iter(range(11, 99))
    monkeypatch.setattr(
        conversations, "add_message", lambda *a, **kw: next(ids)
    )
    monkeypatch.setattr(
        main_module, "generate_answer", lambda q, c, h=None: "10日です。[1]"
    )
    monkeypatch.setattr(
        main_module, "stream_answer", lambda q, c, h=None: iter(["10日", "です。[1]"])
    )
    return passed


def test_chat_returns_the_settings_it_actually_used(client, stubs):
    """返す条件と検索に渡す条件が同じものであること。

    ★ここがこのテストの主眼★
      既定を「検索する側」と「返す側」で別々に読むと、片方だけ変わったときに
      使っていない条件を記録してしまう。同じ値が両方に出ることを固定する。
    """
    res = client.post("/chat", json={"question": "有給は?"})
    assert res.status_code == 200
    body = res.json()

    retrieval = body["retrieval"]
    assert retrieval["retriever"] == ",".join(stubs["retrievers"])
    assert retrieval["top_k"] == stubs["top_n"]
    assert retrieval["reranked"] == stubs["rerank"]
    # 記録に使う値なので、空やゼロで返らないこと（設定の読み落としに気づく）
    assert retrieval["retriever"]
    assert retrieval["top_k"] > 0


def test_chat_returns_message_id_and_latency(client, stubs):
    body = client.post("/chat", json={"question": "有給は?"}).json()
    # 質問(11)の次に保存される回答が 12。フィードバックの宛先はこちら。
    assert body["message_id"] == 12
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0


def test_chat_citations_carry_the_chunk_order(client, stubs):
    """chunk_ids はクライアントが citations から作る。その並び＝順位であること。"""
    body = client.post("/chat", json={"question": "有給は?"}).json()
    assert [c["chunk_id"] for c in body["citations"]] == [101, 203]
    assert [c["n"] for c in body["citations"]] == [1, 2]


def _events(text: str) -> list[tuple[str, dict]]:
    """SSEの本文を (イベント名, データ) の列に開く。"""
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        name = next(l[7:] for l in lines if l.startswith("event: "))
        raw = next(l[6:] for l in lines if l.startswith("data: "))
        out.append((name, json.loads(raw)))
    return out


def test_stream_puts_settings_in_meta_and_the_answer_id_in_done(client, stubs):
    """★条件は meta・回答IDと所要時間は done★

    meta は生成の前に流れるので、そこに message_id は入れられない（回答を
    保存して初めてIDが決まる）。取り違えるとフィードバックが常に null になる。
    """
    res = client.post("/chat/stream", json={"question": "有給は?"})
    assert res.status_code == 200
    events = dict(_events(res.text))

    assert events["meta"]["retrieval"]["retriever"] == ",".join(stubs["retrievers"])
    assert "message_id" not in events["meta"]

    assert events["done"]["message_id"] == 12
    assert events["done"]["latency_ms"] >= 0
