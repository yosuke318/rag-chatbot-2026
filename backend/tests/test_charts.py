"""チャート読解のスコープ制限（5-4）のテスト。★この段はここが本体★

守りたいこと:
  この機能は「読解・集約の支援」であって売買判断は出さない。
  個別銘柄の売買判断を業として提供すると金融商品取引法の投資助言・代理業の
  登録が要る可能性が高く、登録なしにその設計へ踏み込まないため。

  防御は2段構え（app.charts 参照）。プロンプトで書かせないのが主だが、
  生成モデルの指示追従は確率的なので、漏れたときに気づける出力側の検査も置く。
  ここで固定するのは主に後者（前者は自動テストでは担保しきれない）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

charts = pytest.importorskip("app.charts")


# ---------------------------------------------------------------------------
# 出力側の検査
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "この水準は買い時と考えられます。",
        "今は売り推奨です。",
        "購入を検討してもよいでしょう。",
        "ここでエントリーするのが有効です。",
        "目標株価は3,200円です。",
        "来月は上昇するでしょう。",
        "今後の見通しは明るいです。",
        "現在の株価は割安です。",
        "絶好の買い場です。",
        "利確を考えるべき局面です。",
    ],
)
def test_advice_like_sentences_are_detected(sentence):
    assert charts.find_advice(sentence) == [sentence]


@pytest.mark.parametrize(
    "sentence",
    [
        "直近3か月は右肩上がりに推移しています。",
        "1,200円付近に複数回反発した水準が見られます。",
        "出来高は3月に増加しています。",
        "形状としてはダブルトップに見えます。",
        "画像からは期間の記載が読み取れません。",
        "移動平均線は25日線と75日線の2本が描かれています。",
    ],
)
def test_plain_readings_are_not_flagged(sentence):
    """★誤検知で正常な読解を壊さない★

    観察の記述まで落とすと、機能そのものが役に立たなくなる。
    """
    assert charts.find_advice(sentence) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "将来の値動きや目標株価は、このチャートからは読み取れません。",
        "売買判断は行いません。",
        "今後の見通しについてはお答えできません。",
        "目標株価は画像に記載されていません。",
        "サポートラインは描かれていません。",
        "将来の予想は行いません。",
    ],
)
def test_refusals_are_not_treated_as_advice(sentence):
    """★拒否文を落とさない★（実測で踏んだ誤検知）

    「目標株価は読み取れません」は禁止語を含むが、中身は正反対でまさに
    守れている文。ここを落とすと利用者に伝わるべき拒否そのものが消え、
    「除いて表示しています」の注記が空振りして、本当に漏れたときの注記まで
    信用されなくなる。
    """
    assert charts.find_advice(sentence) == []


def test_a_negated_recommendation_is_still_advice():
    """否定なら何でも許す、にはしない。これは売買判断そのもの。"""
    assert charts.find_advice("今は買うべきではありません。") != []


def test_strip_advice_removes_only_the_offending_sentence():
    """問題のある文だけを外す（全部捨てると「何も答えない機能」になる）。"""
    text = (
        "直近3か月は右肩上がりに推移しています。"
        "1,200円付近に反発の水準が見られます。"
        "この水準は買い時と考えられます。"
    )

    kept, removed = charts.strip_advice(text)

    assert removed == ["この水準は買い時と考えられます。"]
    assert "右肩上がり" in kept and "1,200円付近" in kept
    assert "買い時" not in kept


def test_strip_advice_keeps_clean_text_untouched():
    text = "直近3か月は右肩上がりに推移しています。"
    assert charts.strip_advice(text) == (text, [])


def test_advice_labels_reports_the_kind_of_leak():
    labels = charts.advice_labels("目標株価は3,200円で、買い推奨です。")
    assert "目標値" in labels and "売買推奨" in labels


# ---------------------------------------------------------------------------
# read_charts（プロンプト + 出力検査の組み合わせ）
# ---------------------------------------------------------------------------


def test_reading_always_carries_the_scope_notice(monkeypatch):
    """★毎回スコープを明示する★

    「売買判断ではない」と書いていない出力を利用者が投資判断に使うと、
    こちらの意図と関係なく助言として受け取られうる。
    """
    monkeypatch.setattr(
        charts, "generate_answer", lambda *a, **k: "直近は右肩上がりです [1]。"
    )

    result = charts.read_charts("この図はどういう状態?", ["ctx"])

    assert "売買判断は行いません" in result["reading"]
    assert result["removed"] == []


def test_reading_drops_advice_that_slipped_through_the_prompt(monkeypatch):
    """プロンプトが効かなかったときに出力側で止める（2段目の防御）。"""
    monkeypatch.setattr(
        charts,
        "generate_answer",
        lambda *a, **k: "直近は右肩上がりです。したがって買い推奨です。",
    )

    result = charts.read_charts("この図はどういう状態?", ["ctx"])

    assert "買い推奨" not in result["reading"]
    assert result["removed"] == ["したがって買い推奨です。"]
    assert "売買推奨" in result["labels"]
    # 消したことを黙らない（利用者は出力を全文だと思ってしまう）
    assert "除いて表示しています" in result["reading"]


def test_reading_uses_the_chart_system_prompt(monkeypatch):
    """通常の回答用ではなく、判断を禁じたプロンプトで生成させる。"""
    seen = {}

    def fake(question, contexts, system=None, **k):
        seen["system"] = system
        return "右肩上がりです。"

    monkeypatch.setattr(charts, "generate_answer", fake)
    charts.read_charts("q", ["ctx"])

    assert seen["system"] is charts.CHART_READING_SYSTEM_PROMPT
    assert "書いてはいけないこと" in seen["system"]


# ---------------------------------------------------------------------------
# read_trend（バックテスト用の観察）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("up", "up"), (" DOWN \n", "down"), ("flat。", "flat")])
def test_read_trend_normalizes_a_single_word(monkeypatch, raw, expected):
    monkeypatch.setattr(charts, "generate_answer", lambda *a, **k: raw)
    assert charts.read_trend(object()) == expected


def test_read_trend_returns_none_for_anything_else(monkeypatch):
    """★解釈できない出力を当て推量で埋めない★

    埋めると、読めていないものを的中/不的中として数えることになり、
    バックテストの数字が意味を失う。
    """
    monkeypatch.setattr(
        charts, "generate_answer", lambda *a, **k: "上昇トレンドだと思われます"
    )
    assert charts.read_trend(object()) is None


# ---------------------------------------------------------------------------
# /chart-read（API境界）
# ---------------------------------------------------------------------------

pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app import main as main_module

    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def _image_hit(hit_id=1):
    return {
        "id": hit_id,
        "content": "[画像] 3ページ目",
        "source": "月次レポート.pdf",
        "image_path": "images/月次レポート.pdf/0003.png",
        "context": "3ページ目",
    }


def _text_hit(hit_id=2):
    return {
        "id": hit_id,
        "content": "第1条 …",
        "source": "月次レポート.pdf",
        "image_path": None,
        "context": None,
    }


def test_chart_read_returns_a_reading_with_citations(client):
    from app import main as main_module

    with (
        patch.object(main_module, "hybrid_search", return_value=[_image_hit()]),
        patch.object(
            main_module.storage, "get_object", return_value=(b"raw", "image/png")
        ),
        patch.object(main_module.storage, "file_url", return_value="/files/x.png"),
        patch.object(
            main_module.charts,
            "read_charts",
            return_value={
                "reading": "直近は右肩上がりです [1]。\n\n" + main_module.charts.SCOPE_NOTICE,
                "removed": [],
                "labels": [],
            },
        ),
    ):
        resp = client.post("/chart-read", json={"question": "この図はどういう状態?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["charts_read"] == 1
    assert "売買判断は行いません" in body["reading"]
    assert body["citations"][0]["image_url"] == "/files/x.png"
    assert body["removed"] == []


def test_chart_read_ignores_text_chunks(client):
    """★根拠を画像だけに絞る★

    本文チャンクを混ぜると、チャートを読んだのか本文を読んだのか区別が
    付かない説明になる。
    """
    from app import main as main_module

    seen = {}

    def fake_read(question, contexts):
        seen["contexts"] = contexts
        return {"reading": "…", "removed": [], "labels": []}

    with (
        patch.object(
            main_module, "hybrid_search", return_value=[_text_hit(), _image_hit()]
        ),
        patch.object(
            main_module.storage, "get_object", return_value=(b"raw", "image/png")
        ),
        patch.object(main_module.storage, "file_url", return_value=None),
        patch.object(main_module.charts, "read_charts", fake_read),
    ):
        resp = client.post("/chart-read", json={"question": "この図はどういう状態?"})

    assert resp.status_code == 200
    assert resp.json()["charts_read"] == 1
    assert len(seen["contexts"]) == 1  # 本文チャンクは渡っていない


def test_chart_read_404s_when_no_image_was_retrieved(client):
    """★図が引けなければ答えない★

    テキストだけで作った説明をチャート読解として返すと、利用者は図を読んだ
    結果だと受け取ってしまう。
    """
    from app import main as main_module

    with patch.object(main_module, "hybrid_search", return_value=[_text_hit()]):
        resp = client.post("/chart-read", json={"question": "この図はどういう状態?"})

    assert resp.status_code == 404
    assert resp.json()["error"] == "no_chart_found"


def test_chart_read_rejects_an_empty_question(client):
    resp = client.post("/chart-read", json={"question": "   "})
    assert resp.status_code == 400


def test_chart_read_is_not_exposed_on_the_public_api(client):
    """★社外向けAPI(/v1)には載せない★

    個別銘柄の売買判断を業として提供すると投資助言・代理業の登録が要る
    可能性が高い。社外へ返す経路自体を作らない、というのが留保への対処。
    """
    from app import main as main_module

    # ルータを include した分もあるので、公開されている経路は OpenAPI から見る
    paths = set(main_module.app.openapi()["paths"])

    assert "/chart-read" in paths                       # 社内向けには在る
    assert not [p for p in paths if p.startswith("/v1") and "chart" in p]
    # /v1 に何かは載っている（そもそも空で通ってしまうテストにしない）
    assert [p for p in paths if p.startswith("/v1")]
