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

評価用の質問集はどこにあるか:
  正解ラベル付きの質問は DB の eval_questions テーブルに置く（プロジェクト・
  トピックごとに分けられる）。文書(documents)を project/topic で分ける方針に
  評価も合わせるため。★DBが正★で、コード内に質問を持たない。

  初期データは seed_docs/*.txt とセットの fixture として
  backend/seed_data/eval_questions.json に置き、--seed でDBへ流し込む
  （Django の fixture と同じ位置づけ。冪等なので何度流してもよい）。
  設問を足すときはこの JSON に追記するか、POST /eval-questions でDBへ直接入れる。

使い方:
  python -m app.eval --seed                       # fixture をDBへ初期投入（冪等）
  python -m app.eval                              # DBの全質問で検索を評価
  python -m app.eval --project 社内規程 --topic 労務    # プロジェクト・トピックで絞って評価
  python -m app.eval --retrievers vector,bm25     # 手法を変えて比較
  python -m app.eval --top-k 4 --rerank           # 上位件数やリランクの有無を変える
  python -m app.eval --rerank --rerank-method llm # リランクの方式を変えて比較
      （3条件の比較: 素の検索 / --rerank --rerank-method llm / --rerank --rerank-method voyage）
  python -m app.eval --gen                        # 回答生成まで走らせて目視（要Anthropic）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import TOP_K
from app.db import get_conn
from app.llm import embed_texts, generate_answer
from app.retrieval import RERANKERS, hybrid_search, resolve_retrievers

# 429の待ち時間はseedと同じ設定(SEED_RETRY_WAITS)を使う。バッチ処理の待ち方は
# 「取り込み」も「評価」も同じでよく、環境変数を2つに増やす理由がないため。
from app.seed import RETRY_WAITS

# --- 初期投入用の質問セット（fixture） ----------------------------------------
# 質問の正はDB(eval_questions)。ここはあくまで「seed_docs とセットの初期データ」で、
# --seed でDBへ流し込む（Django の fixture と同じ位置づけ）。
#   expected_source: この質問に答えられる根拠が入っている文書（正解ラベル）
#   note           : 何を確かめる質問かのメモ（人間向け。採点には使わない）
#   project/topic: 省略可（＝プロジェクト・トピックをまたぐ共通の質問）
SEED_QUESTIONS_PATH = (
    Path(__file__).resolve().parent.parent / "seed_data" / "eval_questions.json"
)


def load_seed_questions(path: Path | None = None) -> list[dict]:
    """fixture(JSON)から初期投入用の質問を読む。ファイルが無ければ空リスト。"""
    path = SEED_QUESTIONS_PATH if path is None else path
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def seed_questions(questions: list[dict] | None = None) -> int:
    """fixture の質問をDBへ投入する。既にある質問(同じ本文)は入れない（冪等）。

    追加した件数を返す。ローカルや初期セットアップで一度流すことを想定。
    """
    questions = load_seed_questions() if questions is None else questions
    added = 0
    with get_conn() as conn:
        for item in questions:
            exists = conn.execute(
                "SELECT 1 FROM eval_questions WHERE question = %s LIMIT 1",
                (item["question"],),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO eval_questions "
                "(project, topic, question, expected_source, note) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    item.get("project"),
                    item.get("topic"),
                    item["question"],
                    item["expected_source"],
                    item.get("note"),
                ),
            )
            added += 1
    return added


def load_questions(
    project: str | None = None, topic: str | None = None
) -> list[dict]:
    """評価用の質問をDBから読む。project/topic を指定するとその分だけに絞る。

    指定しなかった軸は絞り込まない（例: project だけ指定ならトピックは問わず全部）。
    文書(documents)を同じ軸で分ける方針に合わせ、「そのプロジェクトの文書 ×
    その質問」で評価できるようにするための絞り込み。
    """
    clauses = []
    params: list[str] = []
    if project is not None:
        clauses.append("project = %s")
        params.append(project)
    if topic is not None:
        clauses.append("topic = %s")
        params.append(topic)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT question, expected_source, note, project, topic "
            f"FROM eval_questions {where} ORDER BY id",
            params,
        ).fetchall()
    return [
        {
            "question": r[0],
            "expected_source": r[1],
            "note": r[2],
            "project": r[3],
            "topic": r[4],
        }
        for r in rows
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
    params: dict[str, dict] | None = None,
    rrf_k: int | None = None,
    query_vecs: list[list[float]] | None = None,
    rerank_method: str | None = None,
    retry_waits: list[int] | None = None,
) -> dict:
    """質問を1問ずつ検索にかけ、Hit@k と MRR を集計して返す。

    各問について hybrid_search を実行し、正解文書が上位 top_k に入ったか・
    何位だったかを記録する。ここでは回答生成はしないが、--gen で回答を目視する
    ときに「評価と同じ検索結果」を使い回せるよう、引いたチャンク本文(contexts)も
    results に持たせておく（--gen で再検索しないための保存）。

    params / rrf_k を渡すと、検索の数値パラメータ（字面の閾値・BM25のk1/b・RRFのk）を
    変えて評価できる。「k1を上げるとHit@kは上がるか」を数値で測るための引数。
    未指定なら設定の既定値で評価する。

    rerank_method: リランクの方式（"voyage" / "llm"）。rerank=True のときだけ効く。
      「リランクなし / プロンプト式 / rerank-2」の3条件を同じ質問集で比較するための引数。

    retry_waits: Voyage が 429 を返したときに待つ秒数の並び（質問のベクトル化と
      リランクの両方に効く）。★埋め込みと違いリランクは質問ごとに1リクエスト★
      （まとめられない）ため、無料枠(3 RPM)ではリランク有りの評価が4問目で必ず
      当たる。CLI(python -m app.eval)は待ってでも完走させたいので渡す。
      Web経路(/eval)は None のまま即429を返す（利用者を何十秒も待たせない）。

    query_vecs: 質問のベクトルを外から渡す（gold と同じ並び・同じ長さ）。
      取り込み方を変えて2回評価する A/B 測定（app.compare）で、両方の評価に
      ★同一のベクトル★を使うための引数。こうすると差が文書側の変更だけに
      由来すると言い切れるうえ、埋め込みAPIの呼び出しも1回で済む。
    """
    # None のときだけ既定を使う。空リスト [] は「0問で評価」の明示指定として尊重する
    gold = load_seed_questions() if gold is None else gold
    results = []
    hit_count = 0
    reciprocal_sum = 0.0

    # ★質問のベクトル化は1回にまとめる★
    #   1問ずつ埋め込むと「質問数 = APIリクエスト数」になり、埋め込みAPIの分間
    #   リクエスト上限（Voyage 無料枠は 3 RPM）に4問目で当たって評価が完走しない。
    #   評価は質問が最初から全部分かっているので、まとめて1リクエストで済む。
    #   ベクトル検索を使わない構成（trgm/bm25 のみ）では埋め込み自体を呼ばない。
    if query_vecs is not None:
        if len(query_vecs) != len(gold):
            raise ValueError("query_vecs は gold と同じ長さで渡してください")
        vecs: list[list[float] | None] = list(query_vecs)
    elif gold and "vector" in resolve_retrievers(retrievers):
        vecs = list(
            embed_texts(
                [g["question"] for g in gold],
                input_type="query",
                retry_waits=retry_waits,
            )
        )
    else:
        vecs = [None] * len(gold)

    for item, query_vec in zip(gold, vecs):
        hits = hybrid_search(
            item["question"],
            top_n=top_k,
            rerank=rerank,
            retrievers=retrievers,
            params=params,
            rrf_k=rrf_k,
            query_vec=query_vec,
            rerank_method=rerank_method,
            rerank_retry_waits=retry_waits,
            # ★質問と同じ区分の文書だけを対象にする★
            # 「社内規程の質問」を全プロジェクトの文書から探すと、他プロジェクトの
            # 文書が上位を埋めて Hit@k が下がる＝その区分の実力を測れない。
            # 区分なし(NULL)の質問は従来どおり全文書が対象。
            project=item.get("project"),
            topic=item.get("topic"),
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
        "rerank_method": rerank_method,
        "rrf_k": rrf_k,
        "params": params,
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
    if report["rerank"]:  # 有効なときだけ方式を添える（None/無効では意味が無い）
        rerank += f"({report.get('rerank_method') or '既定'})"
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
        help="リランクを有効にして評価する",
    )
    parser.add_argument(
        "--rerank-method",
        type=str,
        default=None,
        choices=sorted(RERANKERS),
        help="リランクの方式（voyage=専用API / llm=プロンプト式・要 ANTHROPIC_API_KEY）。"
        "未指定は設定の既定",
    )
    parser.add_argument(
        "--gen",
        action="store_true",
        help="各問で回答生成まで走らせて目視する（要 ANTHROPIC_API_KEY）",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="fixture(seed_data/eval_questions.json)をDBへ投入して終了する（冪等）",
    )
    parser.add_argument(
        "--project", type=str, default=None, help="このプロジェクトの質問だけで評価する"
    )
    parser.add_argument(
        "--topic", type=str, default=None, help="このトピックの質問だけで評価する"
    )
    args = parser.parse_args()

    if args.seed:
        added = seed_questions()
        # 「0件」は fixture が空なのか全部スキップされたのか区別が付かないので、
        # fixture の件数とDBの現在件数まで出す（冪等な操作は結果が読めることが大事）。
        total = len(load_questions())
        print(
            f"評価質問: {added} 件を追加"
            f"（fixture {len(load_seed_questions())} 件中、既存はスキップ）。"
            f"DBの登録件数は {total} 件。"
        )
        return

    gold = load_questions(project=args.project, topic=args.topic)
    if not gold:
        scope = " / ".join(
            filter(None, [args.project, args.topic])
        ) or "指定なし"
        print(
            f"評価用の質問が見つかりません（絞り込み: {scope}）。\n"
            f"まず `python -m app.eval --seed` でサンプルを投入するか、"
            f"POST /eval-questions で質問を登録してください。"
        )
        return

    names = (
        [n.strip() for n in args.retrievers.split(",") if n.strip()]
        if args.retrievers
        else None
    )
    report = evaluate(
        top_k=args.top_k,
        retrievers=names,
        rerank=True if args.rerank else None,
        gold=gold,
        rerank_method=args.rerank_method,
        # CLIはバッチなので、429は待って再試行する（Web経路の /eval は待たない）
        retry_waits=RETRY_WAITS,
    )
    _print_report(report, generate=args.gen)


if __name__ == "__main__":
    main()
