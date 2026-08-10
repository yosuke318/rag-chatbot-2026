"""検索: ハイブリッド検索 → (次段) リランク。★このプロジェクトの学習の核★

流れ:
  1. ベクトル検索   (意味の近さ) … 上位 CANDIDATES 件 -> vector_search
  2. 字面検索       (文字の一致) … 上位 CANDIDATES 件 -> lexical_search
  3. RRF で 2つのランキングを融合 → 上位 TOP_K 件 -> reciprocal_rank_fusion と _rrf_scores
  4. リランク で最終並べ替え -> rerank_candidates（USE_RERANK 有効時のみ）

文書内の図表は取り込み方で当たり方が変わる:
  - 案A（自動キャプション）… 画像の説明文が普通のチャンクとして入るので、上の
    1〜3 がそのまま効く。専用の手法は要らない。
  - 案B（マルチモーダル埋め込み）… 画像は別の空間のベクトルなので、専用の
    image_search を RRF にもう1本足して比較する。

なぜ2種類混ぜるか:
  - ベクトル検索は「意味」に強いが、型番・固有名詞など"字面そのもの"の一致に弱い。
  - 字面検索は逆。「有給」という語がそのまま入っている文書を確実に拾う。
  - 両者は得意分野が違うので、混ぜると取りこぼしが減る。
"""
from __future__ import annotations

from app.config import (
    BM25_B,
    BM25_K1,
    LEXICAL_MIN_SIMILARITY,
    RERANK_CANDIDATES,
    RERANK_METHOD,
    RETRIEVERS_DEFAULT,
    TOP_K,
    USE_RERANK,
)
from app.db import get_conn
from app.keywords import extract_nouns, noun_text
from app.llm import (
    embed_multimodal_queries,
    embed_query,
    rank_by_relevance,
    voyage_rerank,
)

CANDIDATES = 20  # 各検索が融合前に返す候補数
RRF_K = 60       # RRFの平滑化定数（順位差をなだらかにする）


def _scope_sql(
    project: str | None, topic: str | None, alias: str = "d"
) -> tuple[str, list]:
    """project / topic の絞り込みを「AND句の断片」と埋め込み値にする。

    指定しなかった軸は絞り込まない（project だけ指定ならトピックは問わず全部）。
    NULL は「どこにも属さない共通文書」の意味なので `= %s` で拾えない値であり、
    未指定＝条件を作らない、で扱いを揃えている（空文字はAPI境界で None に
    正規化済み。app.main._blank_to_none 参照）。

    ★受け取るのは名前、絞るのは id★
      行が持つのは project_id / topic_id（マスタへの参照）だが、APIの境界は
      名前のままなので、ここでサブクエリで id に引く。
      - project: 名前はユニークなのでスカラサブクエリで `=`。無い名前なら
        NULL になり、`= NULL` は常に偽 ＝ 0件（TEXT時代の「無い名前は0件」と同じ）。
      - topic: ★同名トピックが複数プロジェクトに在り得る★ ので IN で全部拾う。
        TEXT時代の `topic = %s` も名前だけの一致だったので挙動は変わらない。

    pgvector の HNSW は「近傍を探してから絞る」ため、区分が細かく1区分の割合が
    小さいと候補が不足しうる。個人利用の規模では実害が無いのでそのままにし、
    必要になったら pgvector 0.8+ の iterative scan か区分別の部分インデックスで
    対処する。
    """
    clauses: list[str] = []
    values: list = []
    if project is not None:
        clauses.append(
            f" AND {alias}.project_id = (SELECT id FROM projects WHERE name = %s)"
        )
        values.append(project)
    if topic is not None:
        clauses.append(
            f" AND {alias}.topic_id IN (SELECT id FROM topics WHERE name = %s)"
        )
        values.append(topic)
    return "".join(clauses), values


def vector_search(
    question: str,
    k: int = CANDIDATES,
    params: dict | None = None,
    query_vec: list[float] | None = None,
    image_query_vec: list[float] | None = None,  # 使わない（image_search 専用）
    project: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """意味の近さ（コサイン距離）で上位k件。

    pgvector の `<=>` はコサイン**距離**（0=完全一致、大きいほど遠い）。
    直感的に読めるよう コサイン類似度 = 1 - 距離 も併せて返す。

    embedding は NULL 許容（遅延埋め込み・画像チャンク等の余地）。NULL の行は
    距離が NULL になり float(None) で落ちるため、SQL 段階で除外する。

    query_vec: 質問のベクトルを外から渡す（未指定なら埋め込みAPIを呼ぶ）。
      評価(eval)のように質問が何件も分かっているときは、呼び出し側で全件を
      1回の embed にまとめてからここへ渡す。1問1リクエストだと埋め込みAPIの
      分間リクエスト上限（Voyage 無料枠は 3 RPM）に4問目で当たるため。

    project / topic: 指定するとその区分の文書だけを検索対象にする（_scope_sql）。
    """
    if query_vec is None:
        query_vec = embed_query(question)
    scope, scope_values = _scope_sql(project, topic)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.content, d.source, c.image_path, c.context,
                   c.embedding <=> %s::vector AS cosine_distance
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL{scope}
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vec, *scope_values, query_vec, k),
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1],
            "source": r[2],
            "image_path": r[3],
            "context": r[4],
            "cosine_distance": float(r[5]),
            "cosine_similarity": 1.0 - float(r[5]),
        }
        for r in rows
    ]


def image_search(
    question: str,
    k: int = CANDIDATES,
    params: dict | None = None,
    query_vec: list[float] | None = None,  # 使わない（空間が違う。下記）
    image_query_vec: list[float] | None = None,
    project: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """画像ベクトル（案B: voyage-multimodal-3）での近傍探索。上位k件。

    ★query_vec を使わない★のがこの手法の要点。vector_search が使う質問ベクトルは
    voyage-3.5 の空間、画像は voyage-multimodal-3 の空間で、次元は同じ1024でも
    まったく別物。取り違えてもSQLはエラーにならず、ただ無意味な順位が返るだけなので、
    ここでは受け取った query_vec を明示的に無視し、専用の image_query_vec を使う。

    image_embedding を持つのは IMAGE_INDEX_METHOD=multimodal で取り込んだ画像
    チャンクだけ。案A（キャプション）で運用しているときこの手法は常に空を返し、
    RRF は残りの手法だけで決まる（lexical_search が閾値未満で空を返すのと同じ）。

    image_query_vec: 質問のマルチモーダルベクトルを外から渡す（未指定なら呼ぶ）。
      vector_search の query_vec と同じ理由 ＝ 評価で質問ごとに1リクエスト
      投げると Voyage の分間上限に当たるため、eval は全問まとめて渡す。
    """
    if image_query_vec is None:
        image_query_vec = embed_multimodal_queries([question])[0]
    scope, scope_values = _scope_sql(project, topic)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.content, d.source, c.image_path, c.context,
                   c.image_embedding <=> %s::vector AS cosine_distance
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.image_embedding IS NOT NULL{scope}
            ORDER BY c.image_embedding <=> %s::vector
            LIMIT %s
            """,
            (image_query_vec, *scope_values, image_query_vec, k),
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1],
            "source": r[2],
            "image_path": r[3],
            "context": r[4],
            "cosine_distance": float(r[5]),
            "cosine_similarity": 1.0 - float(r[5]),
        }
        for r in rows
    ]


def lexical_search(
    question: str,
    k: int = CANDIDATES,
    params: dict | None = None,
    query_vec: list[float] | None = None,  # 使わない（レジストリの引数を揃えるため）
    image_query_vec: list[float] | None = None,  # 同上
    project: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """字面の一致（トライグラム類似度）で上位k件。閾値未満は返さない。

    ★質問側・文書側とも「名詞だけ」に絞って突き合わせる★
      助詞や活用語尾（「〜は」「〜もらえる」）が類似度に影響しないようにするため。
      文書側の名詞は取り込み時に chunks.content_nouns へ保存済み。

    ※ pg_trgm の similarity() は「文字3つ組の重なり具合」で、厳密なBM25ではない。
      日本語をそのまま扱える手軽さを優先した選択。BM25本来の実装は
      tsvector+ts_rank（日本語は形態素解析器が別途必要）や外部エンジンになる。

    閾値の意図: 類似度0の候補まで順位を持つと、その"偽の順位"がRRFに票を投じて
    しまう（実測で無関係な文書が融合2位に浮上した）。ここで落とせば、
    全件が閾値未満のときは字面リストが空になり、RRFは自然とベクトルの順位だけで
    決まる ＝ 分岐を書かずに「cos類似度のみで評価」が成立する。
    """
    min_similarity = (params or {}).get("min_similarity", LEXICAL_MIN_SIMILARITY)

    query_nouns = noun_text(question)
    if not query_nouns:  # 名詞が1つも無い質問は字面検索を行わない
        return []

    scope, scope_values = _scope_sql(project, topic)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.content, d.source, c.image_path, c.context,
                   similarity(COALESCE(c.content_nouns, ''), %s) AS trgm_similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE similarity(COALESCE(c.content_nouns, ''), %s) >= %s{scope}
            ORDER BY similarity(COALESCE(c.content_nouns, ''), %s) DESC
            LIMIT %s
            """,
            (query_nouns, query_nouns, min_similarity, *scope_values, query_nouns, k),
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1],
            "source": r[2],
            "image_path": r[3],
            "context": r[4],
            "trgm_similarity": float(r[5]),  # 0〜1（1に近いほど字面が一致）
        }
        for r in rows
    ]


def bm25_search(
    question: str,
    k: int = CANDIDATES,
    params: dict | None = None,
    query_vec: list[float] | None = None,  # 使わない（レジストリの引数を揃えるため）
    image_query_vec: list[float] | None = None,  # 同上
    project: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """BM25 で上位k件。名詞列(content_nouns)を単語列とみなして計算する。

    score(D,Q) = Σ_t  IDF(t) · [ f(t,D)·(k1+1) ] / [ f(t,D) + k1·(1 - b + b·|D|/avgdl) ]
    IDF(t)     = ln( (N - n(t) + 0.5) / (n(t) + 0.5) + 1 )

      f(t,D) : 文書D中での語tの出現回数（TF）
      n(t)   : 語tを含む文書数            N : 全文書数
      |D|    : 文書Dの語数               avgdl : 平均語数

    トライグラムとの決定的な違いは IDF。「どの文書にも出る語」を軽く、
    「珍しい語」を重く扱うので、"会社"より"有給"の一致を高く評価できる。

    ※ PostgreSQLの ts_rank は IDF を持たないため使わず、式をそのままSQLで書いている。
      毎回コーパス統計を計算するので大規模では重い（本番は事前集計テーブルにする）。

    project / topic: 指定するとその区分の文書だけを検索対象にする。★絞り込みは
      統計を作る doc CTE に掛ける★ので、N・avgdl・IDF もその区分の中で計算される
      （他プロジェクトの文書数や語の分布が IDF を歪めない）。最後だけ絞ると
      「他区分を含めたコーパスで付けたスコア」を並べ替えることになってしまう。
    """
    p = params or {}
    k1 = p.get("k1", BM25_K1)
    b = p.get("b", BM25_B)

    terms = extract_nouns(question)
    if not terms:
        return []

    scope, scope_values = _scope_sql(project, topic)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            WITH
            -- 質問の名詞（重複除去）
            q AS (
                SELECT DISTINCT term FROM unnest(%s::text[]) AS term
            ),
            -- 各チャンクの名詞を配列に。空文字は NULL にして語数0扱いにする。
            -- documents を JOIN するのは区分で絞るため（chunks.document_id は
            -- NOT NULL + 外部キーなので、絞らないときの件数は変わらない）。
            doc AS (
                SELECT c.id,
                       string_to_array(
                           NULLIF(TRIM(COALESCE(c.content_nouns, '')), ''), ' '
                       ) AS nouns
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                -- ★語を持たない画像チャンクはコーパスから除く★
                -- 含めると語数0の行が N と avgdl を動かし、画像を1枚取り込んだ
                -- だけで既存文書のBM25スコアが変わってしまう。
                -- 自動キャプション(案A)が付いた画像は content_nouns を持つので
                -- 通常のチャンクと同じ資格でコーパスに入る。
                WHERE (c.image_path IS NULL OR c.content_nouns IS NOT NULL){scope}
            ),
            -- |D| : 各文書の語数
            lens AS (
                SELECT id, COALESCE(cardinality(nouns), 0)::float AS doc_len FROM doc
            ),
            -- N と avgdl : コーパス全体の統計
            stats AS (
                SELECT count(*)::float AS n_docs, avg(doc_len) AS avgdl FROM lens
            ),
            -- f(t,D) : 質問に出てくる語だけTFを数える
            tf AS (
                SELECT d.id AS chunk_id, tok AS term, count(*)::float AS f
                FROM doc d, unnest(d.nouns) AS tok
                WHERE tok IN (SELECT term FROM q)
                GROUP BY d.id, tok
            ),
            -- n(t) : その語を含む文書数
            df AS (
                SELECT term, count(DISTINCT chunk_id)::float AS n_t FROM tf GROUP BY term
            ),
            -- IDF(t)
            idf AS (
                SELECT df.term,
                       ln(((s.n_docs - df.n_t + 0.5) / (df.n_t + 0.5)) + 1) AS idf
                FROM df CROSS JOIN stats s
            ),
            -- 語ごとのスコアを文書単位で合算
            scored AS (
                SELECT tf.chunk_id,
                       sum(
                           i.idf * (tf.f * (%s + 1))
                           / (tf.f + %s * (1 - %s + %s * l.doc_len / NULLIF(s.avgdl, 0)))
                       ) AS bm25
                FROM tf
                JOIN idf i ON i.term = tf.term
                JOIN lens l ON l.id = tf.chunk_id
                CROSS JOIN stats s
                GROUP BY tf.chunk_id
            )
            SELECT c.id, c.content, d.source, c.image_path, c.context, sc.bm25
            FROM scored sc
            JOIN chunks c ON c.id = sc.chunk_id
            JOIN documents d ON d.id = c.document_id
            WHERE sc.bm25 > 0
            ORDER BY sc.bm25 DESC
            LIMIT %s
            """,
            (terms, *scope_values, k1, k1, b, b, k),
        ).fetchall()

    return [
        {
            "id": r[0],
            "content": r[1],
            "source": r[2],
            "image_path": r[3],
            "context": r[4],
            "bm25_score": float(r[5]),
        }
        for r in rows
    ]


def _rrf_scores(
    ranked_lists: list[list[dict]], k: int = RRF_K
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


def reciprocal_rank_fusion(ranked_lists: list[list[dict]], k: int = RRF_K) -> list[dict]:
    """複数のランキングを RRF で融合し、スコア降順のアイテム列を返す。"""
    return [item for item, _score, _ranks in _rrf_scores(ranked_lists, k)]


# --- リランクの方式レジストリ -------------------------------------------------
# どちらも (question, passages, retry_waits) -> 関連順の番号リスト という同じ形。
# 方式を足すときはここに1行増やす（呼び出し側は変更不要）。


def _prompt_rerank(
    question: str, passages: list[str], retry_waits: list[int] | None = None
) -> list[int]:
    """プロンプト式リランク。レジストリの引数を揃えるための薄い包み。

    retry_waits は使わない: Anthropic SDK は 429 を内部で数回リトライしてから
    例外にするため、ここで待ち時間を重ねる必要がない（Voyage SDK はしない）。
    """
    return rank_by_relevance(question, passages)


RERANKERS = {
    "voyage": voyage_rerank,      # Voyage 専用リランクAPI(rerank-2)。既定
    "llm": _prompt_rerank,        # Claudeに番号を並べ替えさせるプロンプト式。比較用
}

class UnknownReranker(ValueError):
    """未知のリランク方式が指定された（RERANK_METHOD のtypo等）。"""


def resolve_rerank_method(name: str | None = None) -> str:
    """使用するリランク方式を確定し、妥当性を検証する。

    name=None（未指定）なら設定の既定（RERANK_METHOD）を使う。
    resolve_retrievers と同じ考え方で、APIを呼ぶ前に名前を弾く。
    """
    resolved = (name or RERANK_METHOD).strip().lower()
    if resolved not in RERANKERS:
        raise UnknownReranker(
            f"未知のリランク方式: {resolved} / 利用可能: {', '.join(RERANKERS)}"
        )
    return resolved


def rerank_candidates(
    question: str,
    candidates: list[dict],
    top_n: int = TOP_K,
    method: str | None = None,
    retry_waits: list[int] | None = None,
) -> list[dict]:
    """候補をリランクにかけて関連度で並べ替え、上位top_nを返す。

    RRFは「複数の検索が上位に挙げたか」で決まるが、実際に質問に答えているかは
    見ていない。そこを読み直して最終順位を付け直すのがリランク。
    方式（voyage / llm）は method で切り替える。未指定なら設定の既定。

    retry_waits: レート制限(429)で待つ秒数の並び（app.llm._voyage_call）。
      Web経路は None のまま即429を返し、待っても困らないバッチ（評価）だけが渡す。

    番号を漏らした候補は末尾に補う（安全網：件数が減らないように）。
    voyage は全候補にスコアを付けるので漏れないが、プロンプト式(llm)は
    Claudeが番号を書き落とすことが実際にあるため、方式によらず補っておく。
    """
    if not candidates:
        return []
    reranker = RERANKERS[resolve_rerank_method(method)]
    order = reranker(question, [c["content"] for c in candidates], retry_waits)
    seen = set(order)
    order += [i for i in range(len(candidates)) if i not in seen]
    return [candidates[i] for i in order[:top_n]]



# --- 検索手法のレジストリ -----------------------------------------------------
# 手法を足すときはここに1行増やすだけ。RRFは可変長リストを受けるので変更不要。
RETRIEVERS = {
    "vector": vector_search,
    "trgm": lexical_search,
    "bm25": bm25_search,
    "image": image_search,
}

# 各手法の「生スコア」がどのキーに入っているか
METRIC_KEY = {
    "vector": "cosine_similarity",
    "trgm": "trgm_similarity",
    "bm25": "bm25_score",
    "image": "cosine_similarity",
}

RETRIEVER_META = {
    "vector": {"label": "ベクトル検索（意味）", "metric_label": "cos類似度"},
    "trgm": {"label": "字面検索（名詞トライグラム）", "metric_label": "字面類似度"},
    "bm25": {"label": "BM25全文検索（名詞）", "metric_label": "BM25スコア"},
    "image": {"label": "画像ベクトル検索（マルチモーダル）", "metric_label": "cos類似度"},
}


# --- 調整可能なパラメータの仕様 -----------------------------------------------
# UIのフォームはこの定義から生成する（画面側に定数をハードコードしない）。
PARAM_SPECS: dict[str, list[dict]] = {
    "vector": [],  # コサイン類似度に調整可能な定数はない
    "image": [],   # 同上（画像ベクトルも距離そのもので順位を決める）
    "trgm": [
        {
            "name": "min_similarity",
            "label": "閾値",
            "default": LEXICAL_MIN_SIMILARITY,
            "min": 0.0,
            "max": 1.0,
            "step": 0.001,
            "description": "これ未満の一致はRRFに票を投じない。上げるほどノイズ票が減る",
        },
    ],
    "bm25": [
        {
            "name": "k1",
            "label": "k1（TF飽和）",
            "default": BM25_K1,
            "min": 0.0,
            "max": 3.0,
            "step": 0.1,
            "description": "大きいほど「出現回数が多い」ことを強く評価する（定番 1.2〜2.0）",
        },
        {
            "name": "b",
            "label": "b（長さ正規化）",
            "default": BM25_B,
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "description": "1に近いほど長い文書を不利にする。0で文書長を無視（定番 0.75）",
        },
    ],
}

# 融合そのもののパラメータ（手法によらない）
FUSION_PARAM_SPECS: list[dict] = [
    {
        "name": "rrf_k",
        "label": "RRF k",
        "default": RRF_K,
        "min": 1.0,
        "max": 300.0,
        "step": 1.0,
        "description": "順位差を平滑化する定数。大きいほど1位と2位の差が縮まる",
    },
]


def default_params(name: str) -> dict:
    """その手法の既定パラメータ。"""
    return {spec["name"]: spec["default"] for spec in PARAM_SPECS.get(name, [])}


def retriever_infos() -> list[dict]:
    """選択可能な手法の一覧（UIのチェックボックス用）。"""
    return [
        {
            "name": n,
            "label": m["label"],
            "metric_label": m["metric_label"],
            "params": PARAM_SPECS.get(n, []),
        }
        for n, m in RETRIEVER_META.items()
    ]


class UnknownRetriever(ValueError):
    """未知の検索手法が指定された（/search?retrievers=... のtypo等）。"""


def resolve_retrievers(names: list[str] | None = None) -> list[str]:
    """使用する手法名を確定し、妥当性を検証する。

    names=None（未指定）なら設定の既定（RETRIEVERS_DEFAULT）を使う。
    names=[]（明示的に空）はエラーにする。UIで全ての手法のチェックを外したときに
    黙って既定へ戻して結果を出すと混乱するため、None と [] を区別している。
    未知の名前はここで弾き、SQLを投げる前にエラーにする。
    """
    resolved = list(RETRIEVERS_DEFAULT) if names is None else [n for n in names if n]
    if not resolved:
        raise UnknownRetriever(
            f"検索手法が1つも指定されていません / 利用可能: {', '.join(RETRIEVERS)}"
        )
    unknown = [n for n in resolved if n not in RETRIEVERS]
    if unknown:
        raise UnknownRetriever(
            f"未知の検索手法: {', '.join(unknown)} / 利用可能: {', '.join(RETRIEVERS)}"
        )
    # 同じ手法を2度渡すと票が二重に入るため除去（順序は保つ）
    return list(dict.fromkeys(resolved))


def hybrid_search(
    question: str,
    top_n: int = TOP_K,
    rerank: bool | None = None,
    retrievers: list[str] | None = None,
    params: dict[str, dict] | None = None,
    rrf_k: int | None = None,
    query_vec: list[float] | None = None,
    image_query_vec: list[float] | None = None,
    rerank_method: str | None = None,
    rerank_retry_waits: list[int] | None = None,
    project: str | None = None,
    topic: str | None = None,
) -> list[dict]:
    """指定した検索手法を RRF で融合。rerank=True ならリランクで再並べ替え。

    retrievers 未指定なら設定の既定を使う。手法を増やしても
    reciprocal_rank_fusion は可変長リストを受けるので変更不要。
    rerank を省略すると設定(USE_RERANK)に、rerank_method を省略すると
    設定(RERANK_METHOD)に従う。

    project / topic は各手法へそのまま渡る（未指定＝絞り込まない）。
    """
    if rerank is None:
        rerank = USE_RERANK
    if rerank:
        # 名前の妥当性はAPIを呼ぶ前に検証しておく（typoで検索まで走らせない）
        resolve_rerank_method(rerank_method)

    names = resolve_retrievers(retrievers)
    p = params or {}
    fused = reciprocal_rank_fusion(
        [
            RETRIEVERS[n](
                question,
                params=p.get(n),
                query_vec=query_vec,
                image_query_vec=image_query_vec,
                project=project,
                topic=topic,
            )
            for n in names
        ],
        k=rrf_k if rrf_k is not None else RRF_K,
    )

    if rerank:
        # 融合上位を少し多めにリランク対象へ渡し、そこから top_n を選ばせる
        return rerank_candidates(
            question,
            fused[:RERANK_CANDIDATES],
            top_n,
            method=rerank_method,
            retry_waits=rerank_retry_waits,
        )
    return fused[:top_n]


def preview(text: str, n: int = 80) -> str:
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def search_stages(
    question: str,
    top_n: int = TOP_K,
    retrievers: list[str] | None = None,
    params: dict[str, dict] | None = None,
    rrf_k: int | None = None,
    show: int = 5,
    query_vec: list[float] | None = None,
    image_query_vec: list[float] | None = None,
    project: str | None = None,
    topic: str | None = None,
) -> dict:
    """検索の各段階を返す（学習・デバッグ用）。

    ★Claudeを呼ばないのでANTHROPIC_API_KEYは不要★
    ただし質問のベクトル化で埋め込みAPIを使うためVOYAGE_API_KEYは必要
    （vector を外して trgm/bm25 だけにすれば埋め込みも呼ばない）。

    検索手法の本数に依らない形で返す。手法を足しても構造は変わらない。
    fused の contributions に「どの手法が何位に置き、いくら寄与したか」が入る。
    """
    names = resolve_retrievers(retrievers)
    given = params or {}
    # 未指定のパラメータは既定で埋める（実際に使われた値をそのまま返せるように）
    effective = {n: {**default_params(n), **(given.get(n) or {})} for n in names}
    effective_rrf_k = int(rrf_k if rrf_k is not None else RRF_K)

    lists = [
        RETRIEVERS[n](
            question,
            params=effective[n],
            query_vec=query_vec,
            image_query_vec=image_query_vec,
            project=project,
            topic=topic,
        )
        for n in names
    ]
    scored = _rrf_scores(lists, k=effective_rrf_k)

    # 融合後の行から各手法の生スコアを引くための索引
    by_id = [{h["id"]: h for h in lst} for lst in lists]

    def metric_of(list_index: int, chunk_id: int) -> float | None:
        hit = by_id[list_index].get(chunk_id)
        if hit is None:
            return None
        return round(float(hit[METRIC_KEY[names[list_index]]]), 4)

    stages = [
        {
            "name": name,
            "label": RETRIEVER_META[name]["label"],
            "metric_label": RETRIEVER_META[name]["metric_label"],
            "hits": [
                {
                    "rank": i,
                    "id": h["id"],
                    "source": h["source"],
                    "metric_value": round(float(h[METRIC_KEY[name]]), 4),
                    "preview": preview(h["content"]),
                }
                for i, h in enumerate(lst[:show])
            ],
        }
        for name, lst in zip(names, lists)
    ]

    fused = []
    for i, (item, score, ranks) in enumerate(scored[:top_n]):
        contributions = []
        for li, name in enumerate(names):
            rank = ranks.get(li)  # None = この手法のリストに出てこなかった
            contributions.append(
                {
                    "retriever": name,
                    "rank": rank,
                    "metric_value": metric_of(li, item["id"]),
                    # この手法が RRF スコアに足した分
                    "rrf_term": round(1.0 / (effective_rrf_k + rank + 1), 5)
                    if rank is not None
                    else None,
                }
            )
        fused.append(
            {
                "rank": i,
                "id": item["id"],
                "source": item["source"],
                "score": round(score, 5),
                "contributions": contributions,
                "preview": preview(item["content"]),
            }
        )

    return {
        "question": question,
        "retrievers": names,
        "available_retrievers": retriever_infos(),
        "applied_params": {"rrf_k": effective_rrf_k, "retrievers": effective},
        "lexical_min_similarity": LEXICAL_MIN_SIMILARITY,
        "stages": stages,
        "fused": fused,
    }
