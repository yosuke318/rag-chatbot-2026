"""差分検知（content_hash で無変更なら埋め込みをスキップ）のテスト。

狙いは「埋め込みAPIが呼ばれないこと」なので、DBは触らず（このリポジトリの
テストはDB非依存）、ingest_text が発行するSQLを記録する偽コネクションと、
呼ばれたら記録するだけの偽 embed_texts で確かめる。
"""
from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from app import ingest as ingest_module  # noqa: E402
from app.config import EMBED_DIM  # noqa: E402
from app.ingest import content_hash, ingest_text  # noqa: E402

TEXT = (
    "第1条 目的\nこの規程は、従業員の労働条件について定める。\n"
    "第2条 適用範囲\n本規程は全従業員に適用する。\n"
)


class _Result:
    """psycopg の execute() が返すカーソルの、テストで使う部分だけ。"""

    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, params_seq):
        self.conn.chunk_rows.extend(params_seq)


class FakeConn:
    """ingest_text が発行するSQLだけを解釈する偽コネクション。

    existing: documents の既存行として SELECT が返すタプル
      (id, content_hash, project_id, topic_id)。None なら未登録。
    chunk_count: スキップ時に数える既存チャンク数。
    """

    def __init__(self, existing=None, chunk_count=2):
        self.existing = existing
        self.chunk_count = chunk_count
        self.sql = []  # (sql, params) の記録
        self.chunk_rows = []

    def __call__(self):  # get_conn() の差し替え先
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        head = sql.strip().split()[0].upper()
        if head == "SELECT":
            if "count(*)" in sql:  # スキップ時のチャンク数
                return _Result(row=(self.chunk_count,))
            return _Result(row=self.existing)
        if head == "DELETE":
            return _Result(rowcount=1 if self.existing else 0)
        if head == "INSERT":
            return _Result(row=(42,))
        return _Result()

    def transaction(self):
        return _Transaction()

    def cursor(self):
        return _Cursor(self)

    def statements(self, head):
        return [(s, p) for s, p in self.sql if s.strip().upper().startswith(head)]


@pytest.fixture
def calls(monkeypatch):
    """埋め込み・文脈生成・S3保存の呼び出し回数を数える。"""
    counts = {"embed": 0, "context": 0, "save_text": 0}

    def fake_embed(texts, **kwargs):
        counts["embed"] += 1
        return [[0.0] * EMBED_DIM for _ in texts]

    def fake_contexts(text, chunk_texts):
        counts["context"] += 1
        return ["" for _ in chunk_texts]

    class FakeStorage:
        @staticmethod
        def save_text(source, text):
            counts["save_text"] += 1

    monkeypatch.setattr(ingest_module, "embed_texts", fake_embed)
    monkeypatch.setattr(ingest_module, "generate_chunk_contexts", fake_contexts)
    monkeypatch.setattr(ingest_module, "storage", FakeStorage)
    return counts


def _existing(hash_value, project_id=None, topic_id=None):
    return (42, hash_value, project_id, topic_id)


@pytest.fixture
def scope_ids(monkeypatch):
    """scopes.register を固定idを返す偽物に差し替える。

    ingest_text は区分名をマスタへ写して id を得てから documents に入れる。
    マスタ側の挙動は test_scopes.py の関心事なので、ここでは
    「社内規程→1 / 労務→2」を返すだけにして本体（差分検知）に集中する。
    """
    ids = {"社内規程": 1, "労務": 2}
    monkeypatch.setattr(
        ingest_module.scopes,
        "register",
        lambda p, t: (ids.get(p), ids.get(t)),
    )
    return ids


# --- content_hash 単体 -------------------------------------------------


def test_hash_is_stable_for_same_input():
    assert content_hash(TEXT, False) == content_hash(TEXT, False)


def test_hash_changes_with_text():
    assert content_hash(TEXT, False) != content_hash(TEXT + "第3条 改正", False)


def test_hash_changes_with_contextual():
    """app.compare は同じ文書を False/True で入れ直して比較する。

    ここが同じハッシュになると2回目がスキップされ、比較が成立しなくなる。
    """
    assert content_hash(TEXT, False) != content_hash(TEXT, True)


def test_hash_handles_separator_like_text():
    """本文に区切り文字（NUL）が混ざっても、項目の境目がずれない。"""
    assert content_hash("a\x00False", False) != content_hash("a", False)
    assert content_hash("a\x00b", False) == content_hash("a\x00b", False)


def test_hash_changes_with_embed_model(monkeypatch):
    before = content_hash(TEXT, False)
    monkeypatch.setattr(ingest_module, "EMBED_MODEL", "voyage-9-imaginary")
    assert content_hash(TEXT, False) != before


def test_hash_changes_with_chunking_version(monkeypatch):
    before = content_hash(TEXT, False)
    monkeypatch.setattr(ingest_module, "CHUNKING_VERSION", "99")
    assert content_hash(TEXT, False) != before


# --- ingest_text の分岐 ------------------------------------------------


def test_first_ingest_embeds_and_stores_hash(monkeypatch, calls):
    """新規登録では埋め込みを呼び、documents に content_hash を入れる。"""
    conn = FakeConn(existing=None)
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    result = ingest_text("a.txt", TEXT, contextual=False)

    assert calls["embed"] == 1
    assert result["skipped"] is False
    assert result["chunks_created"] == len(conn.chunk_rows)
    insert_sql, insert_params = conn.statements("INSERT")[0]
    assert "content_hash" in insert_sql
    assert insert_params[3] == content_hash(TEXT, False)


def test_reingest_same_content_skips_embedding(monkeypatch, calls):
    """受け入れ条件: 同一内容の再取り込みで埋め込みAPIが呼ばれない。"""
    conn = FakeConn(existing=_existing(content_hash(TEXT, False)), chunk_count=2)
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    result = ingest_text("a.txt", TEXT, contextual=False)

    assert calls["embed"] == 0
    assert calls["context"] == 0  # Claude（文脈生成）も呼ばない
    assert result == {
        "chunks_created": 2,
        "replaced": 0,
        "skipped": True,
        "images_stored": 0,
    }
    assert conn.statements("DELETE") == []  # 既存文書は消さない（id が保たれる）
    assert conn.statements("INSERT") == []


def test_changed_content_reembeds(monkeypatch, calls):
    """受け入れ条件: 内容変更時のみ再埋め込みされる。"""
    conn = FakeConn(existing=_existing(content_hash(TEXT, False)))
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    changed = TEXT + "第3条 この規程は令和8年4月1日から施行する。\n"
    result = ingest_text("a.txt", changed, contextual=False)

    assert calls["embed"] == 1
    assert result["skipped"] is False
    assert result["replaced"] == 1
    assert conn.statements("INSERT")[0][1][3] == content_hash(changed, False)
    # 作り直す場合、既存チャンク数は戻り値に使わないので数えない
    assert not [s for s, _ in conn.sql if "count(*)" in s]


def test_contextual_switch_reembeds(monkeypatch, calls):
    """本文が同じでも contextual を切り替えたら埋め込み直す（app.compare 用）。"""
    conn = FakeConn(existing=_existing(content_hash(TEXT, False)))
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    ingest_text("a.txt", TEXT, contextual=True)

    assert calls["embed"] == 1
    assert calls["context"] == 1


def test_legacy_row_without_hash_is_reingested(monkeypatch, calls):
    """この機能より前に入った文書（content_hash が NULL）は一度だけ入れ直す。"""
    conn = FakeConn(existing=_existing(None))
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    result = ingest_text("a.txt", TEXT, contextual=False)

    assert calls["embed"] == 1
    assert result["skipped"] is False


def test_scope_only_change_updates_row_without_embedding(monkeypatch, calls, scope_ids):
    """本文は同じで区分だけ変えた場合、埋め込みは使い回して documents だけ更新。"""
    conn = FakeConn(
        existing=_existing(content_hash(TEXT, False), project_id=None, topic_id=None)
    )
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    result = ingest_text("a.txt", TEXT, project="社内規程", topic="労務", contextual=False)

    assert calls["embed"] == 0
    assert result["skipped"] is True
    update_sql, update_params = conn.statements("UPDATE")[0]
    assert "documents" in update_sql
    # 行に入るのは名前ではなくマスタの id（社内規程→1 / 労務→2）
    assert update_params == (1, 2, 42)


def test_same_scope_does_not_update(monkeypatch, calls, scope_ids):
    """区分も同じなら UPDATE も出さない（完全な no-op）。"""
    conn = FakeConn(
        existing=_existing(content_hash(TEXT, False), project_id=1, topic_id=2)
    )
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    ingest_text("a.txt", TEXT, project="社内規程", topic="労務", contextual=False)

    assert conn.statements("UPDATE") == []


def test_skip_still_saves_original(monkeypatch, calls):
    """スキップ時も原本保存は続ける（S3側だけ欠けている状態を直せるように）。"""
    conn = FakeConn(existing=_existing(content_hash(TEXT, False)))
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    ingest_text("a.txt", TEXT, contextual=False)
    assert calls["save_text"] == 1


def test_skip_respects_store_original_false(monkeypatch, calls):
    """/ingest-file 経由（原本はバイナリ側で保存）ではテキストを保存しない。"""
    conn = FakeConn(existing=_existing(content_hash(TEXT, False)))
    monkeypatch.setattr(ingest_module, "get_conn", conn)

    ingest_text("a.txt", TEXT, store_original=False, contextual=False)
    assert calls["save_text"] == 0


# --- 文書内画像（5-1）の受け渡し ---------------------------------------


def _record_store_images(monkeypatch):
    """store_images の呼び出しを記録するだけの差し替え。"""
    calls_made: list[tuple] = []

    def fake(document_id, source, images):
        calls_made.append((document_id, source, images))
        return len(images)

    monkeypatch.setattr(ingest_module, "store_images", fake)
    return calls_made


def test_ingest_stores_images_with_new_document_id(monkeypatch, calls):
    """新規登録では、作ったばかりの documents.id に画像を紐づける。"""
    conn = FakeConn(existing=None)
    monkeypatch.setattr(ingest_module, "get_conn", conn)
    made = _record_store_images(monkeypatch)

    result = ingest_text("a.pdf", TEXT, contextual=False, images=["img1", "img2"])

    assert made == [(42, "a.pdf", ["img1", "img2"])]  # 42 = INSERT ... RETURNING id
    assert result["images_stored"] == 2
    # 画像は chunks_created（本文を何チャンクに割ったか）には数えない
    assert result["chunks_created"] == len(conn.chunk_rows)


def test_skipped_ingest_still_stores_images(monkeypatch, calls):
    """★本文が同じでも画像は保存する★

    画像の保存には埋め込みAPIもClaudeも要らないので省く理由が無く、この機能より
    前に登録済みの文書を「同じファイルを入れ直すだけ」で画像対応にできる。
    """
    conn = FakeConn(existing=_existing(content_hash(TEXT, False)))
    monkeypatch.setattr(ingest_module, "get_conn", conn)
    made = _record_store_images(monkeypatch)

    result = ingest_text("a.pdf", TEXT, contextual=False, images=["img1"])

    assert calls["embed"] == 0  # 埋め込みはやはり省く
    assert result["skipped"] is True
    assert made == [(42, "a.pdf", ["img1"])]  # 既存文書の id に紐づく
    assert result["images_stored"] == 1
