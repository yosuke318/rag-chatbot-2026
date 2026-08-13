"""文書の削除（DELETE /documents）のテスト。

削除は取り消せない操作で、しかも失敗の仕方が静かなので、ここで固定するのは
主に「消しすぎない」と「消し残さない」:

  1. 0件の指定はエラー（押し間違いを 200 で返すと「消えたつもり」になる）
  2. 存在しない id が混ざっても、あるものは消えて missing_ids で返る
  3. 画像・原本はS3からも消える（DBだけ消すと取り出せない容量が残る）
  4. ★同名の行がまだ残っているときは原本を消さない★（残った行が使う）
  5. 正解ラベルが宙に浮いた評価質問の件数を返す（黙って指標が下がるのを防ぐ）

DBは触らず get_conn を差し替えて発行SQLとパラメータを見る
（test_feedback_promote.py と同じやり方）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import documents as documents_module  # noqa: E402
from app import main as main_module  # noqa: E402


class FakeConn:
    """SQLの見出しで答えを撃ち分ける偽コネクション。

    削除は「消す前に控える → 消す → 消した後の状態を見る」という順で複数の
    SELECT を撃つので、呼ばれた順ではなくSQLの中身で返す行を決める。
    """

    def __init__(self, calls: list, rows: dict, rowcount: int = 0):
        self.calls = calls
        self.rows = rows
        self.rowcount = rowcount

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        if sql.startswith("DELETE"):
            return type("R", (), {"rowcount": self.rowcount})()
        for key, rows in self.rows.items():
            if key in sql:
                return type("R", (), {"fetchall": lambda _s, r=rows: r})()
        return type("R", (), {"fetchall": lambda _s: []})()


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def _delete(client, ids: list, rows: dict, rowcount: int = 0):
    """削除を1回呼び、(応答, 発行SQL, S3に消しに行ったキー) を返す。"""
    calls: list = []
    deleted_keys: list = []
    with patch.object(documents_module, "get_conn", FakeConn(calls, rows, rowcount)):
        with patch.object(
            documents_module.storage,
            "delete_objects",
            lambda keys: deleted_keys.extend(keys) or len(keys),
        ):
            res = client.request("DELETE", "/documents", json={"ids": ids})
    return res, calls, deleted_keys


def test_empty_ids_is_rejected(client):
    """0件の指定は 400。何も消さずに「消えた」と読める応答を返さない。"""
    res, calls, keys = _delete(client, [], {})
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_ids"
    assert calls == []
    assert keys == []


def test_deletes_rows_chunks_and_files(client):
    """指定した行が消え、画像と原本がS3からも消える。"""
    res, calls, keys = _delete(
        client,
        [1, 2],
        {
            "SELECT id, source": [(1, "就業規則.txt"), (2, "旅費規程.pdf")],
            "image_path IS NOT NULL": [("images/旅費規程.pdf/0001.png",)],
            "SELECT DISTINCT source": [],
            "eval_questions": [],
        },
        rowcount=2,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] == 2
    assert body["sources"] == ["就業規則.txt", "旅費規程.pdf"]
    assert body["missing_ids"] == []

    # documents を消せば chunks は ON DELETE CASCADE で落ちる。
    # chunks を明示的に消すSQLを撃っていないこと自体を固定する
    # （撃っていたら、カスケードに頼らない別経路が増えたということ）。
    deletes = [sql for sql, _ in calls if sql.startswith("DELETE")]
    assert deletes == ["DELETE FROM documents WHERE id = ANY(%s)"]

    # 画像も原本も残さない
    assert keys == [
        "images/旅費規程.pdf/0001.png",
        "就業規則.txt",
        "旅費規程.pdf",
    ]


def test_missing_ids_are_reported_not_fatal(client):
    """存在しない id が混ざっても、あるものは消えて missing_ids に載る。"""
    res, _, _ = _delete(
        client,
        [1, 999],
        {
            "SELECT id, source": [(1, "就業規則.txt")],
            "image_path IS NOT NULL": [],
            "SELECT DISTINCT source": [],
            "eval_questions": [],
        },
        rowcount=1,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] == 1
    assert body["missing_ids"] == [999]


def test_keeps_original_when_same_name_remains(client):
    """同名の行がまだ残っているなら、原本はそのまま（残った行が使う）。

    二重登録の片方だけを消す場合。ここで原本まで消すと、残した行が
    「本文はあるのに原本が開けない」状態になる。
    """
    res, _, keys = _delete(
        client,
        [1],
        {
            "SELECT id, source": [(1, "就業規則.txt")],
            "image_path IS NOT NULL": [],
            # 消した後もまだ同名の行がある
            "SELECT DISTINCT source": [("就業規則.txt",)],
            "eval_questions": [],
        },
        rowcount=1,
    )
    assert res.status_code == 200, res.text
    assert keys == []
    assert res.json()["orphaned_questions"] == 0


def test_reports_orphaned_eval_questions(client):
    """正解ラベルが指す文書を消したら、宙に浮いた質問の件数を返す。"""
    res, _, _ = _delete(
        client,
        [1],
        {
            "SELECT id, source": [(1, "就業規則.txt")],
            "image_path IS NOT NULL": [],
            "SELECT DISTINCT source": [],
            "eval_questions": [("就業規則.txt", 3)],
        },
        rowcount=1,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["orphaned_questions"] == 3
    assert body["orphaned_sources"] == ["就業規則.txt"]
