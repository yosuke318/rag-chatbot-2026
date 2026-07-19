"""検索: ハイブリッド検索 → (次段) リランク。★このプロジェクトの学習の核★

流れ:
  1. ベクトル検索   (意味の近さ) … 上位 CANDIDATES 件 -> vector_search
  2. 字面検索       (文字の一致) … 上位 CANDIDATES 件 -> lexical_search
  3. RRF で 2つのランキングを融合 → 上位 TOP_K 件 -> reciprocal_rank_fusion と _rrf_scores
  4. (TODO) LLMリランク で最終並べ替え -> llm_rerank

なぜ2種類混ぜるか:
  - ベクトル検索は「意味」に強いが、型番・固有名詞など"字面そのもの"の一致に弱い。
  - 字面検索は逆。「有給」という語がそのまま入っている文書を確実に拾う。
  - 両者は得意分野が違うので、混ぜると取りこぼしが減る。
"""
from __future__ import annotations

from app.config import (
    LEXICAL_MIN_SIMILARITY,
    RERANK_CANDIDATES,
    TOP_K,
    USE_RERANK,
)
from app.db import get_conn
from app.llm import embed_query, rank_by_relevance

CANDIDATES = 20  # 各検索が融合前に返す候補数


def vector_search(question: str, k: int = CANDIDATES) -> list[dict]:
    """意味の近さ（コサイン距離）で上位k件。

    pgvector の `<=>` はコサイン**距離**（0=完全一致、大きいほど遠い）。
    直感的に読めるよう コサイン類似度 = 1 - 距離 も併せて返す。
    """
    query_vec = embed_query(question)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.content, d.source,
                   c.embedding <=> %s::vector AS cosine_distance
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vec, query_vec, k),
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1],
            "source": r[2],
            "cosine_distance": float(r[3]),
            "cosine_similarity": 1.0 - float(r[3]),
        }
        for r in rows
    ]


def lexical_search(
    question: str,
    k: int = CANDIDATES,
    min_similarity: float = LEXICAL_MIN_SIMILARITY,
) -> list[dict]:
    """字面の一致（トライグラム類似度）で上位k件。閾値未満は返さない。

    ※ pg_trgm の similarity() は「文字3つ組の重なり具合」で、厳密なBM25ではない。
      日本語をそのまま扱える手軽さを優先した選択。BM25本来の実装は
      tsvector+ts_rank（日本語は形態素解析器が別途必要）や外部エンジンになる。

    閾値の意図: 類似度0の候補まで順位を持つと、その"偽の順位"がRRFに票を投じて
    しまう（実測で無関係な文書が融合2位に浮上した）。ここで落とせば、
    全件が閾値未満のときは字面リストが空になり、RRFは自然とベクトルの順位だけで
    決まる ＝ 分岐を書かずに「cos類似度のみで評価」が成立する。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.content, d.source,
                   similarity(c.content, %s) AS trgm_similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE similarity(c.content, %s) >= %s
            ORDER BY similarity(c.content, %s) DESC
            LIMIT %s
            """,
            (question, question, min_similarity, question, k),
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1],
            "source": r[2],
            "trgm_similarity": float(r[3]),  # 0〜1（1に近いほど字面が一致）
        }
        for r in rows
    ]


def _rrf_scores(
    ranked_lists: list[list[dict]], k: int = 60
) -> list[tuple[dict, float, dict[int, int]]]:
    """RRFの計算本体。(アイテム, 合計スコア, {リスト番号: 順位}) をスコア降順で返す。

    各アイテムのスコア = Σ 1 / (k + そのリストでの順位)
      - 順位ベースなので、ベクトル距離と字面スコアの"単位の違い"を気にせず混ぜられる。
      - 複数リストの上位に現れるほど、逆数が何度も足されて高スコアになる。
      - k(=60が慣例) は順位差を平滑化する定数。大きいほど順位の差が緩やかになる。
    アイテムの同一性は chunk の id で判定（同じチャンクが両リストに出たら合算）。
    """
    scores: dict[int, float] = {}
    items: dict[int, dict] = {}
    ranks: dict[int, dict[int, int]] = {}
    for list_index, ranked in enumerate(ranked_lists):
        for rank, item in enumerate(ranked):  # rank は 0始まり
            cid = item["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            items[cid] = item
            ranks.setdefault(cid, {})[list_index] = rank
    ordered_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [(items[cid], scores[cid], ranks[cid]) for cid in ordered_ids]


def reciprocal_rank_fusion(ranked_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """複数のランキングを RRF で融合し、スコア降順のアイテム列を返す。"""
    return [item for item, _score, _ranks in _rrf_scores(ranked_lists, k)]


def llm_rerank(question: str, candidates: list[dict], top_n: int = TOP_K) -> list[dict]:
    """候補をLLMに渡して関連度で並べ替え、上位top_nを返す。

    RRFは「複数の検索が上位に挙げたか」で決まるが、実際に質問に答えているかは
    見ていない。そこをLLMに読ませて最終順位を付け直すのがリランク。
    LLMが番号を漏らした候補は末尾に補う（安全網：件数が減らないように）。
    """
    if not candidates:
        return []
    order = rank_by_relevance(question, [c["content"] for c in candidates])
    seen = set(order)
    order += [i for i in range(len(candidates)) if i not in seen]
    return [candidates[i] for i in order[:top_n]]


def hybrid_search(
    question: str, top_n: int = TOP_K, rerank: bool | None = None
) -> list[dict]:
    """ベクトル検索 + 字面検索 を RRF で融合。rerank=True ならLLMで再並べ替え。

    rerank を省略すると設定(USE_RERANK)に従う。有り/無しを切り替えて
    評価で比較できるようにしている。
    """
    if rerank is None:
        rerank = USE_RERANK

    vec = vector_search(question)
    lex = lexical_search(question)
    fused = reciprocal_rank_fusion([vec, lex])

    if rerank:
        # 融合上位を少し多めにリランク対象へ渡し、そこから top_n を選ばせる
        return llm_rerank(question, fused[:RERANK_CANDIDATES], top_n)
    return fused[:top_n]


def _preview(text: str, n: int = 80) -> str:
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def search_stages(question: str, top_n: int = TOP_K, show: int = 5) -> dict:
    """検索の各段階を返す（学習・デバッグ用。★Claudeを呼ばないのでAnthropicキー不要★）

    ベクトル検索の順位 / 字面検索の順位 / 融合後のスコアを並べて返すので、
    「両方の検索が上位に挙げたチャンクがRRFで上に来る」挙動を実データで確認できる。
    """
    vec = vector_search(question)
    lex = lexical_search(question)
    scored = _rrf_scores([vec, lex])  # リスト番号 0=ベクトル, 1=字面

    # 融合後の行に「元の生スコア」を戻すための引き当て表
    # （_rrf_scores のitemは後勝ちで上書きされるため、ここで各リストから取り直す）
    vec_by_id = {h["id"]: h for h in vec}
    lex_by_id = {h["id"]: h for h in lex}

    def rounded(value: float | None, digits: int = 4) -> float | None:
        return None if value is None else round(value, digits)

    return {
        "question": question,
        # 字面リストが空 = 全候補が閾値未満 → 実質ベクトル検索のみで順位が決まる
        "lexical_min_similarity": LEXICAL_MIN_SIMILARITY,
        "vector_search": [
            {
                "rank": i,
                "id": h["id"],
                "source": h["source"],
                "cosine_similarity": rounded(h["cosine_similarity"]),
                "cosine_distance": rounded(h["cosine_distance"]),
                "preview": _preview(h["content"]),
            }
            for i, h in enumerate(vec[:show])
        ],
        "lexical_search": [
            {
                "rank": i,
                "id": h["id"],
                "source": h["source"],
                "trgm_similarity": rounded(h["trgm_similarity"]),
                "preview": _preview(h["content"]),
            }
            for i, h in enumerate(lex[:show])
        ],
        "fused": [
            {
                "rank": i,
                "id": item["id"],
                "source": item["source"],
                "score": round(score, 5),
                "vector_rank": ranks.get(0),  # None = そのリストには出なかった
                "lexical_rank": ranks.get(1),
                # 各検索が出した「生の類似度」。出てこなかった検索側は None
                "cosine_similarity": rounded(
                    vec_by_id[item["id"]]["cosine_similarity"]
                    if item["id"] in vec_by_id
                    else None
                ),
                "trgm_similarity": rounded(
                    lex_by_id[item["id"]]["trgm_similarity"]
                    if item["id"] in lex_by_id
                    else None
                ),
                "preview": _preview(item["content"]),
            }
            for i, (item, score, ranks) in enumerate(scored[:top_n])
        ],
    }
