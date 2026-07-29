"""チャンク単位の根拠明示（引用の紐付け）のテスト。

★守りたい不変条件★
  回答本文の [n] と citations[n-1] が必ず同じチャンクを指すこと。
  ここがズレると「根拠として示したチャンクが実は別物」になり、
  利用者が回答を検証できるという機能の前提そのものが壊れる。

DB・外部APIは触らずモックする。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import conversations, llm, main as main_module, storage  # noqa: E402

HITS = [
    {"id": 101, "content": "第5条 年次有給休暇は入社6か月経過後に10日を付与する。", "source": "有給休暇.txt"},
    {"id": 102, "content": "第6条 未消化の休暇は翌年度に限り繰り越せる。", "source": "有給休暇.txt"},
    {"id": 203, "content": "第30条 経費は翌月5日までに申請する。", "source": "経費精算.txt"},
]


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


@pytest.fixture
def chat(client, monkeypatch):
    """/chat を検索・生成・S3抜きで叩けるようにする。生成に渡った引数も返す。"""
    seen: dict = {}

    def fake_generate(question, contexts, history=None):
        seen["question"] = question
        seen["contexts"] = list(contexts)
        seen["history"] = list(history or [])
        return "入社6か月で10日付与されます。[1] 翌年度への繰り越しも可能です。[2]"

    monkeypatch.setattr(main_module, "hybrid_search", lambda q: HITS)
    monkeypatch.setattr(main_module, "generate_answer", fake_generate)
    monkeypatch.setattr(storage, "file_url", lambda source: None)
    # 会話履歴(DB)はここでは関心外なので素通しにする（本体は test_conversations.py）
    monkeypatch.setattr(conversations, "resolve", lambda cid, title=None: cid or 1)
    monkeypatch.setattr(conversations, "load_history", lambda cid: [])
    monkeypatch.setattr(conversations, "add_message", lambda *a, **kw: 1)

    def ask(question: str = "有給は何日?"):
        return client.post("/chat", json={"question": question}), seen

    return ask


# --- /chat のレスポンス -------------------------------------------------------


def test_chat_returns_chunk_level_citations(chat):
    res, _ = chat()

    assert res.status_code == 200
    body = res.json()
    assert [c["n"] for c in body["citations"]] == [1, 2, 3]
    assert [c["chunk_id"] for c in body["citations"]] == [101, 102, 203]
    assert body["citations"][0]["source"] == "有給休暇.txt"
    assert "年次有給休暇" in body["citations"][0]["preview"]
    # 出典名だけの sources も従来どおり返す（重複排除）
    assert body["sources"] == ["有給休暇.txt", "経費精算.txt"]


def test_citation_numbers_match_the_generated_context_order(chat):
    """★[n] の対応★ 生成に渡した本文の並びと citations の並びが一致する。"""
    res, seen = chat()

    citations = res.json()["citations"]
    assert seen["contexts"] == [h["content"] for h in HITS]
    for citation, content in zip(citations, seen["contexts"]):
        # n 番目の引用は、生成に渡した n 番目のコンテキストの冒頭であること
        assert content.startswith(citation["preview"].rstrip("…"))


def test_citation_preview_is_truncated(chat, monkeypatch):
    long_hit = [{"id": 1, "content": "あ" * 500, "source": "長文.txt"}]
    monkeypatch.setattr(main_module, "hybrid_search", lambda q: long_hit)

    res, _ = chat()

    preview = res.json()["citations"][0]["preview"]
    assert preview == "あ" * main_module.CITATION_PREVIEW_CHARS + "…"


def test_citation_carries_file_url_when_original_exists(chat, monkeypatch):
    monkeypatch.setattr(storage, "file_url", lambda source: f"/files/{source}")

    res, _ = chat()

    citations = res.json()["citations"]
    assert citations[0]["file_url"] == "/files/有給休暇.txt"
    assert citations[2]["file_url"] == "/files/経費精算.txt"


def test_file_url_is_looked_up_once_per_source(chat, monkeypatch):
    """同じ文書のチャンクが複数あってもS3への問い合わせは1回（head_objectを連打しない）。"""
    asked: list[str] = []
    monkeypatch.setattr(storage, "file_url", lambda s: asked.append(s) or None)

    chat()

    assert asked == ["有給休暇.txt", "経費精算.txt"]  # 101と102は同じ文書＝1回だけ


def test_citation_file_url_is_null_when_original_is_missing(chat):
    """原本が無い文書には URL を付けない（開けないリンクを出さない）。"""
    res, _ = chat()
    assert all(c["file_url"] is None for c in res.json()["citations"])


def test_empty_question_is_rejected_before_search(chat):
    res, seen = chat("   ")
    assert res.status_code == 400
    assert seen == {}  # 検索も生成も走っていない


# --- プロンプト側（番号付きコンテキスト） -------------------------------------


def test_number_contexts_is_one_based():
    block = llm.number_contexts(["A", "B"])
    assert block.startswith("[1] A")
    assert "[2] B" in block


def test_number_contexts_without_contexts():
    assert llm.number_contexts([]) == "(該当なし)"


def test_generate_answer_passes_numbered_contexts_and_asks_for_markers():
    with patch.object(llm, "_anthropic") as client:
        block = type("B", (), {"type": "text", "text": "回答[1]"})()
        client.messages.create.return_value = type("R", (), {"content": [block]})()
        assert llm.generate_answer("質問", ["文脈A", "文脈B"]) == "回答[1]"

    kwargs = client.messages.create.call_args.kwargs
    user_content = kwargs["messages"][0]["content"]
    assert "[1] 文脈A" in user_content and "[2] 文脈B" in user_content
    # 引用マーカーを付けさせる指示が system 側に入っていること
    assert "[1]" in kwargs["system"]
