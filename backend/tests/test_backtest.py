"""チャート読解のバックテスト（5-4）のテスト。

この枠組みの存在意義は「当てにいく」ことではなく、**当たっていないことを
言えるようにする**こと。だからここで固定するのは主に、
  - ベースライン（画像を見ない戦略）を必ず置くこと
  - 読み取れなかったものを当て推量で埋めないこと
  - 件数が少ないときに有意だと言わないこと
の3点。
"""
from __future__ import annotations

import json

import pytest

backtest = pytest.importorskip("app.backtest")


def _case(image, outcome, note=None):
    return {"image": image, "outcome": outcome, "note": note}


@pytest.fixture
def no_io(monkeypatch):
    """S3と生成APIを止める。read_trend の戻り値はテストごとに差し替える。"""
    monkeypatch.setattr(
        backtest, "_load_image", lambda ref: f"image:{ref}"
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# ベースライン
# ---------------------------------------------------------------------------


def test_baseline_is_the_most_common_outcome():
    """★何も読まない戦略を必ず置く★

    上昇が7割の期間なら、画像を見ずに up と言い続けるだけで70%当たる。
    これを超えない限り「読解に情報がある」とは言えない。
    """
    cases = [_case("a", "up"), _case("b", "up"), _case("c", "down")]
    assert backtest._baseline_state(cases) == "up"


def test_reading_that_only_matches_the_baseline_shows_no_gain(no_io):
    """常に up と答えるだけの読解は、ベースラインとの差が0になる。"""
    no_io.setattr(backtest, "read_trend", lambda img: "up")
    cases = [_case("a", "up"), _case("b", "up"), _case("c", "down")]

    report = backtest.run_backtest(cases)

    assert report["accuracy"] == report["baseline_accuracy"]
    assert report["test"]["diff"] == 0.0


def test_a_perfect_reading_beats_the_baseline(no_io):
    outcomes = ["up", "up", "down", "flat"]
    no_io.setattr(
        backtest, "read_trend", lambda img, it=iter(outcomes): next(it)
    )
    cases = [_case(str(i), o) for i, o in enumerate(outcomes)]

    report = backtest.run_backtest(cases)

    assert report["accuracy"] == 1.0
    assert report["test"]["diff"] > 0


# ---------------------------------------------------------------------------
# 読み取れなかったものの扱い
# ---------------------------------------------------------------------------


def test_unreadable_charts_are_skipped_not_guessed(no_io):
    """★当て推量で埋めない★

    埋めると、読めていないものを的中/不的中として数えることになり、
    的中率が意味を失う。
    """
    no_io.setattr(
        backtest, "read_trend", lambda img, it=iter(["up", None, "down"]): next(it)
    )
    cases = [_case("a", "up"), _case("b", "up"), _case("c", "down")]

    report = backtest.run_backtest(cases)

    assert report["n"] == 2          # 読めた2件だけで測る
    assert report["skipped"] == 1
    assert report["accuracy"] == 1.0  # 読めた分は全部当たり


def test_missing_images_are_skipped(monkeypatch):
    monkeypatch.setattr(backtest, "_load_image", lambda ref: None)
    monkeypatch.setattr(backtest, "read_trend", lambda img: "up")

    report = backtest.run_backtest([_case("消えた画像", "up")])

    assert report["n"] == 0 and report["skipped"] == 1


def test_empty_input_does_not_crash():
    report = backtest.run_backtest([])
    assert report["n"] == 0
    assert report["accuracy"] == 0.0


# ---------------------------------------------------------------------------
# 出力（結論の言い方）
# ---------------------------------------------------------------------------


def test_small_sample_is_reported_as_inconclusive(no_io, capsys):
    """★件数が少ないときに「有意」と言わない★

    3件が全部当たれば p は0に張り付くが、それは偶然を排除できていないだけ。
    """
    no_io.setattr(backtest, "read_trend", lambda img: "up")
    cases = [_case("a", "up"), _case("b", "up"), _case("c", "down")]

    backtest._print_report(backtest.run_backtest(cases))
    out = capsys.readouterr().out

    assert "判断不可（件数不足）" in out
    assert "ベースラインを有意に上回った" not in out


def test_report_always_states_what_is_being_measured(no_io, capsys):
    """予測の性能評価だと誤読されないよう、毎回書く。"""
    no_io.setattr(backtest, "read_trend", lambda img: "up")

    backtest._print_report(backtest.run_backtest([_case("a", "up")]))
    out = capsys.readouterr().out

    assert "売買判断の性能ではなく" in out
    assert "ベースライン" in out


def test_report_shows_the_baseline_strategy_next_to_the_score(no_io, capsys):
    no_io.setattr(backtest, "read_trend", lambda img: "up")

    backtest._print_report(backtest.run_backtest([_case("a", "up"), _case("b", "up")]))
    out = capsys.readouterr().out

    assert "画像を見ず常に「up」と答える戦略" in out


# ---------------------------------------------------------------------------
# データの読み込み
# ---------------------------------------------------------------------------


def test_missing_fixture_is_treated_as_no_data(tmp_path):
    """データが無い状態で黙って0点を出さない（CLI側が案内を出す）。"""
    assert backtest.load_cases(tmp_path / "none.json") == []


def test_cases_are_loaded_from_json(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps([{"image": "a.png", "outcome": "up", "note": "n"}]),
        encoding="utf-8",
    )

    assert backtest.load_cases(path) == [
        {"image": "a.png", "outcome": "up", "note": "n"}
    ]


def test_the_shipped_example_matches_the_documented_schema():
    """同梱の雛形が、実装が読める形からずれていないこと。"""
    from pathlib import Path

    example = (
        Path(backtest.__file__).resolve().parent.parent
        / "seed_data"
        / "chart_backtest.example.json"
    )
    cases = json.loads(example.read_text(encoding="utf-8"))

    assert cases
    for c in cases:
        assert set(c) == {"image", "outcome", "note"}
        assert c["outcome"] in backtest.TREND_STATES
        assert c["note"], "「その後」の判定条件は note に必ず書く（揃えないと平均が無意味になる）"
