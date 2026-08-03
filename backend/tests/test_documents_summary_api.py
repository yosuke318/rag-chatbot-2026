"""GET /documents/summary（文書一覧画面に出す表）のテスト。

このAPIは「登録したつもりで入っていない」「区分が NULL のまま」「二重登録」に
★気づくため★のもの。壊れ方が黙るのが怖い種類の画面なので、次を固定する:

  - 絞り込み（project/topic）が効くこと・空文字は絞り込まないこと
  - 同名の行を潰さないこと（/documents と正反対。潰すと二重登録が見えなくなる）
  - チャンク0件の文書が行として残ること（LEFT JOIN。落とすと「入っていない」が
    そもそも一覧から消え、入っていないことに気づけなくなる）
  - 上限で打ち切ったら truncated=true（黙って切ると「これで全部」と読まれる）

DBには繋がず、app.main.get_conn を差し替えて発行SQLとパラメータを見る
（test_documents_api.py と同じやり方）。
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def _row(
    id_: int,
    source: str,
    project=None,
    topic=None,
    created_at=None,
    chunks: int = 3,
    images: int = 0,
    has_hash: bool = True,
):
    """SELECT の列順どおりのタプルを作る（順序を1か所に閉じ込める）。"""
    return (id_, source, project, topic, created_at, chunks, images, has_hash)


def _get(client, query: str, rows: list) -> tuple[dict, list]:
    calls: list = []
    with patch.object(main_module, "get_conn", _FakeConn(calls, rows)):
        res = client.get(f"/documents/summary{query}")
    assert res.status_code == 200, res.text
    return res.json(), calls


def test_returns_counts_and_scope(client):
    at = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(2, "就業規則.pdf", "社内規程", "労務", at, chunks=12, images=3),
        # 区分なしの文書。LEFT JOIN を INNER にすると共通文書が丸ごと消える。
        _row(1, "メモ.txt", None, None, at, chunks=1, images=0, has_hash=False),
    ]
    body, _ = _get(client, "", rows)

    assert body["truncated"] is False
    assert body["documents"] == [
        {
            "id": 2,
            "source": "就業規則.pdf",
            "project": "社内規程",
            "topic": "労務",
            "created_at": "2026-08-03T12:00:00Z",
            "chunk_count": 12,
            "image_chunk_count": 3,
            "has_content_hash": True,
        },
        {
            "id": 1,
            "source": "メモ.txt",
            "project": None,
            "topic": None,
            "created_at": "2026-08-03T12:00:00Z",
            "chunk_count": 1,
            "image_chunk_count": 0,
            "has_content_hash": False,
        },
    ]


def test_filters_by_project_and_topic(client):
    _, calls = _get(client, "?project=社内規程&topic=労務", [])

    sql, params = calls[0]
    assert "p.name = %s" in sql and "t.name = %s" in sql
    # 末尾は LIMIT（打ち切り判定のため既定+1）。区分の2つがその前に並ぶ。
    assert params[:2] == ["社内規程", "労務"]


def test_no_filter_has_no_where(client):
    _, calls = _get(client, "", [])

    sql, params = calls[0]
    assert "WHERE" not in sql
    assert params == [main_module.DOCUMENTS_LIMIT_DEFAULT + 1]


@pytest.mark.parametrize("blank", ["", "%20%20"])
def test_blank_scope_is_not_a_filter(client, blank):
    """★空文字で絞り込まない★

    UIの「すべて」は空文字で届く。既存エンドポイント（/documents /search）と
    同じ規則に揃っていないと、この画面だけ常に0件になる。
    """
    _, calls = _get(client, f"?project={blank}", [])

    assert "WHERE" not in calls[0][0]


def test_keeps_duplicate_sources(client):
    """★同名を潰さない★

    /documents は DISTINCT ON で1件に寄せるが、この画面では二重登録が
    見えることが目的。潰すと「同じ内容を別IDで2回入れた」に永久に気づけない。
    """
    rows = [_row(9, "就業規則.pdf"), _row(4, "就業規則.pdf")]
    body, calls = _get(client, "", rows)

    assert "DISTINCT" not in calls[0][0]
    assert [d["id"] for d in body["documents"]] == [9, 4]


def test_zero_chunk_document_is_kept(client):
    """チャンク0件の文書も1行として残る（LEFT JOIN chunks）。

    「登録したつもりで索引に載っていない」を見つけるのがこの列の役目なので、
    INNER JOIN にしてしまうと、探したい行だけが一覧から消える。
    """
    body, calls = _get(client, "", [_row(1, "空.pdf", chunks=0, images=0)])

    assert "LEFT JOIN chunks" in calls[0][0]
    assert body["documents"][0]["chunk_count"] == 0


def test_newest_first_with_nulls_last(client):
    """並びは新しい順。created_at が NULL の古い行は末尾へ送る。

    DESC の既定では NULL が先頭に来るので、日時不明の行が「一番新しい」
    位置に居座る。
    """
    sql, _ = _get(client, "", [])[1][0]

    assert "ORDER BY d.created_at DESC NULLS LAST" in sql


def test_truncated_when_over_limit(client):
    """limit を超えたら1件多く取った分を捨てて truncated=true。"""
    rows = [_row(i, f"{i}.txt") for i in range(3)]
    body, calls = _get(client, "?limit=2", rows)

    assert calls[0][1] == [3]  # limit+1 を要求している
    assert len(body["documents"]) == 2
    assert body["truncated"] is True


def test_not_truncated_when_exactly_limit(client):
    """ちょうど limit 件なら打ち切っていない（境界で誤検知しない）。"""
    rows = [_row(i, f"{i}.txt") for i in range(2)]
    body, _ = _get(client, "?limit=2", rows)

    assert len(body["documents"]) == 2
    assert body["truncated"] is False


@pytest.mark.parametrize(
    "given,expected",
    [
        (0, 1),  # 0件は指定として無意味なので最小の1へ寄せる
        (-5, 1),
        (main_module.DOCUMENTS_LIMIT_MAX + 1, main_module.DOCUMENTS_LIMIT_MAX),
    ],
)
def test_limit_is_clamped_not_rejected(client, given, expected):
    """範囲外の limit は 400 にせず黙って丸める。

    表示件数はUIの都合の値で、呼び出し側が直せる種類の間違いではない。
    ここでエラーにすると一覧が「壊れた」ように見えるだけで得がない。
    """
    _, calls = _get(client, f"?limit={given}", [])

    assert calls[0][1] == [expected + 1]
