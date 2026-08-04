"""評価用質問が0件のとき GET /eval が 404 を返すテスト。

★なぜ200の空レポートをやめたか★
  以前は n=0 / Hit@k=0.000 の空レポートを 200 で返していた。これだと
  「測ったら0点だった」と「そもそも測る対象が無い」が同じ形で返るので、
  画面には 0.000 が並び★検索精度が悪いように見える★。データが無いことは
  HTTPステータスで表す。

★0件は2種類ある★
  全体で0件（まだ登録していない）と、区分で絞った結果0件（他の区分には在る）。
  次にすべきことが違う ─ 後者は「区分を外せば見られる」─ ので、error コードと
  文面を分ける。ここではその出し分けを固定する。

DBには繋がず、app.main.load_questions を差し替える。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_module  # noqa: E402

# evaluate に渡る形（run_eval は中身を見ないので最小限でよい）
_QUESTION = {"question": "有給は何日？", "expected_source": "有給休暇.txt"}


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def _get(client, query: str, loader):
    """load_questions を差し替えて GET /eval を叩く。呼ばれ方も返す。"""
    calls: list = []

    def fake_load_questions(project=None, topic=None):
        calls.append({"project": project, "topic": topic})
        return loader(project, topic)

    with patch.object(main_module, "load_questions", fake_load_questions):
        res = client.get(f"/eval{query}")
    return res, calls


def test_404_when_no_questions_at_all(client):
    res, _ = _get(client, "", lambda p, t: [])

    assert res.status_code == 404
    body = res.json()
    assert body["error"] == "no_eval_questions"
    # ★投入手段が本文に入っていること★ エラーだけ出して次の手が書いていないと、
    # 画面を見た人はここで詰まる。
    assert "task seed" in body["hint"]
    assert "python -m app.eval --seed" in body["hint"]
    assert "質問を追加" in body["hint"]


def test_404_in_scope_when_other_scopes_have_questions(client):
    """区分で絞って0件・ほかには在る → 「区分を外せ」と案内する別コード。"""
    res, calls = _get(
        client,
        "?project=社内規程",
        # 絞り込みありなら0件、絞り込み無し（project=None）なら1件
        lambda p, t: [] if p or t else [_QUESTION],
    )

    assert res.status_code == 404
    body = res.json()
    assert body["error"] == "no_eval_questions_in_scope"
    assert "区分" in body["hint"]
    # 絞り込み有り→無しの順で2回引いている（2回目が「他にはあるか」の確認）
    assert calls == [{"project": "社内規程", "topic": None}, {"project": None, "topic": None}]


def test_scoped_but_globally_empty_uses_the_plain_message(client):
    """区分で絞っていても、全体でも0件なら「まだ登録されていない」の方。

    ここを取り違えると、1件も登録していない人に「区分を外してください」と
    案内してしまい、外しても0件のままで堂々巡りになる。
    """
    res, _ = _get(client, "?project=社内規程", lambda p, t: [])

    assert res.status_code == 404
    assert res.json()["error"] == "no_eval_questions"


def test_no_extra_query_when_not_scoped(client):
    """絞り込んでいなければ、確認の2回目は撃たない（0件でも1回だけ）。"""
    _, calls = _get(client, "", lambda p, t: [])

    assert calls == [{"project": None, "topic": None}]


@pytest.mark.parametrize("blank", ["", "%20%20"])
def test_blank_scope_is_treated_as_no_scope(client, blank):
    """空文字の区分は「絞り込みなし」。他エンドポイントと同じ規則。

    ここで空文字を絞り込みとして扱うと、UIの「すべて」を選んだ人に
    「区分を外してください」（＝もう外れている）と案内してしまう。
    """
    res, calls = _get(client, f"?project={blank}", lambda p, t: [])

    assert res.json()["error"] == "no_eval_questions"
    assert calls == [{"project": None, "topic": None}]


def test_200_when_questions_exist(client):
    """1件でもあれば従来どおり評価して 200。"""
    with patch.object(main_module, "evaluate", lambda **kw: {
        "n": 1,
        "top_k": 4,
        "retrievers": None,
        "rerank": None,
        "rrf_k": None,
        "params": None,
        "hit_at_k": 1.0,
        "mrr": 1.0,
        "results": [],
    }):
        res, _ = _get(client, "", lambda p, t: [_QUESTION])

    assert res.status_code == 200
    assert res.json()["n"] == 1
