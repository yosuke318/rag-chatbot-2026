"""公開API(/v1)の認証・テナント分離・レート制限・利用ログのテスト（YOSUKE-22）。

ここで守りたいのは「キーを持つ人が、自分のプロジェクトの範囲だけを、決めた
本数だけ叩ける」こと。特にテナント分離は破れると他社の文書が回答に混ざるので、
"越えられないこと"を明示的に書いてある。

DBには繋がず、app.apikeys の読み書きを差し替えて検証する。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import apikeys  # noqa: E402
from app import main as main_module  # noqa: E402

KEY = apikeys.ApiKey(id=7, name="営業部ツール", project="社内規程", rate_limit_per_min=60)
TOKEN = "ragk_dummy-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


@pytest.fixture
def authed(monkeypatch):
    """有効なキーが1本ある状態。利用ログとレート制限の記録を返す。"""
    usage: list[tuple[int, str]] = []
    statuses: list[tuple[int, int]] = []

    monkeypatch.setattr(apikeys, "lookup", lambda t: KEY if t == TOKEN else None)
    monkeypatch.setattr(apikeys, "count_recent", lambda key_id: 0)
    monkeypatch.setattr(
        apikeys,
        "log_request",
        lambda key_id, path: (usage.append((key_id, path)), 100)[1],
    )
    monkeypatch.setattr(
        apikeys, "set_status", lambda uid, status: statuses.append((uid, status))
    )
    return {"usage": usage, "statuses": statuses}


# --- 認証 ---------------------------------------------------------------------


def test_bearer_token_parsing():
    assert apikeys.bearer_token("Bearer abc") == "abc"
    assert apikeys.bearer_token("bearer abc") == "abc"  # スキーム名は大小を問わない
    assert apikeys.bearer_token("Basic abc") is None
    assert apikeys.bearer_token("abc") is None
    assert apikeys.bearer_token("Bearer   ") is None
    assert apikeys.bearer_token(None) is None


def test_token_is_hashed_not_stored_raw():
    token = apikeys.generate_token()
    assert token.startswith(apikeys.KEY_PREFIX)
    digest = apikeys.token_hash(token)
    assert digest != token and len(digest) == 64  # sha256 の16進表現
    assert apikeys.token_hash(token) == digest  # 同じ入力は同じハッシュ


def test_v1_requires_api_key(client):
    res = client.get("/v1/search?q=有給")
    assert res.status_code == 401
    assert res.json()["error"] == "invalid_api_key"


def test_v1_rejects_unknown_key(client, authed):
    res = client.get("/v1/search?q=有給", headers={"authorization": "Bearer nope"})
    assert res.status_code == 401
    assert authed["usage"] == []  # 認証に落ちたものは利用ログに残さない


def test_revoked_key_is_rejected(client, monkeypatch):
    """失効させたキーは lookup が None を返す＝401。"""
    monkeypatch.setattr(apikeys, "lookup", lambda t: None)
    res = client.get("/v1/search?q=有給", headers=AUTH)
    assert res.status_code == 401


# --- テナント分離 -------------------------------------------------------------


def test_v1_search_uses_project_from_key(client, authed):
    empty = {"question": "q", "retrievers": [], "available_retrievers": [],
             "applied_params": {"rrf_k": 60, "retrievers": {}},
             "lexical_min_similarity": 0.0, "stages": [], "fused": []}
    with patch.object(main_module, "search_stages", return_value=empty) as m:
        res = client.get("/v1/search?q=有給&topic=労務", headers=AUTH)

    assert res.status_code == 200
    # ★検索範囲はキーの project★（リクエストからは取らない）
    assert m.call_args.kwargs["project"] == "社内規程"
    assert m.call_args.kwargs["topic"] == "労務"


def test_v1_rejects_project_in_query(client, authed):
    """★project は指定できない★ 黙って無視すると「絞ったつもり」で使われる。"""
    with patch.object(main_module, "search_stages") as m:
        res = client.get("/v1/search?q=有給&project=他社", headers=AUTH)

    assert res.status_code == 400
    assert res.json()["error"] == "project_not_allowed"
    assert m.call_count == 0  # 検索まで進ませない


def test_v1_chat_rejects_project_in_body(client, authed):
    """本文に project を混ぜても通らない（PublicChatRequest は extra="forbid"）。"""
    res = client.post(
        "/v1/chat", json={"question": "有給は?", "project": "他社"}, headers=AUTH
    )
    assert res.status_code == 422


def test_v1_chat_answers_within_key_project(client, authed, monkeypatch):
    from app import conversations, storage

    seen: dict = {}

    def fake_search(question, **kwargs):
        seen.update(kwargs)
        return [{"id": 1, "content": "本文", "source": "有給休暇.txt"}]

    monkeypatch.setattr(main_module, "hybrid_search", fake_search)
    monkeypatch.setattr(main_module, "generate_answer", lambda *a, **kw: "回答 [1]")
    monkeypatch.setattr(storage, "file_url", lambda source: None)
    monkeypatch.setattr(conversations, "load_history", lambda cid: [])
    monkeypatch.setattr(conversations, "add_message", lambda *a, **kw: 1)
    # 会話の持ち主としてキーIDが渡ることを見る
    resolved: dict = {}

    def fake_resolve(cid, title=None, api_key_id=None):
        resolved.update({"cid": cid, "api_key_id": api_key_id})
        return cid or 42

    monkeypatch.setattr(conversations, "resolve", fake_resolve)

    res = client.post("/v1/chat", json={"question": "有給は?"}, headers=AUTH)

    assert res.status_code == 200
    assert seen["project"] == "社内規程"  # キーのプロジェクトで検索している
    assert resolved["api_key_id"] == KEY.id  # 会話はこのキーのものとして作られる


def test_v1_chat_cannot_continue_another_owners_conversation(
    client, authed, monkeypatch
):
    """★他人の conversation_id は続けられない★（履歴＝他テナントの中身が漏れる）。"""
    from app import conversations

    def fake_resolve(cid, title=None, api_key_id=None):
        # owned_by が False のときの実装と同じ振る舞い
        raise conversations.UnknownConversation(f"会話が見つかりません: {cid}")

    monkeypatch.setattr(conversations, "resolve", fake_resolve)

    res = client.post(
        "/v1/chat", json={"question": "続き", "conversation_id": 1}, headers=AUTH
    )
    # 403 ではなく404: 「そのIDは存在する」と教えない
    assert res.status_code == 404
    assert res.json()["error"] == "unknown_conversation"


def test_owned_by_matches_null_owner_for_ui(monkeypatch):
    """UI発の会話(api_key_id=NULL)は None 同士で一致する（IS NOT DISTINCT FROM）。"""
    from app import conversations

    calls: list = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))
            return type("R", (), {"fetchone": lambda _s: (1,)})()

    monkeypatch.setattr(conversations, "get_conn", FakeConn)
    assert conversations.owned_by(5, None) is True
    sql, params = calls[0]
    assert "IS NOT DISTINCT FROM" in sql  # = %s だと NULL に一致しない
    assert params == (5, None)


# --- レート制限・利用ログ -----------------------------------------------------


def test_rate_limit_returns_429_without_searching(client, monkeypatch):
    monkeypatch.setattr(apikeys, "lookup", lambda t: KEY)
    monkeypatch.setattr(apikeys, "count_recent", lambda key_id: KEY.rate_limit_per_min)
    logged: list = []
    monkeypatch.setattr(
        apikeys, "log_request", lambda k, p: (logged.append(p), 1)[1]
    )

    with patch.object(main_module, "search_stages") as m:
        res = client.get("/v1/search?q=有給", headers=AUTH)

    assert res.status_code == 429
    assert res.json()["error"] == "api_rate_limit"
    # 上限超えは検索も外部API呼び出しもさせない／記録もしない
    assert m.call_count == 0 and logged == []


def test_request_is_logged_with_path_and_status(client, authed):
    empty = {"question": "q", "retrievers": [], "available_retrievers": [],
             "applied_params": {"rrf_k": 60, "retrievers": {}},
             "lexical_min_similarity": 0.0, "stages": [], "fused": []}
    with patch.object(main_module, "search_stages", return_value=empty):
        client.get("/v1/search?q=有給", headers=AUTH)

    assert authed["usage"] == [(KEY.id, "/v1/search")]
    assert authed["statuses"] == [(100, 200)]  # 応答後にステータスを書き戻す


def test_status_writeback_failure_does_not_break_the_response(client, monkeypatch):
    """★利用ログの書き戻しに失敗しても応答は壊さない★（PR #9 レビュー指摘）。

    ログは補助情報で、応答はもう出来上がっている。ここで例外を通すと、DBの
    一時障害だけで成功していた /v1 の応答が 500 に化ける。
    """
    monkeypatch.setattr(apikeys, "lookup", lambda t: KEY)
    monkeypatch.setattr(apikeys, "count_recent", lambda key_id: 0)
    monkeypatch.setattr(apikeys, "log_request", lambda k, p: 100)

    def boom(usage_id, status):
        raise RuntimeError("DBが一時的に落ちている")

    monkeypatch.setattr(apikeys, "set_status", boom)

    empty = {"question": "q", "retrievers": [], "available_retrievers": [],
             "applied_params": {"rrf_k": 60, "retrievers": {}},
             "lexical_min_similarity": 0.0, "stages": [], "fused": []}
    with patch.object(main_module, "search_stages", return_value=empty):
        res = client.get("/v1/search?q=有給", headers=AUTH)

    assert res.status_code == 200


def test_usage_is_not_recorded_for_internal_endpoints(client, authed):
    """既存の /search（画面用）は認証も課金対象の記録もしない（据え置き）。"""
    empty = {"question": "q", "retrievers": [], "available_retrievers": [],
             "applied_params": {"rrf_k": 60, "retrievers": {}},
             "lexical_min_similarity": 0.0, "stages": [], "fused": []}
    with patch.object(main_module, "search_stages", return_value=empty), \
         patch("app.saved_questions.save"):
        res = client.get("/search?q=有給")

    assert res.status_code == 200
    assert authed["usage"] == []


# --- 発行まわり（DBに触る部分は SQL の形だけ見る） -----------------------------


def test_create_key_requires_project(monkeypatch):
    """project 無しのキーは作らせない（テナントの境界が無いキーになる）。"""
    with pytest.raises(ValueError):
        apikeys.create_key("名前", "  ")


def test_create_key_stores_hash_only(monkeypatch):
    saved: list = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            saved.append((sql, params))
            return type("R", (), {"fetchone": lambda _s: (3,)})()

    monkeypatch.setattr(apikeys, "get_conn", FakeConn)
    token, key_id = apikeys.create_key("営業部ツール", "社内規程")

    _sql, params = saved[0]
    assert key_id == 3
    assert token not in params  # 平文は渡さない
    assert apikeys.token_hash(token) in params
    assert "社内規程" in params
