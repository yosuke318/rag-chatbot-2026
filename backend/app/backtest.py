"""チャート読解のバックテスト。★「価値を主張しない」ための道具★

なぜ要るか:
  LLM は必ず「もっともらしいテクニカル分析文」を書く。読めているように見えることと、
  その読解に情報があることは別で、チャート形状からの将来予測はそもそも予測力が
  限定的。LLM を挟んでも予測力は上がらない。
  だからこの機能については、**測るまで価値を主張しない**。それを測る枠組みがこれ。

何を測るか:
  過去のチャート画像（ある時点まで）と、その★あと★に実際どう動いたかのペアを用意し、

    1. 画像から「観察される直近の傾き」を読ませる（up/down/flat。予測ではなく観察）
    2. その観察が、その後の実際の値動きの向きとどれくらい一致したかを数える
    3. ★ベースラインと比べる★ … いつも同じ向きを答える戦略（最頻の結果）と
       比較する。ここを飛ばすと「的中率55%」が偉く見えるが、結果の55%が上昇なら
       何も読まずに「up」と言い続けても55%になる。差が無ければ読解に情報は無い
    4. 差が偶然の範囲かを paired bootstrap で検定する（app.eval と同じ手続き）

  ★これは売買判断の性能評価ではない★
    測っているのは「観察した傾きがその後も続いたか」だけ。当たっていたとしても
    それは売買の助言にはならないし、この結果をもって売買判断機能を作ることもしない
    （スコープの理由は app.charts の冒頭を参照）。

データの用意:
  backend/seed_data/chart_backtest.json に (画像, その後の動き) を並べる。
  画像は S3 キー（取り込み済みの文書内画像）かローカルパスで指定する。

    [
      {
        "image": "images/月次レポート.pdf/0003.png",   // S3キー or ローカルパス
        "outcome": "up",                                // その後 実際にどう動いたか
        "note": "2026-02 月次レポート p3 / 翌月末までの終値比較"
      }
    ]

  outcome は up / down / flat。「その後」の定義（何日後まで・何%で flat とするか）は
  データを作る側の決めごとなので、note に必ず書いて揃えること。ここを揃えないと
  的中率の意味が問題ごとに変わり、平均に意味が無くなる。

使い方:
  python -m app.backtest                       # fixture で測る
  python -m app.backtest --file path/to.json   # 別のデータで測る
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from app import storage
from app.charts import TREND_STATES, read_trend
from app.eval import MIN_QUESTIONS_FOR_TEST, paired_bootstrap
from app.llm import ANSWER_IMAGE_MEDIA_TYPES, ImageContext

logger = logging.getLogger(__name__)

BACKTEST_PATH = (
    Path(__file__).resolve().parent.parent / "seed_data" / "chart_backtest.json"
)


def load_cases(path: Path | None = None) -> list[dict]:
    """(画像, その後の動き) の一覧を読む。ファイルが無ければ空。"""
    path = BACKTEST_PATH if path is None else path
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_image(ref: str) -> ImageContext | None:
    """S3キーかローカルパスから画像を読む。読めなければ None。

    S3 を先に見るのは、取り込み済み文書の画像をそのまま使うのが本来の流れだから
    （ローカルパスは手元で試すための逃げ道）。
    """
    obj = storage.get_object(ref)
    if obj is not None:
        data, media_type = obj
    else:
        path = Path(ref)
        if not path.exists():
            logger.warning("画像が見つかりません（この件は飛ばします）: %s", ref)
            return None
        data = path.read_bytes()
        media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    if media_type not in ANSWER_IMAGE_MEDIA_TYPES:
        logger.warning("扱えない画像形式です（飛ばします）: %s (%s)", ref, media_type)
        return None
    return ImageContext(data=data, media_type=media_type, label=ref)


def _baseline_state(cases: list[dict]) -> str:
    """ベースライン戦略が答え続ける向き（結果の最頻値）。

    ★「何も読まない戦略」を必ず置く★
      的中率は単独では読めない。上昇が7割の期間なら、画像を一切見ずに up と
      言い続けるだけで70%になる。読解に情報があると言うには、これを超える必要がある。
    """
    counts = {s: sum(1 for c in cases if c["outcome"] == s) for s in TREND_STATES}
    return max(counts, key=lambda s: counts[s])


def run_backtest(cases: list[dict] | None = None) -> dict:
    """各チャートを読ませ、その後の動きとの一致率をベースラインと比べる。

    戻り値は {"n", "skipped", "accuracy", "baseline_state", "baseline_accuracy",
              "test", "results"}。
    """
    cases = load_cases() if cases is None else cases
    baseline_state = _baseline_state(cases) if cases else "flat"

    results: list[dict] = []
    skipped = 0
    for case in cases:
        image = _load_image(case["image"])
        if image is None:
            skipped += 1
            continue
        observed = read_trend(image)
        if observed is None:
            # 1語で読み取れなかったものを当て推量で埋めない
            # （埋めると、読めていないものを的中/不的中として数えてしまう）
            logger.warning("傾きを読み取れませんでした（飛ばします）: %s", case["image"])
            skipped += 1
            continue
        results.append(
            {
                "image": case["image"],
                "observed": observed,
                "outcome": case["outcome"],
                "correct": observed == case["outcome"],
                "baseline_correct": baseline_state == case["outcome"],
                "note": case.get("note"),
            }
        )

    n = len(results)
    accuracy = sum(r["correct"] for r in results) / n if n else 0.0
    baseline_accuracy = sum(r["baseline_correct"] for r in results) / n if n else 0.0
    return {
        "n": n,
        "skipped": skipped,
        "accuracy": round(accuracy, 3),
        "baseline_state": baseline_state,
        "baseline_accuracy": round(baseline_accuracy, 3),
        # 「読解 - ベースライン」の差が偶然の範囲かを検定する（app.eval と同じ手続き）
        "test": paired_bootstrap(
            [float(r["baseline_correct"]) for r in results],
            [float(r["correct"]) for r in results],
        ),
        "results": results,
    }


def _print_report(report: dict) -> None:
    n = report["n"]
    t = report["test"]
    print(f"\n{'=' * 66}")
    print("チャート読解のバックテスト（観察した傾きが その後も続いたか）")
    print(f"{'=' * 66}")
    print(f"  N={n}  読み取れず除外={report['skipped']}")
    print(f"  読解の一致率     = {report['accuracy']:.3f}")
    print(
        f"  ベースライン     = {report['baseline_accuracy']:.3f}"
        f"  （画像を見ず常に「{report['baseline_state']}」と答える戦略）"
    )
    print(
        f"  差               = {t['diff']:+.4f}  "
        f"95%CI=[{t['ci_low']:+.4f}, {t['ci_high']:+.4f}]  p={t['p_value']:.4f}"
    )

    if n < MIN_QUESTIONS_FOR_TEST:
        verdict = "判断不可（件数不足）"
    elif t["p_value"] < 0.05 and t["diff"] > 0:
        verdict = "ベースラインを有意に上回った"
    else:
        verdict = "ベースラインと差があるとは言えない"
    print(f"  → {verdict}")

    print(f"\n{'=' * 66}")
    print("  ※ 測っているのは「観察した傾きがその後も続いたか」だけです。")
    print("     チャート形状からの将来予測はそもそも予測力が限定的で、")
    print("     LLMを挟んでも上がりません。この結果は売買判断の性能ではなく、")
    print("     ★この機能に予測の価値を主張してよいか★の判断材料です。")
    if n and n < MIN_QUESTIONS_FOR_TEST:
        print(f"     {MIN_QUESTIONS_FOR_TEST}件未満では p値が小さく出ても意味を持ちません。")
    print(f"{'=' * 66}\n")

    for r in report["results"]:
        mark = "○" if r["correct"] else "×"
        print(f"{mark} 観察={r['observed']:<5} 実際={r['outcome']:<5} {r['image']}")
        if r["note"]:
            print(f"    {r['note']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="チャート読解のバックテスト（的中率をベースラインと比較）"
    )
    parser.add_argument(
        "--file", type=str, default=None, help="(画像, その後の動き) のJSON。既定は fixture"
    )
    args = parser.parse_args()

    cases = load_cases(Path(args.file) if args.file else None)
    if not cases:
        print(
            "バックテスト用のデータがありません。\n"
            f"{BACKTEST_PATH} に (画像, その後の実際の動き) の組を用意してください。\n"
            "形式は app.backtest のドキュメントを参照。\n\n"
            "★データが無いうちは、この機能に予測の価値があるとは主張できません★"
        )
        return
    _print_report(run_backtest(cases))


if __name__ == "__main__":
    main()
