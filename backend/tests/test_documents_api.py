"""GET /documents（文書名の候補を返す）のテスト。

このAPIは「評価用の質問に付ける正解の文書名」をUIで選ばせるためのもの。
絞り込みが効かない・区分なしの文書が落ちるといった壊れ方をすると、UIは黙って
候補が減るだけで気付けず、結果として ★存在する文書を正解に指定できない★
（＝その設問が永久に不正解になる）ところまで繋がる。ここで形と分岐を固定する。

DBには繋がず、app.main.get_conn を差し替えて発行SQLとパラメータを見る
（test_eval_question_api.py と同じやり方）。
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
    """発行SQLを記録し、SELECT には渡した行を返す最小のコネクション。"""

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
        return type("R", (), {"fetchall": lambda self: rows})()


def _get(client, query: str, rows: list) -> tuple[dict, list]:
    calls: list = []
    with patch.object(main_module, "get_conn", _FakeConn(calls, rows)):
        res = client.get(f"/documents{query}")
    assert res.status_code == 200, res.text
    return res.json(), calls


def test_returns_source_with_scope(client):
    rows = [("就業規則.txt", "社内規程", "労務"), ("メモ.txt", None, None)]
    body, _ = _get(client, "", rows)

    assert body["documents"] == [
        {"source": "就業規則.txt", "project": "社内規程", "topic": "労務"},
        # 区分なしの文書（project_id/topic_id が NULL）も候補に残る。
        # LEFT JOIN を INNER にすると、共通文書だけ選べなくなる。
        {"source": "メモ.txt", "project": None, "topic": None},
    ]


def test_filters_by_project_and_topic(client):
    _, calls = _get(client, "?project=社内規程&topic=労務", [])

    sql, params = calls[0]
    assert "p.name = %s" in sql and "t.name = %s" in sql
    assert params == ["社内規程", "労務"]


def test_no_filter_has_no_where(client):
    _, calls = _get(client, "", [])

    sql, params = calls[0]
    assert "WHERE" not in sql
    assert params == []


@pytest.mark.parametrize("blank", ["", "%20%20"])
def test_blank_scope_is_not_a_filter(client, blank):
    """★空文字で絞り込まない★

    UIの「すべて」は空文字で届く。それを「空文字という名前の区分で絞る」と
    解釈すると常に0件になり、候補が消えた理由が画面から分からなくなる。
    """
    _, calls = _get(client, f"?project={blank}", [])

    assert "WHERE" not in calls[0][0]


def test_one_row_per_source(client):
    """同じ source が複数行あっても候補は1つ（DISTINCT ON）。

    documents.source は UNIQUE ではない（この機能より前のDBに同名の行が
    残っている可能性があるため）。重複したまま返すと、プルダウンに同じ名前が
    並んでどちらを選んでも同じ、という無意味な選択肢が出る。
    """
    _, calls = _get(client, "", [])

    sql, _ = calls[0]
    assert "DISTINCT ON (d.source)" in sql
    # DISTINCT ON は ORDER BY の先頭が一致していないとPostgresが受け付けない
    assert "ORDER BY d.source" in sql
