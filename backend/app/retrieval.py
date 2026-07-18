"""検索: ハイブリッド検索 → (次段) リランク。★このプロジェクトの学習の核★

流れ:
  1. ベクトル検索   (意味の近さ) … 上位 CANDIDATES 件
  2. 字面検索       (文字の一致) … 上位 CANDIDATES 件
  3. RRF で 2つのランキングを融合 → 上位 TOP_K 件
  4. (TODO) LLMリランク で最終並べ替え

なぜ2種類混ぜるか:
  - ベクトル検索は「意味」に強いが、型番・固有名詞など"字面そのもの"の一致に弱い。
  - 字面検索は逆。「有給」という語がそのまま入っている文書を確実に拾う。
  - 両者は得意分野が違うので、混ぜると取りこぼしが減る。
"""
from __future__ import annotations

from app.config import RERANK_CANDIDATES, TOP_K, USE_RERANK
from app.db import get_conn
from app.llm import embed_query, rank_by_relevance

CANDIDATES = 20  # 各検索が融合前に返す候補数


def vector_search(question: str, k: int = CANDIDATES) -> list[dict]:
    """意味の近さ（コサイン距離）で上位k件。"""
    query_vec = embed_query(question)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.content, d.source
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            ORDER BY c.embedding <=> %s
            LIMIT %s
            """,
            (query_vec, k),
        ).fetchall()
    return [{"id": r[0], "content": r[1], "source": r[2]} for r in rows]


def lexical_search(question: str, k: int = CANDIDATES) -> list[dict]:
    """字面の一致（トライグラム類似度）で上位k件。

    ※ pg_trgm の similarity() は「文字3つ組の重なり具合」で、厳密なBM25ではない。
      日本語をそのまま扱える手軽さを優先した選択。BM25本来の実装は
      tsvector+ts_rank（日本語は形態素解析器が別途必要）や外部エンジンになる。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.content, d.source
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            ORDER BY similarity(c.content, %s) DESC
            LIMIT %s
            """,
            (question, k),
        ).fetchall()
    return [{"id": r[0], "content": r[1], "source": r[2]} for r in rows]


def reciprocal_rank_fusion(ranked_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """複数のランキングを RRF で1つに融合する。

    各アイテムのスコア = Σ 1 / (k + そのリストでの順位)
      - 順位ベースなので、ベクトル距離と字面スコアの"単位の違い"を気にせず混ぜられる。
      - 上位に何度も現れるアイテムほど高スコアになる。
      - k(=60が慣例) は順位差を平滑化する定数。大きいほど順位の差が緩やかになる。
    アイテムの同一性は chunk の id で判定（同じチャンクが両リストに出たら合算）。
    """
    scores: dict[int, float] = {}
    items: dict[int, dict] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):  # rank は 0始まり
            cid = item["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            items[cid] = item
    ordered_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [items[cid] for cid in ordered_ids]


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
