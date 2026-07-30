"""比較評価: contextual retrieval の有無で検索精度がどう変わるかを数字で出す。

なぜ必要か:
  「チャンクに文脈を付けたら良くなった気がする」を数字に変えるための道具。
  取り込み方（contextual の有無）だけを変えて同じ質問集を2回評価し、
  Hit@k / MRR と、質問ごとの順位の変化を並べて出す。

  ★順位の他に「1位チャンクの本文」も突き合わせる★
    正解ラベルが文書単位の設問では、同じ文書の別チャンクに入れ替わっても
    順位は動かない。contextual retrieval が変えるのはまさにそこなので、
    順位だけを見ていると「差が無い」と「測れていない」を混同する。
    チャンク単位のラベル(expected_text)を整備すれば数値にも出る。

公平に測るための決めごと:
  1. 質問のベクトルは最初に1回だけ作り、両方の評価で使い回す。
     こうすると差が「文書側の作り方」だけに由来すると言い切れる
     （毎回作り直しても値は同じはずだが、それを仮定せずに済ませる）。
  2. 検索の手法・パラメータは両方で同一にする（引数はそのまま両方へ渡す）。
  3. 取り込み直すのは seed_docs/*.txt だけ。APIで別途入れた文書は両方の
     評価で同じ状態のまま残り、共通の妨害文書として働く。

★DBを書き換える★:
  seed_docs の文書を2回取り込み直す（既存の同名文書は置き換わる）。
  最後は設定(USE_CONTEXTUAL_CHUNKING)と同じ状態で終わるように順番を決めるので、
  実行後のDBは設定どおりの内容になる。

使い方:
  python -m app.compare                     # DBの全質問で比較（seed_docs）
  python -m app.compare --project 社内規程   # プロジェクトで絞って比較
  python -m app.compare --top-k 4 --retrievers vector,bm25
  python -m app.compare --corpus            # 評価専用コーパスで比較

★どのコーパスで測るかで結論が変わる★
  seed_docs は4文書12チャンクしかなく、Hit@4 が天井に張り付くため、contextual の
  有無で差が出ない（実測で ±0.000、1位チャンクも1問も動かなかった）。
  「効果がない」のではなく「測れていない」。
  --corpus は数百チャンクの評価専用の文書群で測る（app.seed の CORPUS_DIR）。
  こちらは取り込み直しに時間とAPIの費用がかかるので、既定にはしていない。
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from app.config import TOP_K, USE_CONTEXTUAL_CHUNKING
from app.db import init_db
from app.eval import evaluate, load_questions
from app.ingest import ingest_text
from app.llm import embed_texts
from app.retrieval import resolve_retrievers
from app.seed import (
    CORPUS_DIR,
    CORPUS_PROJECT,
    CORPUS_SCOPES_PATH,
    RETRY_WAITS,
    SEED_DIR,
    load_scopes,
)

LABELS = {False: "文脈なし（見出しのみ）", True: "contextual あり"}


def reingest(
    contextual: bool,
    seed_dir: Path | None = None,
    scopes_path: Path | None = None,
) -> int:
    """対象ディレクトリの文書を、指定した contextual 設定で取り込み直す。

    チャンク総数を返す。区分(project/topic)は app.seed と同じ documents.json から
    読む。取り込みは既存の同名文書を削除してから入れ直すので、ここで区分を渡さないと
    `task seed` で付けた区分がこのCLIを実行するたびに NULL で消える。

    scopes_path: 区分のマニフェスト。ディレクトリを差し替えるときは★必ず対になる
      マニフェストも渡す★（渡さないと評価コーパスの文書が区分なしで入り、
      project で絞った評価から丸ごと外れて「質問が見つかりません」になる）。
    """
    scopes = load_scopes(scopes_path)
    total = 0
    for path in sorted((seed_dir or SEED_DIR).glob("*.txt")):
        scope = scopes.get(path.name, {})
        result = ingest_text(
            source=path.name,
            text=path.read_text(encoding="utf-8"),
            project=scope.get("project"),
            topic=scope.get("topic"),
            contextual=contextual,
            embed_retry_waits=RETRY_WAITS,
        )
        total += result["chunks_created"]
    return total


def compare(
    top_k: int = TOP_K,
    retrievers: list[str] | None = None,
    gold: list[dict] | None = None,
    seed_dir: Path | None = None,
    scopes_path: Path | None = None,
) -> dict:
    """contextual なし/あり の2構成で評価し、両方のレポートを返す。

    戻り値: {"gold": [...], "runs": {False: report, True: report}}

    seed_dir / scopes_path: 取り込み直す文書の置き場所とその区分のマニフェスト
      （未指定なら seed_docs）。評価専用コーパスで測るときに差し替える。
    """
    gold = load_questions() if gold is None else gold
    if not gold:
        return {"gold": [], "runs": {}}

    # 質問ベクトルは1回だけ。ベクトル検索を使わない構成では作らない。
    query_vecs = None
    if "vector" in resolve_retrievers(retrievers):
        query_vecs = embed_texts(
            [g["question"] for g in gold], input_type="query", retry_waits=RETRY_WAITS
        )

    # 設定と同じ構成を最後に回す ＝ 実行後のDBが設定どおりの状態で残る
    order = [False, True] if USE_CONTEXTUAL_CHUNKING else [True, False]

    runs: dict[bool, dict] = {}
    for contextual in order:
        print(f"\n▶ {LABELS[contextual]} で取り込み直しています…", flush=True)
        chunks = reingest(contextual, seed_dir=seed_dir, scopes_path=scopes_path)
        report = evaluate(
            top_k=top_k,
            retrievers=retrievers,
            gold=gold,
            query_vecs=query_vecs,
        )
        report["chunks_created"] = chunks
        runs[contextual] = report

    return {"gold": gold, "runs": runs}


def _rank_label(rank: int | None) -> str:
    return "圏外" if rank is None else f"{rank + 1}位"


# 1位チャンクの本文をどこまで出すか。差が読めれば十分なので頭だけ出す。
EXCERPT_CHARS = 48


def _top_chunk(result: dict) -> str:
    """その質問で1位に来たチャンクの本文（無ければ空文字）。

    evaluate() が results[].contexts に引いたチャンク本文を持っているので、
    その先頭が1位のチャンク。
    """
    contexts = result.get("contexts") or []
    return contexts[0] if contexts else ""


def _excerpt(text: str) -> str:
    """1行に収まる抜粋にする（改行をつぶして頭だけ）。"""
    flat = " ".join(text.split())
    if not flat:
        return "(なし)"
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS] + "…"


def print_comparison(outcome: dict) -> None:
    """2構成の結果を並べて出力する。"""
    runs = outcome["runs"]
    if not runs:
        print("評価用の質問がありません。`task seed` で投入してください。")
        return

    before, after = runs[False], runs[True]
    k = before["top_k"]

    print(f"\n{'=' * 66}")
    print(f"contextual retrieval の効果  N={before['n']}  top_k={k}")
    print(f"{'=' * 66}")
    print(f"{'':<24}{'文脈なし':>12}{'あり':>12}{'差':>12}")
    for name, key in (("Hit@%d" % k, "hit_at_k"), ("MRR", "mrr")):
        b, a = before[key], after[key]
        print(f"{name:<24}{b:>12.3f}{a:>12.3f}{a - b:>+12.3f}")
    print(
        f"{'チャンク数':<21}"
        f"{before['chunks_created']:>12}{after['chunks_created']:>12}"
    )

    # 何かが動いた質問だけを出す（変化なしの行で埋めない）
    #
    # ★順位だけを見ると変化を見落とす★
    #   正解ラベルが文書単位（expected_text なし）の設問では、同じ文書の別の
    #   チャンクに入れ替わっても順位は動かない。contextual retrieval が変える
    #   のはまさにそこなので、順位が同じでも1位チャンクが入れ替わったなら出す。
    #   ラベルをチャンク単位にする前に「そもそも検索結果が動いているのか」を
    #   確かめられる（動いていなければ、疑うのはラベルではなく前置きの作り方）。
    print(f"\n{'-' * 66}")
    moved = 0
    for b, a in zip(before["results"], after["results"]):
        rank_changed = b["rank"] != a["rank"]
        top_before, top_after = _top_chunk(b), _top_chunk(a)
        top_changed = top_before != top_after
        if not rank_changed and not top_changed:
            continue
        moved += 1
        if rank_changed:
            # rank は 0始まり・None=圏外。小さいほど良いので None を最下位に倒して比較
            worse = (a["rank"] if a["rank"] is not None else 10**9) > (
                b["rank"] if b["rank"] is not None else 10**9
            )
            mark = "▼ 悪化" if worse else "▲ 改善"
            print(
                f"{mark}  {_rank_label(b['rank'])} → {_rank_label(a['rank'])}  "
                f"{b['question']}"
            )
        else:
            print(f"= 同順位  1位チャンクが入れ替わり  {b['question']}")
        if top_changed:
            print(f"        1位(なし): {_excerpt(top_before)}")
            print(f"        1位(あり): {_excerpt(top_after)}")
    if moved == 0:
        print("順位も1位チャンクも変わった質問はありません。")
    print(f"{'-' * 66}")
    print(f"実行後のDBは「{LABELS[USE_CONTEXTUAL_CHUNKING]}」の状態で残っています。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="contextual retrieval の有無で検索精度を比較する"
    )
    parser.add_argument("--top-k", type=int, default=TOP_K, help="上位いくつを正解判定に使うか")
    parser.add_argument(
        "--retrievers",
        type=str,
        default=None,
        help="使う検索手法をカンマ区切りで指定（例: vector,bm25）。未指定は設定の既定",
    )
    parser.add_argument(
        "--project", type=str, default=None, help="このプロジェクトの質問だけで比較する"
    )
    parser.add_argument(
        "--topic", type=str, default=None, help="このトピックの質問だけで比較する"
    )
    parser.add_argument(
        "--corpus",
        action="store_true",
        help="評価専用コーパス(eval_corpus)で比較する（既定は seed_docs）。"
        "数百チャンクを2回取り込み直すため、時間とAPIの費用がかかる",
    )
    args = parser.parse_args()

    # 再試行やフォールバックは llm.py が WARNING に出す（app.seed と同じ方針）
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    init_db()

    names = (
        [n.strip() for n in args.retrievers.split(",") if n.strip()]
        if args.retrievers
        else None
    )
    # --corpus 指定時の既定の絞り込み。--project を明示したときはそちらを尊重する。
    project = args.project or (CORPUS_PROJECT if args.corpus else None)
    gold = load_questions(project=project, topic=args.topic)
    if not gold:
        scope = " / ".join(filter(None, [project, args.topic])) or "指定なし"
        seed_cmd = "python -m app.eval --seed" + (" --corpus" if args.corpus else "")
        print(
            f"評価用の質問が見つかりません（絞り込み: {scope}）。\n"
            f"まず `{seed_cmd}` で質問集を投入してください。"
        )
        return
    print_comparison(
        compare(
            top_k=args.top_k,
            retrievers=names,
            gold=gold,
            seed_dir=CORPUS_DIR if args.corpus else None,
            scopes_path=CORPUS_SCOPES_PATH if args.corpus else None,
        )
    )


if __name__ == "__main__":
    main()
