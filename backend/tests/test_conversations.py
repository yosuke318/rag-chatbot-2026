"""会話履歴（conversations / messages）とストリーミング回答のテスト。

DB・生成APIは触らずモックする。確認するのは:
  - 履歴の読み出し順（Claudeへ渡すのは必ず古い順）と件数の絞り込み
  - 続きの質問で履歴が生成に渡ること・自分の質問が履歴に混ざらないこと
  - 過去の回答から引用マーカーを外して渡すこと（古い番号の再利用を防ぐ）
  - SSE の順序（meta → delta → done）と、生成中に落ちたときの error イベント
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import conversations, llm, main as main_module, storage  # noqa: E402

HITS = [{"id": 1, "content": "第5条 有給は10日付与する。", "source": "有給休暇.txt"}]


class FakeConn:
    """conn.execute(...).fetchone()/fetchall() だけを満たす最小のダミー接続。"""

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


# --- 履歴の読み書き -----------------------------------------------------------


def test_load_history_returns_oldest_first(monkeypatch):
    """DBからは新しい順で取り、返すときに古い順へ戻す。

    直近N件に絞るには新しい順で LIMIT するしかないが、Claudeへ渡すメッセージ列は
    時系列でなければならないため、ここで必ず反転させる。
    """
    conn = FakeConn([("assistant", "A2"), ("user", "Q2"), ("assistant", "A1")])
    monkeypatch.setattr(conversations, "get_conn", lambda: conn)

    history = conversations.load_history(7)

    assert history == [
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "A2"},
    ]
    sql, params = conn.calls[0]
    assert "ORDER BY id DESC" in sql
    assert params == (7, conversations.HISTORY_MESSAGES)


def test_load_history_with_zero_limit_skips_db(monkeypatch):
    """履歴0件設定（単発の一問一答に戻す）ではDBを引かない。"""
    monkeypatch.setattr(
        conversations, "get_conn", lambda: pytest.fail("DBを引いてはいけない")
    )
    assert conversations.load_history(1, limit=0) == []


def test_resolve_creates_conversation_when_id_is_missing(monkeypatch):
    monkeypatch.setattr(conversations, "create", lambda title=None: 42)
    monkeypatch.setattr(conversations, "exists", lambda cid: pytest.fail("見に行かない"))
    assert conversations.resolve(None, title="有給は?") == 42


def test_resolve_keeps_existing_id(monkeypatch):
    monkeypatch.setattr(conversations, "exists", lambda cid: True)
    assert conversations.resolve(9) == 9


def test_resolve_rejects_unknown_id(monkeypatch):
    """存在しないIDは黙って新規作成しない（履歴が繋がらない事故に気づけるように）。"""
    monkeypatch.setattr(conversations, "exists", lambda cid: False)
    monkeypatch.setattr(
        conversations, "create", lambda title=None: pytest.fail("作ってはいけない")
    )
    with pytest.raises(conversations.UnknownConversation):
        conversations.resolve(999)


# --- 履歴の渡し方（llm 側） ---------------------------------------------------


def test_strip_citations_removes_markers():
    assert llm.strip_citations("10日です [1]。上限は20日 [1][2]。") == "10日です。上限は20日。"


def test_history_is_placed_before_the_current_question():
    history = [{"role": "user", "content": "有給は?"}, {"role": "assistant", "content": "10日です [1]。"}]
    messages = llm._answer_messages("その上限は?", ["文脈A"], history)

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # 過去の回答は引用マーカーを外して渡す（今回の [1] と混同させない）
    assert messages[1]["content"] == "10日です。"
    # コンテキストが付くのは今回の質問だけ
    assert "コンテキスト" not in messages[0]["content"]
    assert "[1] 文脈A" in messages[2]["content"]
    assert "その上限は?" in messages[2]["content"]


def test_stream_answer_yields_text_deltas():
    with patch.object(llm, "_anthropic") as client:
        stream = client.messages.stream.return_value.__enter__.return_value
        stream.text_stream = iter(["入社6か月", "で10日です [1]。"])
        assert list(llm.stream_answer("質問", ["文脈"])) == ["入社6か月", "で10日です [1]。"]

    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["system"] == llm.SYSTEM_PROMPT  # 非ストリーミング版と同じ指示


# --- エンドポイント -----------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


@pytest.fixture
def stubs(monkeypatch):
    """検索・S3・会話DBを差し替える。保存された発言と生成への引数を記録して返す。"""
    saved: list[tuple] = []
    seen: dict = {}

    monkeypatch.setattr(main_module, "hybrid_search", lambda q: HITS)
    monkeypatch.setattr(storage, "file_url", lambda source: None)
    monkeypatch.setattr(conversations, "resolve", lambda cid, title=None: cid or 5)
    monkeypatch.setattr(
        conversations,
        "load_history",
        lambda cid: [{"role": "user", "content": "有給は?"}] if cid == 5 else [],
    )
    monkeypatch.setattr(
        conversations,
        "add_message",
        lambda cid, role, content, sources=None: saved.append((cid, role, content, sources))
        or len(saved),
    )

    def fake_generate(question, contexts, history=None):
        seen["history"] = list(history or [])
        return "10日です。[1]"

    def fake_stream(question, contexts, history=None):
        seen["history"] = list(history or [])
        yield "10日"
        yield "です。[1]"

    monkeypatch.setattr(main_module, "generate_answer", fake_generate)
    monkeypatch.setattr(main_module, "stream_answer", fake_stream)
    return saved, seen


def test_chat_returns_conversation_id(client, stubs):
    res = client.post("/chat", json={"question": "有給は?"})
    assert res.status_code == 200
    assert res.json()["conversation_id"] == 5


def test_chat_feeds_history_to_generation_and_saves_both_messages(client, stubs):
    saved, seen = stubs

    res = client.post("/chat", json={"question": "その上限は?", "conversation_id": 5})

    assert res.status_code == 200
    # 直前のやり取りが生成に渡っている＝続きの質問に答えられる
    assert seen["history"] == [{"role": "user", "content": "有給は?"}]
    # 質問→回答の順で保存される（回答には出典も残す）
    assert [(s[1], s[2]) for s in saved] == [
        ("user", "その上限は?"),
        ("assistant", "10日です。[1]"),
    ]
    assert saved[1][3] == ["有給休暇.txt"]


def test_history_is_read_before_saving_the_new_question(client, monkeypatch, stubs):
    """★今回の質問を履歴に含めない★（同じ質問が2回入って文脈が濁るのを防ぐ）。"""
    order: list[str] = []
    monkeypatch.setattr(
        conversations, "load_history", lambda cid: order.append("load") or []
    )
    monkeypatch.setattr(
        conversations,
        "add_message",
        lambda *a, **kw: order.append("save") or 1,
    )

    client.post("/chat", json={"question": "有給は?", "conversation_id": 5})

    assert order[:2] == ["load", "save"]


def test_unknown_conversation_returns_404(client, stubs, monkeypatch):
    def boom(cid, title=None):
        raise conversations.UnknownConversation("会話が見つかりません: 999")

    monkeypatch.setattr(conversations, "resolve", boom)
    res = client.post("/chat", json={"question": "有給は?", "conversation_id": 999})

    assert res.status_code == 404
    assert res.json()["error"] == "unknown_conversation"


# --- ストリーミング -----------------------------------------------------------


def _events(text: str) -> list[tuple[str, str]]:
    """SSEの本文を (イベント名, data) の並びに戻す。"""
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], lines["data"]))
    return out


def test_stream_sends_meta_then_deltas_then_done(client, stubs):
    saved, seen = stubs

    res = client.post("/chat/stream", json={"question": "有給は?", "conversation_id": 5})

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = _events(res.text)
    assert [name for name, _ in events] == ["meta", "delta", "delta", "done"]

    # meta は生成より先に出る＝本文が届く前に根拠を表示できる
    meta = json.loads(events[0][1])
    assert meta["conversation_id"] == 5
    assert meta["sources"] == ["有給休暇.txt"]
    assert meta["citations"][0]["chunk_id"] == 1
    # delta を連結すると回答本文になる
    assert "".join(json.loads(d)["text"] for _, d in events[1:3]) == "10日です。[1]"
    # 履歴に保存されるのは連結後の完成した回答
    assert saved[-1][1:3] == ("assistant", "10日です。[1]")
    assert seen["history"] == [{"role": "user", "content": "有給は?"}]


def test_stream_reports_generation_failure_as_an_error_event(client, stubs, monkeypatch):
    """ストリーム開始後は4xx/5xxに戻せないので、エラーは error イベントで伝える。"""
    saved, _ = stubs

    def boom(question, contexts, history=None):
        raise llm.MissingAPIKey("ANTHROPIC_API_KEY")
        yield  # pragma: no cover - ジェネレータにするためだけ

    monkeypatch.setattr(main_module, "stream_answer", boom)

    res = client.post("/chat/stream", json={"question": "有給は?"})

    assert res.status_code == 200
    events = _events(res.text)
    assert [name for name, _ in events] == ["meta", "error"]

    payload = json.loads(events[1][1])
    assert payload["error"] == "missing_api_key"
    assert "ANTHROPIC_API_KEY" in payload["hint"]
    # 失敗した回答は履歴に残さない（質問だけが残る）
    assert [s[1] for s in saved] == ["user"]


def test_stream_rejects_empty_question(client, stubs):
    res = client.post("/chat/stream", json={"question": "   "})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_question"
