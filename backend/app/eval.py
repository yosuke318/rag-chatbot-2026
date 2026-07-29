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

図表（画像）の検索を比較評価する（5-2）:

  python -m app.eval --compare-image-index

  これ1本で「案A(自動キャプション)で索引 → 評価 → 案B(マルチモーダル埋め込み)で
  索引し直す → 評価 → 有意差を検定」まで回る。画像原本はS3にあるので、方式を
  変えるのにファイルを上げ直す必要はない（app.ingest.reindex_images）。

  ★成立の条件が2つある★

  1. 画像にしか答えが無い設問を入れること。本文テキストでも答えられる質問
     ばかりだと、どちらの方式でも同じ数字が出る（何も測れていない）。

  2. その設問の expected_kind を "image" にすること。既定の "any" は
     「その文書が上位に来れば正解」なので、本文チャンクが1位でも正解になり、
     ★索引方式を変えても数字が動かない★。'image' にして初めて
     「画像チャンクそのものを引けたか」を測ることになる。

  この2つは ViDoRe（ColPaliと同時に出た文書画像検索のベンチマーク）が
  「視覚的に情報が詰まったページ」と「テキストで足りるページ」を分けて
  評価しているのと同じ考え方。混ぜて平均すると差が消える。

  判定は paired bootstrap（同じ質問集の問ごとの差を再標本化して信頼区間とp値を
  出す）。20〜30問では Hit@k が 0.05 動いた程度では有意にならないので、
  信頼区間が0をまたぐうちは「まだ判断できない」と読むこと。
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from app.config import IMAGE_INDEX_METHOD, TOP_K
from app.db import get_conn
from app.llm import embed_multimodal_queries, embed_texts, generate_answer
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
                "(project, topic, question, expected_source, expected_kind, note) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    item.get("project"),
                    item.get("topic"),
                    item["question"],
                    item["expected_source"],
                    # 省略時は 'any'（文書が上位に来れば正解）＝ 従来の fixture が
                    # そのまま動く。図表根拠の設問だけ "image" を書く。
                    item.get("expected_kind") or "any",
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
            f"SELECT question, expected_source, note, project, topic, expected_kind "
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
            "expected_kind": r[5],
        }
        for r in rows
    ]


# 正解と認める「チャンクの種類」。eval_questions.expected_kind が取りうる値。
#   any   … 文書が上位に来れば正解（従来どおり）
#   text  … 本文チャンクで引けたときだけ正解
#   image … 文書内の画像チャンクで引けたときだけ正解
EXPECTED_KINDS = ("any", "text", "image")


def _matches(hit: dict, expected_source: str, expected_kind: str) -> bool:
    """このヒットを「正解」と認めるか。

    ★文書名だけでは図表の検索を測れない★
      画像は本文と同じ文書に属するので、文書名だけを正解ラベルにすると
      「本文チャンクが1位」でも「画像チャンクが1位」でも同じ点になる。
      それでは図表の索引方式(5-2の案A/案B)を変えても数字が動かず、比較にならない。
      そこで expected_kind='image' の設問は★画像チャンクで引けたときだけ★正解にする。

    種類は image_path の有無で判定する（値あり＝画像チャンク）。
    """
    if hit["source"] != expected_source:
        return False
    if expected_kind == "text":
        return hit.get("image_path") is None
    if expected_kind == "image":
        return hit.get("image_path") is not None
    return True


def _rank_of(
    hits: list[dict], expected_source: str, expected_kind: str = "any"
) -> int | None:
    """検索結果の中で正解が最初に現れた順位（0始まり）。無ければ None。"""
    for i, h in enumerate(hits):
        if _matches(h, expected_source, expected_kind):
            return i
    return None


def _summarize(results: list[dict], top_k: int) -> dict:
    """結果の並びから Hit@k と MRR を集計する（全体・種類別で使い回す）。"""
    n = len(results)
    if not n:
        return {"n": 0, "hit_at_k": 0.0, "mrr": 0.0}
    return {
        "n": n,
        "hit_at_k": round(sum(r["hit"] for r in results) / n, 3),
        "mrr": round(sum(r["reciprocal_rank"] for r in results) / n, 3),
    }


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
      取り込み方を変えて2回評価する比較評価（app.compare）で、両方の評価に
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

    # 画像ベクトル検索（案B）を使うときは、質問を★別のモデル★でもベクトル化する。
    # まとめて1リクエストにする理由はテキスト側とまったく同じ（レート制限）。
    if gold and "image" in resolve_retrievers(retrievers):
        image_vecs: list[list[float] | None] = list(
            embed_multimodal_queries(
                [g["question"] for g in gold], retry_waits=retry_waits
            )
        )
    else:
        image_vecs = [None] * len(gold)

    for item, query_vec, image_query_vec in zip(gold, vecs, image_vecs):
        hits = hybrid_search(
            item["question"],
            top_n=top_k,
            rerank=rerank,
            retrievers=retrievers,
            params=params,
            rrf_k=rrf_k,
            query_vec=query_vec,
            image_query_vec=image_query_vec,
            rerank_method=rerank_method,
            rerank_retry_waits=retry_waits,
            # ★質問と同じ区分の文書だけを対象にする★
            # 「社内規程の質問」を全プロジェクトの文書から探すと、他プロジェクトの
            # 文書が上位を埋めて Hit@k が下がる＝その区分の実力を測れない。
            # 区分なし(NULL)の質問は従来どおり全文書が対象。
            project=item.get("project"),
            topic=item.get("topic"),
        )
        expected_kind = item.get("expected_kind") or "any"
        rank = _rank_of(hits, item["expected_source"], expected_kind)
        hit = rank is not None and rank < top_k
        if hit:
            hit_count += 1
            reciprocal_sum += 1.0 / (rank + 1)  # MRR: 1位=1.0, 2位=0.5, ...

        results.append(
            {
                "question": item["question"],
                "expected_source": item["expected_source"],
                "expected_kind": expected_kind,
                "hit": hit,
                "rank": rank,  # None = 圏外
                # ★1問ごとの成績★ 条件Aと条件Bを問単位で対にして比べる
                # （paired_bootstrap）ために、平均する前の値を残しておく。
                "reciprocal_rank": (1.0 / (rank + 1)) if hit else 0.0,
                "retrieved": [h["source"] for h in hits],
                # 引いたのが本文か画像かは種類別の集計に要る（同じ文書名で並ぶため）
                "retrieved_kinds": [
                    "image" if h.get("image_path") else "text" for h in hits
                ],
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
        # ★比較評価の結果に条件を貼り付けておく★ 画像の索引方式は評価コマンドの
        # 引数ではなく「取り込み時の設定」で決まるため、レポートだけを見比べても
        # どちらを測ったのか分からなくなる。現在の設定値を一緒に残す
        # （手順どおりなら、この値の索引に対して測っている）。
        "image_index_method": IMAGE_INDEX_METHOD,
        "hit_at_k": round(hit_count / n, 3) if n else 0.0,
        "mrr": round(reciprocal_sum / n, 3) if n else 0.0,
        # ★種類別の内訳★ 図表根拠の設問と本文根拠の設問を混ぜて平均すると、
        # 図表側の改善が本文側の大量の設問に薄められて見えなくなる。
        # 索引方式の効果は "image" の行だけを見て判断する。
        "by_kind": {
            kind: _summarize([r for r in results if r["expected_kind"] == kind], top_k)
            for kind in EXPECTED_KINDS
            if any(r["expected_kind"] == kind for r in results)
        },
        "results": results,
    }


# ============================================================
# 比較評価: 2条件の差に意味があるかを測る
#
# ★これは A/B テストではない★
#   A/Bテストは本番トラフィックを無作為に2群へ分け、実利用者の行動で優劣を
#   決める online experiment。ここでやっているのは、固定の質問集に2つの構成を
#   通して正解ラベルと突き合わせる offline evaluation で、同じ質問に両方を通す
#   「対応のある比較(paired comparison)」。利用者も無作為化も関与しないので、
#   名前を分けてある（変数名の "conditions" は A/Bテストの "arms" と違う概念）。
#
# 検定が要る理由:
#   質問集が20〜30問の規模では、Hit@k が 0.70 → 0.75 に動いても「たまたま」で
#   説明が付いてしまう。IR分野の慣習に合わせ、問ごとの差をブートストラップで
#   再標本化して有意かどうかを見る。
# ============================================================

BOOTSTRAP_SAMPLES = 10000
BOOTSTRAP_SEED = 0  # 同じ入力なら同じ結果になるよう固定（再現できない検定は使えない）

# これ未満の設問数では、p値が出ても検定として意味を持たない。
# 極端な例: 3問すべてが同じ向きに動くと差のばらつきが0になり、
# 再標本化しても同じ値しか出ないので p=0.0000（＝完全に有意）になる。
# 「3問で有意差が出た」は★偶然を排除できていない★だけなので、警告を出す。
MIN_QUESTIONS_FOR_TEST = 10


def paired_bootstrap(
    baseline: list[float],
    variant: list[float],
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """対応のあるブートストラップ検定。{"diff", "ci_low", "ci_high", "p_value", "n"}。

    baseline / variant は★同じ質問の同じ並び★での1問ごとの成績
    （MRRなら reciprocal_rank、Hit@kなら 0/1）。

    やっていること: 質問の番号を復元抽出でN個選び直して平均差を計算する、を
    samples 回。得られた分布の 2.5%〜97.5% が95%信頼区間で、
    p値は「差が0以上/以下に転ぶ割合」の小さい方の2倍（両側）。

    t検定ではなくブートストラップを使うのは、MRRの分布が正規から程遠い
    （0に山があり1にも山がある）ため。TREC系の比較でも定番の手続き。
    """
    if len(baseline) != len(variant):
        raise ValueError("baseline と variant は同じ長さで渡してください")
    n = len(baseline)
    if n == 0:
        return {"diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_value": 1.0, "n": 0}

    diffs = [v - b for b, v in zip(baseline, variant)]
    observed = sum(diffs) / n

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        means.append(total / n)
    means.sort()

    # 片側の割合の小さい方を2倍（両側p値）。1.0 を超えないよう丸める
    n_le = sum(1 for m in means if m <= 0.0)
    n_ge = sum(1 for m in means if m >= 0.0)
    p_value = min(1.0, 2.0 * min(n_le, n_ge) / samples)

    return {
        "diff": round(observed, 4),
        "ci_low": round(means[int(0.025 * samples)], 4),
        "ci_high": round(means[min(samples - 1, int(0.975 * samples))], 4),
        "p_value": round(p_value, 4),
        "n": n,
    }


def compare_reports(
    baseline: dict, variant: dict, kind: str | None = None
) -> dict:
    """2つの評価レポートを問単位で突き合わせ、MRR と Hit@k の差を検定する。

    kind を指定すると、その expected_kind の設問だけで比較する
    （図表の索引方式を測るなら kind="image"）。

    同じ質問集に対する2回の評価であることを、質問文の並びで確かめる。
    違う質問集を比べると「対応のある比較」が成立しない（差が条件由来だと言えない）。
    """
    a = [r for r in baseline["results"] if kind is None or r["expected_kind"] == kind]
    b = [r for r in variant["results"] if kind is None or r["expected_kind"] == kind]
    if [r["question"] for r in a] != [r["question"] for r in b]:
        raise ValueError(
            "2つのレポートの質問集が一致しません（対応のある比較になりません）"
        )
    return {
        "kind": kind or "all",
        "mrr": paired_bootstrap(
            [r["reciprocal_rank"] for r in a], [r["reciprocal_rank"] for r in b]
        ),
        "hit_at_k": paired_bootstrap(
            [float(r["hit"]) for r in a], [float(r["hit"]) for r in b]
        ),
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
    # 画像の索引方式は取り込み時の設定なので、比較評価の条件として毎回出す
    print(f"  画像索引 = {report.get('image_index_method', 'none')}")
    print(f"  Hit@{k} = {report['hit_at_k']:.3f}   （上位{k}件に正解が入った割合）")
    print(f"  MRR    = {report['mrr']:.3f}   （正解の順位の逆数平均・1.0が満点）")
    print(f"{'='*60}")

    # 種類別の内訳。図表根拠の設問だけを見たいときはここ（"image" の行）を読む。
    # 全体平均は本文根拠の設問に引きずられるので、索引方式の判断には使えない。
    by_kind = report.get("by_kind") or {}
    if len(by_kind) > 1:
        print("  --- 正解の種類別 ---")
        for kind, s in by_kind.items():
            label = {"any": "文書単位", "text": "本文", "image": "画像"}[kind]
            print(
                f"  {label:<6} N={s['n']:<3} "
                f"Hit@{k}={s['hit_at_k']:.3f}  MRR={s['mrr']:.3f}"
            )
        print(f"{'='*60}")

    for r in report["results"]:
        mark = "○" if r["hit"] else "×"
        rank = "圏外" if r["rank"] is None else f"{r['rank'] + 1}位"
        print(f"\n{mark} [{rank}] {r['question']}")
        kind = r.get("expected_kind", "any")
        suffix = "" if kind == "any" else f"（{kind} チャンクで引けたら正解）"
        print(f"    正解: {r['expected_source']}{suffix}")
        # 同じ文書名が本文と画像で並ぶので、どちらを引いたのかを添える
        retrieved = ", ".join(
            f"{s}[{t[0]}]" for s, t in zip(r["retrieved"], r.get("retrieved_kinds", []))
        ) or ", ".join(r["retrieved"])
        print(f"    検索: {retrieved or '(なし)'}")

        if generate:
            # 目視確認用。回答生成には ANTHROPIC_API_KEY が要る。
            # 再検索はせず、評価と同一のヒット(contexts)から回答を作る
            # ＝ 上の○×・順位と回答が必ず同じ検索結果に基づく。
            answer = generate_answer(r["question"], r["contexts"])
            print(f"    回答: {answer.strip()}")


# 画像索引の比較評価で使う条件。案Aは既存3手法だけ、案Bは image をもう1本足す
# （案Bの画像チャンクは説明文を持たないので、image を外すと絶対に引けない）。
IMAGE_INDEX_CONDITIONS = {
    "caption": ["vector", "trgm", "bm25"],
    "multimodal": ["vector", "trgm", "bm25", "image"],
}


def compare_image_index_methods(
    top_k: int = TOP_K,
    gold: list[dict] | None = None,
    retry_waits: list[int] | None = None,
) -> dict:
    """案A（キャプション）と案B（マルチモーダル埋め込み）を同じ質問集で比べる。

    索引を作り直す → 評価する、を2回やって paired bootstrap にかける。
    画像原本はS3にあるので、ファイルを上げ直さずに方式だけ入れ替えられる
    （app.ingest.reindex_images）。

    ★判定は expected_kind='image' の設問だけで行う★
      本文でも答えられる設問を混ぜると、どちらの方式でも同じ数字になり
      差が薄まる（そもそも図表の検索を測れていない）。
    """
    from app.ingest import reindex_images

    gold = load_questions() if gold is None else gold
    reports: dict[str, dict] = {}
    for method, retrievers in IMAGE_INDEX_CONDITIONS.items():
        # ★索引の作り直しでも429を待つ★ ここで待たずに失敗すると、索引の無い
        # 画像が並んだまま評価が走り、「その方式では図を引けない」という
        # 実測値と見分けの付かない0点が出る（実際に踏んだ）。
        reindexed = reindex_images(method, retry_waits=retry_waits)
        report = evaluate(
            top_k=top_k,
            retrievers=retrievers,
            gold=gold,
            retry_waits=retry_waits,
        )
        report["image_index_method"] = method  # 実際に索引し直した方式で上書き
        report["reindexed"] = reindexed
        reports[method] = report

    return {
        "conditions": reports,
        # 図表根拠の設問だけの比較（本命）と、全体の比較（副作用の確認）
        "image_only": compare_reports(
            reports["caption"], reports["multimodal"], kind="image"
        ),
        "overall": compare_reports(reports["caption"], reports["multimodal"]),
    }


def _print_comparison(comparison: dict) -> None:
    """比較評価の結果を、判断できる形（差・信頼区間・p値）で出す。"""
    print(f"\n{'='*60}")
    print("画像索引方式の比較評価（オフライン・対応のある比較）")
    print("  案A(caption) → 案B(multimodal)")
    print(f"{'='*60}")
    incomplete = []
    for method, report in comparison["conditions"].items():
        by_image = (report.get("by_kind") or {}).get("image")
        line = f"  {method:<11} 全体 Hit@k={report['hit_at_k']:.3f} MRR={report['mrr']:.3f}"
        if by_image:
            line += (
                f"   / 画像設問(N={by_image['n']}) "
                f"Hit@k={by_image['hit_at_k']:.3f} MRR={by_image['mrr']:.3f}"
            )
        r = report.get("reindexed") or {}
        line += f"   [索引 {r.get('indexed', 0)}/{r.get('images', 0)}枚]"
        print(line)
        if r.get("indexed", 0) < r.get("images", 0):
            incomplete.append(method)

    if incomplete:
        # ここを黙って通すと、APIが失敗しただけの0点を「その方式は図を引けない」
        # という結論として読んでしまう。数字より先に出す。
        print(
            f"\n  ⚠ 索引を作れなかった画像があります（{', '.join(incomplete)}）。"
            "\n    レート制限やAPIエラーの可能性があります。この比較結果は使えません。"
            "\n    ログの警告を確認し、作り直してから測り直してください。"
        )

    underpowered = False
    for key, label in (("image_only", "画像根拠の設問のみ"), ("overall", "全設問")):
        c = comparison[key]
        print(f"\n  --- {label} ---")
        for metric in ("mrr", "hit_at_k"):
            s = c[metric]
            if s["n"] < MIN_QUESTIONS_FOR_TEST:
                verdict = "判断不可（設問不足）"
                underpowered = True
            elif s["p_value"] < 0.05:
                verdict = "有意差あり"
            else:
                verdict = "有意差なし"
            print(
                f"  {metric:<8} 差={s['diff']:+.4f}  "
                f"95%CI=[{s['ci_low']:+.4f}, {s['ci_high']:+.4f}]  "
                f"p={s['p_value']:.4f}  N={s['n']}  → {verdict}"
            )

    print("\n  ※ 差は「案B - 案A」。信頼区間が0をまたぐ間は、どちらが良いとも言えない。")
    if underpowered:
        # ★少ない設問で出たp値を信じさせない★
        # 数問しか無いと全問が同じ向きに動きやすく、差のばらつきが消えて
        # p が 0 に張り付く。有意なのではなく、偶然を排除できていない。
        print(
            f"  ※ 設問が {MIN_QUESTIONS_FOR_TEST} 問未満の比較は、p値が小さく出ても"
            "検定として意味を持ちません"
            "\n     （全問が同じ向きに動くと差のばらつきが消え、p=0 に張り付くため）。"
            "\n     まずは図表根拠の設問を増やしてください。"
        )


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
    parser.add_argument(
        "--compare-image-index",
        action="store_true",
        help="画像の索引方式を比較評価する（索引を作り直して2回評価し、有意差を検定）",
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

    if args.compare_image_index:
        # 図表根拠の設問が無いと、この比較は「差が無い」としか言えない。
        # 黙って0を並べるより、足りないことを先に伝える。
        if not [g for g in gold if g.get("expected_kind") == "image"]:
            print(
                "expected_kind='image' の設問がありません。\n"
                "画像にしか答えが無い質問を登録してください"
                "（POST /eval-questions に expected_kind=\"image\" を付ける）。\n"
                "本文でも答えられる設問だけでは、どちらの索引方式でも同じ数字になり"
                "比較になりません。"
            )
            return
        _print_comparison(
            compare_image_index_methods(top_k=args.top_k, gold=gold, retry_waits=RETRY_WAITS)
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
