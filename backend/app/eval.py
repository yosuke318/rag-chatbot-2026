"""評価: 検索がどれだけ正解チャンクを拾えているかを数字で測る。★改良の土台★

なぜ必要か:
  「リランクを入れた」「chunkサイズを変えた」等の改良が本当に効いたのかは、
  体感では分からない。同じ質問集を毎回流し、正解率が上がったかで判定する。
  これが無いと以降の改良はすべて「良くなった気がする」で終わる。

測るもの（検索側）:
  - Hit@k : 上位k件の中に正解文書が入っていた質問の割合（拾えたか）
  - MRR   : 正解文書が何位に来たかの逆数の平均（どれだけ上位に置けたか）
            例) 正解が1位なら 1.0、2位なら 0.5、圏外なら 0。1.0に近いほど良い。

  ★Anthropicキーは不要★（検索のベクトル化で VOYAGE_API_KEY だけ要る）。
  vector を外して trgm/bm25 だけにすれば埋め込みも呼ばないのでキー無しで動く。

測るもの（回答側・オプション）:
  --gen を付けたときだけ実際に回答生成まで走らせて目視確認する。
  こちらは ANTHROPIC_API_KEY が要る。忠実性の自動採点(LLM-judge/Ragas)は次段。

使い方:
  python -m app.eval                              # 既定の手法で検索を評価
  python -m app.eval --retrievers vector,bm25     # 手法を変えて比較
  python -m app.eval --top-k 4 --rerank           # 上位件数やリランクの有無を変える
  python -m app.eval --gen                        # 回答生成まで走らせて目視（要Anthropic）

正解データ(GOLD)は seed_docs の内容に紐づく。文書を差し替えたらここも直すこと。
"""
from __future__ import annotations

import argparse

from app.config import TOP_K
from app.llm import generate_answer
from app.retrieval import hybrid_search


# --- 評価用QA集 ---------------------------------------------------------------
# expected_source: この質問に答えられる根拠が入っている文書（正解ラベル）。
# note           : 何を確かめる質問かのメモ（人間向け。採点には使わない）。
# seed_docs/有給休暇.txt・経費精算.txt の記述だけで答えられる問いにしてある。
GOLD: list[dict] = [
    {
        "question": "有給休暇は入社してからどれくらいで何日もらえる？",
        "expected_source": "有給休暇.txt",
        "note": "付与条件（6か月・8割出勤で10日）を含む文書を引けるか",
    },
    {
        "question": "使いきれなかった有給は翌年に繰り越せる？上限は？",
        "expected_source": "有給休暇.txt",
        "note": "繰り越し上限20日。『繰り越し』という語の一致が効くか",
    },
    {
        "question": "経費はいつまでに申請すればいい？",
        "expected_source": "経費精算.txt",
        "note": "締切（発生月の翌月10日）を含む文書を引けるか",
    },
    {
        "question": "領収書が必要になるのはいくらから？",
        "expected_source": "経費精算.txt",
        "note": "5,000円以上という金額条件。数字と単位の字面一致",
    },
    {
        "question": "接待でお金を使うとき事前に何が必要？",
        "expected_source": "経費精算.txt",
        "note": "事前承認。言い換え（接待交際費↔接待）に意味検索が効くか",
    },
]


def _rank_of(hits: list[dict], expected_source: str) -> int | None:
    """検索結果の中で expected_source が最初に現れた順位（0始まり）。無ければ None。"""
    for i, h in enumerate(hits):
        if h["source"] == expected_source:
            return i
    return None


def evaluate(
    top_k: int = TOP_K,
    retrievers: list[str] | None = None,
    rerank: bool | None = None,
    gold: list[dict] | None = None,
) -> dict:
    """GOLD を1問ずつ検索にかけ、Hit@k と MRR を集計して返す。

    各問について hybrid_search を実行し、正解文書が上位 top_k に入ったか・
    何位だったかを記録する。ここでは回答生成はしないが、--gen で回答を目視する
    ときに「評価と同じ検索結果」を使い回せるよう、引いたチャンク本文(contexts)も
    results に持たせておく（--gen で再検索しないための保存）。
    """
    # None のときだけ既定を使う。空リスト [] は「0問で評価」の明示指定として尊重する
    gold = GOLD if gold is None else gold
    results = []
    hit_count = 0
    reciprocal_sum = 0.0

    for item in gold:
        hits = hybrid_search(
            item["question"], top_n=top_k, rerank=rerank, retrievers=retrievers
        )
        rank = _rank_of(hits, item["expected_source"])
        hit = rank is not None and rank < top_k
        if hit:
            hit_count += 1
            reciprocal_sum += 1.0 / (rank + 1)  # MRR: 1位=1.0, 2位=0.5, ...

        results.append(
            {
                "question": item["question"],
                "expected_source": item["expected_source"],
                "hit": hit,
                "rank": rank,  # None = 圏外
                "retrieved": [h["source"] for h in hits],
                # --gen 用。評価に使ったのと同一のヒット本文を回答生成へ渡す
                "contexts": [h["content"] for h in hits],
            }
        )

    n = len(gold)
    return {
        "n": n,
        "top_k": top_k,
        # この評価で実際に使った検索条件（Noneは「設定の既定を使用」の意味）
        "retrievers": retrievers,
        "rerank": rerank,
        "hit_at_k": round(hit_count / n, 3) if n else 0.0,
        "mrr": round(reciprocal_sum / n, 3) if n else 0.0,
        "results": results,
    }


def _print_report(report: dict, generate: bool = False) -> None:
    """人が読めるように結果を整形して標準出力に出す。"""
    k = report["top_k"]
    # 評価に使った検索条件（Noneは設定の既定）
    retrievers = ",".join(report["retrievers"]) if report["retrievers"] else "既定"
    rerank = {True: "有効", False: "無効", None: "既定"}[report["rerank"]]
    print(f"\n{'='*60}")
    print(f"検索評価  N={report['n']}  top_k={k}  手法={retrievers}  リランク={rerank}")
    print(f"  Hit@{k} = {report['hit_at_k']:.3f}   （上位{k}件に正解が入った割合）")
    print(f"  MRR    = {report['mrr']:.3f}   （正解の順位の逆数平均・1.0が満点）")
    print(f"{'='*60}")

    for r in report["results"]:
        mark = "○" if r["hit"] else "×"
        rank = "圏外" if r["rank"] is None else f"{r['rank'] + 1}位"
        print(f"\n{mark} [{rank}] {r['question']}")
        print(f"    正解: {r['expected_source']}")
        print(f"    検索: {', '.join(r['retrieved']) or '(なし)'}")

        if generate:
            # 目視確認用。回答生成には ANTHROPIC_API_KEY が要る。
            # 再検索はせず、評価と同一のヒット(contexts)から回答を作る
            # ＝ 上の○×・順位と回答が必ず同じ検索結果に基づく。
            answer = generate_answer(r["question"], r["contexts"])
            print(f"    回答: {answer.strip()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG検索の評価（Hit@k / MRR）")
    parser.add_argument(
        "--top-k", type=int, default=TOP_K, help="上位いくつを正解判定に使うか"
    )
    parser.add_argument(
        "--retrievers",
        type=str,
        default=None,
        help="使う検索手法をカンマ区切りで指定（例: vector,bm25）。未指定は設定の既定",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="LLMリランクを有効にして評価する（要 ANTHROPIC_API_KEY）",
    )
    parser.add_argument(
        "--gen",
        action="store_true",
        help="各問で回答生成まで走らせて目視する（要 ANTHROPIC_API_KEY）",
    )
    args = parser.parse_args()

    names = (
        [n.strip() for n in args.retrievers.split(",") if n.strip()]
        if args.retrievers
        else None
    )
    report = evaluate(
        top_k=args.top_k,
        retrievers=names,
        rerank=True if args.rerank else None,
    )
    _print_report(report, generate=args.gen)


if __name__ == "__main__":
    main()
