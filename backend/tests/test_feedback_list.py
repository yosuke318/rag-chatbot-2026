"""貯めた 👍/👎 を読み出すテスト（GET /feedback）。

この一覧は「👎を読んで直す」ためではなく★調べる場所を絞る★ための道具なので、
ここで固定するのは「絞り込みが効くこと」と「絞った結果の総数が分かること」。

  1. 区分・評価・期間で絞れること（指定しなかった軸は絞らない）
  2. total が★絞り込んだ後の総件数★で、返した件数ではないこと
     （offset でページを送るので、これが無いと次のページの有無が出せない）
  3. 列の並びと返すキーが対応していること（SELECT の順とdictの組み立ては
     手で揃えるしかなく、足したカラムが1つずれると全部ずれる）
  4. 評価した時点の区分が記録されること（POST /feedback）

DBは触らず get_conn を差し替えて発行SQLとパラメータを見る
（test_feedback_context.py と同じやり方）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import feedback  # noqa: E402
from app import main as main_module  # noqa: E402

# SELECT が並べる17列と同じ並びの1行。
ROW = (
    3,                                              # id
    "有給は?",                                       # question
    "10日です。[1]",                                 # answer
    ["有給休暇.txt"],                                # sources
    -1,                                             # rating
    "内容が間違っている",                             # reason
    "第5条の日数が違う",                              # comment
    datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),  # created_at
    "社内規程",                                      # project
    "労務",                                          # topic
    5,                                              # conversation_id
    42,                                             # message_id
    "vector,trgm",                                  # retriever
    4,                                              # top_k
    False,                                          # reranked
    [101, 203],                                     # chunk_ids
    1234,                                           # latency_ms
)


class FakeConn:
    """1回目の execute に COUNT、2回目に一覧の行を返す偽コネクション。

    件数と一覧を別々のSQLで撃つので、両方の発行を記録して見分けられるようにする。
    """

    def __init__(self, calls: list, total: int = 1, rows: list | None = None):
        self.calls = calls
        self.total = total
        self.rows = [ROW] if rows is None else rows

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        outer = self
        return type(
            "R",
            (),
            {
                "fetchone": lambda _s: (outer.total,),
                "fetchall": lambda _s: outer.rows,
            },
        )()


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def _get(client, query: str = "", **conn_kw) -> tuple[dict, list]:
    calls: list = []
    with patch.object(feedback, "get_conn", FakeConn(calls, **conn_kw)):
        res = client.get(f"/feedback{query}")
    assert res.status_code == 200, res.text
    return res.json(), calls


def _list_sql(calls: list) -> tuple[str, list]:
    """一覧側（COUNTではない方）のSQLと値。"""
    return calls[1]


# --- 返す形 -------------------------------------------------------------------


def test_returns_the_answer_with_the_context_it_was_given(client):
    """1行が★本文＋どういう条件で出た回答か★を揃えて返すこと。

    ここが揃っていないと、👎を開いても「何が起きたか」を確かめに行けない
    （元の会話にも、そのとき渡したチャンクにも辿れない）。
    """
    body, _ = _get(client)

    assert body["feedback"] == [
        {
            "id": 3,
            "question": "有給は?",
            "answer": "10日です。[1]",
            "sources": ["有給休暇.txt"],
            "rating": -1,
            # 選択肢から選んだ理由と自由記述は別々に返る（数えるものと読むもの）
            "reason": "内容が間違っている",
            "comment": "第5条の日数が違う",
            "created_at": "2026-08-10T09:00:00Z",
            "project": "社内規程",
            "topic": "労務",
            "conversation_id": 5,
            "message_id": 42,
            "retriever": "vector,trgm",
            "top_k": 4,
            "reranked": False,
            # 並びがそのまま順位。ここが崩れると「👎のとき正解は何位に居たか」
            # が分からなくなる。
            "chunk_ids": [101, 203],
            "latency_ms": 1234,
        }
    ]


def test_total_is_the_filtered_count_not_the_page_size(client):
    """total は絞り込んだ後の総件数。返した件数を入れるとページ送りが壊れる。"""
    body, calls = _get(client, "?limit=1", total=137)

    assert body["total"] == 137
    assert len(body["feedback"]) == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    # 総件数は COUNT で数える（返した行を数えても総数にはならない）
    assert "COUNT(*)" in calls[0][0]


def test_newest_first_with_undated_rows_last(client):
    """新しい順。日時を持たない古い行が先頭に来ない（DESC の既定は NULL が先頭）。"""
    _, calls = _get(client)
    sql, _ = _list_sql(calls)
    assert "ORDER BY f.created_at DESC NULLS LAST, f.id DESC" in sql


# --- 絞り込み -----------------------------------------------------------------


def test_no_filter_returns_everything(client):
    """指定が無ければ WHERE を付けない（空文字の区分で0件にならないこと含む）。"""
    _, calls = _get(client, "?project=&topic=")
    sql, params = _list_sql(calls)
    assert "WHERE" not in sql
    # 値は LIMIT / OFFSET の2つだけ
    assert params == [feedback.DEFAULT_LIMIT, 0]


def test_filters_by_rating_scope_and_period(client):
    """評価・区分・期間が同時に効くこと。区分はマスタの名前で比べる。"""
    _, calls = _get(
        client,
        "?rating=-1&project=社内規程&topic=労務"
        "&since=2026-08-01T00:00:00Z&until=2026-09-01T00:00:00Z",
    )
    sql, params = _list_sql(calls)

    assert "f.rating = %s" in sql
    assert "p.name = %s" in sql and "t.name = %s" in sql
    # 期間は since 以上 until 未満。両端を含めると月境界の1件が両方に出る。
    assert "f.created_at >= %s" in sql and "f.created_at < %s" in sql
    assert params[:3] == [-1, "社内規程", "労務"]
    assert params[-2:] == [feedback.DEFAULT_LIMIT, 0]
    # 区分なしの行を落とさないため、名前は LEFT JOIN 側から引く
    assert "LEFT JOIN projects p" in sql and "LEFT JOIN topics t" in sql


def test_count_and_list_use_the_same_filter(client):
    """COUNT と一覧が同じ条件で撃たれること。ずれると total と中身が食い違う。"""
    _, calls = _get(client, "?rating=1")
    count_sql, count_params = calls[0]
    list_sql, list_params = _list_sql(calls)

    assert "f.rating = %s" in count_sql
    assert count_params == [1]
    # 一覧側は同じ条件＋ページ送りの2値
    assert list_params == [1, feedback.DEFAULT_LIMIT, 0]
    assert "LIMIT" not in count_sql


def test_invalid_rating_is_rejected_instead_of_returning_nothing(client):
    """+1/-1 以外は 400。0件で返すと「👎が無い」と「指定間違い」が見分けられない。"""
    calls: list = []
    with patch.object(feedback, "get_conn", FakeConn(calls)):
        res = client.get("/feedback?rating=0")
    assert res.status_code == 400
    assert calls == []


# --- ページ送り ---------------------------------------------------------------


def test_limit_is_capped_and_reported(client):
    """上限超過は黙って丸める（表示件数の話で、呼び出し側の間違いではない）。

    丸めた値をそのまま返すので、画面は「何件取れたつもりか」を誤解しない。
    """
    body, calls = _get(client, f"?limit={feedback.MAX_LIMIT + 500}")
    _, params = _list_sql(calls)

    assert params[-2] == feedback.MAX_LIMIT
    assert body["limit"] == feedback.MAX_LIMIT


def test_negative_paging_does_not_reach_sql(client):
    """負の limit/offset は SQL に渡さない（Postgres が構文エラーで落ちる）。"""
    body, calls = _get(client, "?limit=-1&offset=-10")
    _, params = _list_sql(calls)

    assert params[-2] >= 1 and params[-1] == 0
    assert body["offset"] == 0


def test_offset_is_passed_through(client):
    _, calls = _get(client, "?limit=20&offset=40")
    sql, params = _list_sql(calls)
    assert "LIMIT %s OFFSET %s" in sql
    assert params[-2:] == [20, 40]


# --- 記録側（区分を残す） ------------------------------------------------------


def test_feedback_records_the_scope_it_was_asked_in(client):
    """評価時に選んでいた区分を残すこと。

    ★後から復元できない★ 会話は区分を持たず、文書名から逆算すると
    「区分を選ばずに聞いたら偶然その文書が出た」のと区別できない。
    区分別の👎率はこの列が無いと出せない。
    """
    calls: list = []
    with patch.object(main_module, "get_conn", FakeConn(calls)):
        with patch.object(main_module.scopes, "register", lambda p, t: (7, 8)):
            res = client.post(
                "/feedback",
                json={
                    "question": "有給は?",
                    "answer": "10日です。",
                    "rating": -1,
                    "project": "社内規程",
                    "topic": "労務",
                },
            )
    assert res.status_code == 200, res.text

    sql, params = calls[0]
    names = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    row = dict(zip(names, params))
    # 行に入るのは名前ではなくマスタの id（他の書き込み側と同じ）
    assert row["project_id"] == 7 and row["topic_id"] == 8


def test_scope_is_optional_on_feedback(client):
    """区分を選ばずに聞いた回答への評価も記録できる（NULL のまま）。"""
    calls: list = []
    with patch.object(main_module, "get_conn", FakeConn(calls)):
        res = client.post(
            "/feedback",
            json={"question": "有給は?", "answer": "10日です。", "rating": 1},
        )
    assert res.status_code == 200, res.text

    sql, params = calls[0]
    names = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    row = dict(zip(names, params))
    assert row["project_id"] is None and row["topic_id"] is None
