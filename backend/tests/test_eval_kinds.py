"""チャンク種別まで下ろした正解判定と、比較評価の有意差検定のテスト。

ここが守っているのは1点:
  ★文書名だけを正解ラベルにすると、図表の索引方式を変えても数字が動かない★
  画像は本文と同じ文書に属するので、「その文書が1位」では本文で引けたのか
  画像で引けたのか区別が付かない。索引方式の比較評価が成立しなくなる。
"""
from __future__ import annotations

import pytest

eval_mod = pytest.importorskip("app.eval")

_matches = eval_mod._matches
_rank_of = eval_mod._rank_of
paired_bootstrap = eval_mod.paired_bootstrap
compare_reports = eval_mod.compare_reports


def _hit(source: str, image: bool = False) -> dict:
    return {
        "id": 1,
        "content": "…",
        "source": source,
        "image_path": "images/x/0001.png" if image else None,
    }


# ---------------------------------------------------------------------------
# 正解判定
# ---------------------------------------------------------------------------


def test_any_kind_accepts_either_chunk():
    """既定 'any' は従来どおり文書単位（既存の質問集の意味を変えない）。"""
    assert _matches(_hit("決算.pdf"), "決算.pdf", "any")
    assert _matches(_hit("決算.pdf", image=True), "決算.pdf", "any")


def test_image_kind_rejects_a_text_chunk_of_the_same_document():
    """★これが無いと案A/案Bを比較できない★

    同じ文書の本文チャンクが1位でも「画像を引けた」ことにはならない。
    """
    assert not _matches(_hit("決算.pdf"), "決算.pdf", "image")
    assert _matches(_hit("決算.pdf", image=True), "決算.pdf", "image")


def test_text_kind_rejects_an_image_chunk():
    assert _matches(_hit("決算.pdf"), "決算.pdf", "text")
    assert not _matches(_hit("決算.pdf", image=True), "決算.pdf", "text")


def test_kind_never_overrides_the_document_check():
    assert not _matches(_hit("別の文書.pdf", image=True), "決算.pdf", "image")


def test_rank_of_skips_wrong_kind_and_finds_the_later_image_chunk():
    """本文が上位に来ていても、画像設問では画像チャンクの順位を返す。"""
    hits = [
        _hit("決算.pdf"),                 # 0位: 本文（画像設問では不正解）
        _hit("他社.pdf", image=True),      # 1位: 別文書の画像
        _hit("決算.pdf", image=True),      # 2位: これが正解
    ]

    assert _rank_of(hits, "決算.pdf", "image") == 2
    assert _rank_of(hits, "決算.pdf", "text") == 0
    assert _rank_of(hits, "決算.pdf", "any") == 0


def test_rank_of_returns_none_when_only_the_wrong_kind_is_retrieved():
    assert _rank_of([_hit("決算.pdf")], "決算.pdf", "image") is None


def test_expected_kinds_are_the_documented_three():
    assert eval_mod.EXPECTED_KINDS == ("any", "text", "image")


# ---------------------------------------------------------------------------
# 種類別の集計
# ---------------------------------------------------------------------------


def test_summarize_splits_metrics_per_kind():
    results = [
        {"expected_kind": "text", "hit": True, "reciprocal_rank": 1.0},
        {"expected_kind": "text", "hit": True, "reciprocal_rank": 0.5},
        {"expected_kind": "image", "hit": False, "reciprocal_rank": 0.0},
        {"expected_kind": "image", "hit": True, "reciprocal_rank": 0.25},
    ]

    text = eval_mod._summarize([r for r in results if r["expected_kind"] == "text"], 4)
    image = eval_mod._summarize([r for r in results if r["expected_kind"] == "image"], 4)

    assert text == {"n": 2, "hit_at_k": 1.0, "mrr": 0.75}
    # ★全体平均(0.4375)に混ぜると図表側の弱さが見えなくなる★
    assert image == {"n": 2, "hit_at_k": 0.5, "mrr": 0.125}


def test_summarize_handles_empty_group():
    assert eval_mod._summarize([], 4) == {"n": 0, "hit_at_k": 0.0, "mrr": 0.0}


# ---------------------------------------------------------------------------
# paired bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_reports_no_difference_for_identical_runs():
    scores = [1.0, 0.5, 0.0, 0.25, 1.0]

    result = paired_bootstrap(scores, scores, samples=500)

    assert result["diff"] == 0.0
    assert result["p_value"] == 1.0  # 全標本で差0 → 両側とも100%
    assert result["n"] == 5


def test_bootstrap_finds_a_consistent_improvement_significant():
    """全問で改善していれば、少ない問数でも有意になる。"""
    baseline = [0.0] * 12
    variant = [1.0] * 12

    result = paired_bootstrap(baseline, variant, samples=2000)

    assert result["diff"] == 1.0
    assert result["p_value"] < 0.05
    assert result["ci_low"] > 0  # 信頼区間が0をまたがない


def test_bootstrap_calls_a_noisy_small_sample_inconclusive():
    """★これが本題★ 平均は上がっているが、ばらついていれば有意にならない。

    20問規模で Hit@k が少し動いた程度では「良くなった」と言えない、を数字で示す。
    """
    baseline = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
    variant = [0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0]  # 平均は +0.125

    result = paired_bootstrap(baseline, variant, samples=2000)

    assert result["diff"] > 0
    assert result["p_value"] > 0.05
    assert result["ci_low"] < 0 < result["ci_high"]  # 区間が0をまたぐ＝判断できない


def test_bootstrap_is_reproducible():
    """同じ入力なら同じ結果（再現できない検定は判断材料にできない）。"""
    a, b = [0.0, 1.0, 0.5, 0.2], [1.0, 0.0, 0.5, 0.9]

    assert paired_bootstrap(a, b, samples=500) == paired_bootstrap(a, b, samples=500)


def test_bootstrap_p_value_collapses_to_zero_on_a_tiny_sample():
    """★少ない設問のp値を信じてはいけない★を明文化する。

    3問すべてが同じ向きに動くと差のばらつきが消え、再標本化しても同じ値しか
    出ないので p=0（完全に有意）になる。有意なのではなく、偶然を排除できて
    いないだけ。だから _print_comparison は N<MIN_QUESTIONS_FOR_TEST を
    「判断不可」と表示する。
    """
    result = paired_bootstrap([0.0, 0.0, 0.0], [0.5, 0.5, 0.5], samples=500)

    assert result["p_value"] == 0.0
    assert result["ci_low"] == result["ci_high"]  # 区間が潰れている＝情報が無い
    assert result["n"] < eval_mod.MIN_QUESTIONS_FOR_TEST


def test_bootstrap_rejects_unpaired_input():
    with pytest.raises(ValueError):
        paired_bootstrap([1.0, 0.0], [1.0])


def test_bootstrap_handles_empty_input():
    assert paired_bootstrap([], [])["n"] == 0


# ---------------------------------------------------------------------------
# レポート同士の比較
# ---------------------------------------------------------------------------


def _report(rows: list[tuple[str, str, bool, float]]) -> dict:
    return {
        "results": [
            {
                "question": q,
                "expected_kind": kind,
                "hit": hit,
                "reciprocal_rank": rr,
            }
            for q, kind, hit, rr in rows
        ]
    }


def test_compare_reports_can_look_at_image_questions_only():
    """本文根拠の設問に薄められずに、図表の効果だけを取り出せる。"""
    baseline = _report(
        [
            ("本文の質問", "text", True, 1.0),
            ("図の質問1", "image", False, 0.0),
            ("図の質問2", "image", False, 0.0),
        ]
    )
    variant = _report(
        [
            ("本文の質問", "text", True, 1.0),
            ("図の質問1", "image", True, 1.0),
            ("図の質問2", "image", True, 1.0),
        ]
    )

    image_only = compare_reports(baseline, variant, kind="image")
    overall = compare_reports(baseline, variant)

    assert image_only["kind"] == "image"
    assert image_only["mrr"]["diff"] == 1.0    # 図の設問だけなら +1.0
    assert image_only["mrr"]["n"] == 2
    # 本文の設問を混ぜると差が 2/3 に薄まる（判断を誤らせる）
    assert overall["mrr"]["diff"] == pytest.approx(0.6667, abs=1e-4)


def test_print_comparison_flags_underpowered_and_unindexed_runs(capsys):
    """出力側の2つの歯止めを固定する。

    どちらも「数字は出ているのに読んではいけない」ケースで、黙って通すと
    結論を間違える。キー名(conditions)がずれたらここで落ちる。
    """
    comparison = {
        "conditions": {
            "caption": {
                "hit_at_k": 1.0,
                "mrr": 1.0,
                "by_kind": {"image": {"n": 3, "hit_at_k": 1.0, "mrr": 1.0}},
                "reindexed": {"images": 5, "indexed": 2},  # 3枚は索引できていない
            },
            "multimodal": {
                "hit_at_k": 1.0,
                "mrr": 0.5,
                "by_kind": {"image": {"n": 3, "hit_at_k": 1.0, "mrr": 0.5}},
                "reindexed": {"images": 5, "indexed": 5},
            },
        },
        "image_only": {
            "kind": "image",
            "mrr": {"diff": -0.5, "ci_low": -0.5, "ci_high": -0.5, "p_value": 0.0, "n": 3},
            "hit_at_k": {"diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_value": 1.0, "n": 3},
        },
        "overall": {
            "kind": "all",
            "mrr": {"diff": -0.375, "ci_low": -0.5, "ci_high": -0.125, "p_value": 0.005, "n": 4},
            "hit_at_k": {"diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_value": 1.0, "n": 4},
        },
    }

    eval_mod._print_comparison(comparison)
    out = capsys.readouterr().out

    assert "索引 2/5枚" in out
    assert "この比較結果は使えません" in out   # 索引できていない条件がある
    # p=0.0 でも「有意差あり」と書かない（N=3 は検定として成立しない）
    assert "判断不可（設問不足）" in out
    assert "有意差あり" not in out


def test_compare_reports_refuses_mismatched_question_sets():
    """違う質問集を比べると「対応のある比較」が成立しない。"""
    a = _report([("質問A", "image", True, 1.0)])
    b = _report([("質問B", "image", True, 1.0)])

    with pytest.raises(ValueError):
        compare_reports(a, b, kind="image")
