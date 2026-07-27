"""project / topic の空文字を NULL に正規化することのテスト（PR #6 レビュー指摘）。

NULL は「どこにも属さない共通」の意味なので、空文字が混ざると別物になる:
  - 登録側: 空欄が空文字で保存され、NULL 指定の絞り込みから漏れる
  - 検索側: `?project=` が「未指定」ではなく `project=''` での絞り込みになり常に0件
API境界(main.py)で潰す方針なので、テストもエンドポイント越しに書く。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402

_blank_to_none = main_module._blank_to_none


@pytest.fixture(scope="module")
def client():
    """DBに触らない TestClient。

    main.py は `from app.db import init_db` で名前を束縛しているので、
    `patch("app.db.init_db")` では差し替わらない（app.main 側の参照は元のまま）。
    実際に呼ばれる `app.main.init_db` を patch.object で差し替える。
    こうしておけば import 順に依存しない。
    """
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("　", None),  # 全角スペースのみ
        ("社内規程", "社内規程"),
        ("  社内規程  ", "社内規程"),  # 前後の空白は落とす
    ],
)
def test_blank_to_none(value, expected):
    assert _blank_to_none(value) == expected


def test_ingest_stores_blank_scope_as_null(client):
    """空欄で登録したら空文字ではなく NULL が渡る。"""
    with patch("app.main.ingest_text", return_value={"chunks_created": 1, "replaced": 0}) as m:
        res = client.post(
            "/ingest",
            json={"source": "a.txt", "text": "本文", "project": "  ", "topic": ""},
        )
    assert res.status_code == 200
    assert m.call_args.args[2] is None  # project
    assert m.call_args.args[3] is None  # topic


def test_ingest_trims_scope(client):
    with patch("app.main.ingest_text", return_value={"chunks_created": 1, "replaced": 0}) as m:
        client.post(
            "/ingest",
            json={"source": "a.txt", "text": "本文", "project": " 社内規程 ", "topic": " 労務 "},
        )
    assert m.call_args.args[2] == "社内規程"
    assert m.call_args.args[3] == "労務"


def test_ingest_file_stores_blank_scope_as_null(client):
    with patch("app.main.ingest_text", return_value={"chunks_created": 1, "replaced": 0}) as m, \
         patch("app.storage.save_bytes"):
        res = client.post(
            "/ingest-file",
            files={"file": ("a.txt", b"hello", "text/plain")},
            data={"project": "", "topic": "   "},
        )
    assert res.status_code == 200
    assert m.call_args.args[2] is None and m.call_args.args[3] is None


def test_eval_questions_empty_query_is_not_a_filter(client):
    """`?project=` は「未指定」＝絞り込まない（WHERE句を作らない）。"""
    rows = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            rows.append((sql, params))
            return type("R", (), {"fetchall": lambda self: []})()

    with patch("app.main.get_conn", FakeConn):
        res = client.get("/eval-questions?project=&topic=")

    assert res.status_code == 200
    sql, params = rows[0]
    assert "WHERE" not in sql
    assert params == []


def test_eval_questions_real_value_filters(client):
    rows = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            rows.append((sql, params))
            return type("R", (), {"fetchall": lambda self: []})()

    with patch("app.main.get_conn", FakeConn):
        client.get("/eval-questions?project=社内規程")

    sql, params = rows[0]
    assert "project = %s" in sql
    assert params == ["社内規程"]


def test_eval_empty_query_is_not_a_filter(client):
    """/eval も同じ（空クエリで質問0件にならない）。"""
    with patch("app.main.load_questions", return_value=[]) as m, \
         patch("app.main.evaluate", return_value={
             "n": 0, "top_k": 4, "retrievers": None, "rerank": None,
             "rrf_k": None, "params": None, "hit_at_k": 0.0, "mrr": 0.0, "results": [],
         }):
        res = client.get("/eval?project=&topic=")

    assert res.status_code == 200
    assert m.call_args.kwargs == {"project": None, "topic": None}
