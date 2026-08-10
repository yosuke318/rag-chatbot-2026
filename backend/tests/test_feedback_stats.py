"""👎率の集計のテスト（GET /feedback/stats）。

この画面が答えるのは「👎が何件あるか」ではなく★どこを見に行くか★なので、
そこに効く性質だけを固定する:

  1. 率を出すのに分母が要る＝rating で絞らない（👎だけ数えても多寡が言えない）
  2. 0件の区分の率は null（0.0 にすると「👎ゼロ＝良い」と読めてしまう）
  3. 区分別・時系列・理由別を同じ絞り込みで数える（切り口ごとにズレない）
  4. 時系列の刻みは決まった単位だけ（SQLに文字列で入るため）

DBは触らず get_conn を差し替えて発行SQLとパラメータを見る。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import feedback  # noqa: E402
from app import main as main_module  # noqa: E402

AUG1 = datetime(2026, 8, 1, tzinfo=timezone.utc)


class FakeConn:
    """stats が撃つ4本（全体・区分別・時系列・理由別）に順に答える偽コネクション。"""

    def __init__(self, calls: list, total=(10, 4), scope=None, period=None, reason=None):
        self.calls = calls
        self.total = total
        self.scope = scope if scope is not None else [("社内規程", "労務", 6, 4)]
        self.period = period if period is not None else [(AUG1, 10, 4)]
        self.reason = reason if reason is not None else [("情報が古い", 3), (None, 1)]

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        outer = self
        n = len(self.calls)
        rows = {2: outer.scope, 3: outer.period, 4: outer.reason}.get(n, [])
        return type(
            "R",
            (),
            {"fetchone": lambda _s: outer.total, "fetchall": lambda _s: rows},
        )()


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def _get(client, query: str = "", **conn_kw) -> tuple[dict, list]:
    calls: list = []
    with patch.object(feedback, "get_conn", FakeConn(calls, **conn_kw)):
        res = client.get(f"/feedback/stats{query}")
    assert res.status_code == 200, res.text
    return res.json(), calls


# --- 率の出し方 ---------------------------------------------------------------


def test_rate_needs_a_denominator(client):
    """👎率は「👎 ÷ 全評価」。全体も区分別も件数を添えて返す。"""
    body, _ = _get(client)

    assert body["total"] == 10
    assert body["down"] == 4
    assert body["down_rate"] == 0.4
    assert body["by_scope"] == [
        {
            "project": "社内規程",
            "topic": "労務",
            "total": 6,
            "down": 4,
            "down_rate": 4 / 6,
        }
    ]


def test_rate_is_null_when_nothing_was_rated(client):
    """0件の区分は率 null。0.0 にすると「👎ゼロ＝良い区分」と読めてしまう。"""
    body, _ = _get(client, total=(0, 0), scope=[(None, None, 0, 0)])

    assert body["down_rate"] is None
    assert body["by_scope"][0]["down_rate"] is None


def test_rating_cannot_be_filtered_away(client):
    """★rating で絞らない★ 分母が消えると率が出せなくなる。"""
    _, calls = _get(client, "?rating=-1")
    # 未知のクエリは無視される。どのSQLにも rating の絞り込みが入らないこと
    # （理由別だけは「👎の内訳」なので -1 で絞る。そこは分母に使わない）。
    for sql, _params in calls[:3]:
        assert "f.rating = %s" not in sql


def test_reason_breakdown_counts_only_thumbs_down(client):
    """理由は👎にしか付かないので、内訳だけは👎に絞って数える。"""
    body, calls = _get(client)
    reason_sql, _ = calls[3]
    assert "f.rating = -1" in reason_sql
    # 理由を選ばなかった👎も1行として出す（落とすと収集率を見誤る）
    assert body["by_reason"] == [
        {"reason": "情報が古い", "count": 3},
        {"reason": None, "count": 1},
    ]


# --- 切り口をまたいだ一貫性 ----------------------------------------------------


def test_every_breakdown_uses_the_same_filter(client):
    """区分・期間の絞り込みが4本すべてに同じ形で効くこと。"""
    _, calls = _get(
        client,
        "?project=社内規程&since=2026-08-01T00:00:00Z&until=2026-09-01T00:00:00Z",
    )
    for sql, params in calls:
        assert "p.name = %s" in sql
        assert "f.created_at >= %s" in sql and "f.created_at < %s" in sql
        assert params[:1] == ["社内規程"] or params[:2] == ["day", "社内規程"]


def test_undated_rows_are_left_out_of_the_timeline(client):
    """日時を持たない古い行は時系列に置けない（その1点だけ率が狂う）。"""
    _, calls = _get(client)
    period_sql, params = calls[2]
    assert "f.created_at IS NOT NULL" in period_sql
    # 刻みは SQL に文字列で入るので、値として渡していること
    assert params[0] == "day"
    assert "date_trunc(%s" in period_sql


def test_scope_breakdown_is_sorted_by_rate(client):
    """探すのは「この区分だけ突出している」なので、率の高い順に並べる。"""
    _, calls = _get(client)
    scope_sql, _ = calls[1]
    assert "GROUP BY p.name, t.name" in scope_sql
    assert "ORDER BY" in scope_sql and "DESC" in scope_sql


# --- 刻み ---------------------------------------------------------------------


@pytest.mark.parametrize("bucket", ["day", "week", "month"])
def test_supported_buckets(client, bucket):
    body, calls = _get(client, f"?bucket={bucket}")
    assert body["bucket"] == bucket
    assert calls[2][1][0] == bucket


def test_unknown_bucket_is_rejected(client):
    """date_trunc の単位は SQL に文字列で入るので、決めた語だけ通す。"""
    calls: list = []
    with patch.object(feedback, "get_conn", FakeConn(calls)):
        res = client.get("/feedback/stats?bucket=fortnight")
    assert res.status_code == 400
    assert res.json()["error"] == "unknown_bucket"
    assert calls == []
