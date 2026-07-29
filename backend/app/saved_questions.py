"""②で検索した質問の保管と、④でのまとめ検証（RRFの一覧）。

なぜ要るか:
  評価(eval)は「正解ラベル付きの質問集」で Hit@k / MRR を測る仕組みだが、
  正解を用意するのは手間で、実際に聞かれた質問はそのままでは残らない。
  ここでは正解の有無に関わらず ★検索した質問をそのまま貯め★、区分ごとに
  「今の設定だと上位に何が出るか」を一気に見られるようにする。
  正解が無いので採点はしない（○×は出さない）。目視で並びを確かめるための道具。

evalとの違い:
  eval  … 正解ラベルあり・数値(Hit@k/MRR)で良し悪しを判定する
  verify… 正解ラベルなし・上位k件のRRFスコアと出典を眺めて傾向を掴む
"""
from __future__ import annotations

from app.config import TOP_K
from app.db import get_conn
from app.llm import embed_texts
from app.retrieval import resolve_retrievers, search_stages
from app.seed import RETRY_WAITS


def save(question: str, project: str | None = None, topic: str | None = None) -> bool:
    """質問を保管する。同じ区分に同じ質問が既にあれば何もしない。

    追加したら True、重複で見送ったら False。空の質問は保管しない。
    重複判定はDBのユニーク索引に任せる（アプリ側で SELECT してから INSERT すると、
    連打したときに両方が「無い」と判断して二重に入る）。
    """
    question = question.strip()
    if not question:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO saved_questions (project, topic, question) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (project, topic, question) DO NOTHING RETURNING id",
            (project, topic, question),
        ).fetchone()
    return row is not None


def load(project: str | None = None, topic: str | None = None) -> list[dict]:
    """保管済みの質問を返す。project/topic を指定するとその区分だけに絞る。

    指定しなかった軸は絞り込まない（app.eval.load_questions と同じ約束）。
    """
    clauses: list[str] = []
    params: list = []
    if project is not None:
        clauses.append("project = %s")
        params.append(project)
    if topic is not None:
        clauses.append("topic = %s")
        params.append(topic)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, question, project, topic FROM saved_questions {where}"
            "ORDER BY id",
            params,
        ).fetchall()
    return [
        {"id": r[0], "question": r[1], "project": r[2], "topic": r[3]} for r in rows
    ]


def verify(
    project: str | None = None,
    topic: str | None = None,
    top_k: int = TOP_K,
    questions: list[dict] | None = None,
) -> dict:
    """保管済みの質問すべてを検索し、各質問の上位k件（RRF）を返す。

    ★質問の絞り込みと検索スコープに同じ project/topic が効く★
      「その区分の質問を、その区分の文書に対して引く」が揃っていないと、
      別プロジェクトの文書が上位に来て検証にならない。

    ★質問のベクトル化は1回にまとめる★（app.eval.evaluate と同じ理由）
      1問ずつ埋め込むと質問数だけ埋め込みAPIを呼び、Voyage 無料枠(3 RPM)では
      4問目で必ずレート制限に当たって完走しない。
    """
    if questions is None:
        questions = load(project, topic)

    # ベクトル検索を使わない構成（trgm/bm25のみ）なら埋め込みは呼ばない
    if questions and "vector" in resolve_retrievers(None):
        vecs: list[list[float] | None] = list(
            embed_texts(
                [q["question"] for q in questions],
                input_type="query",
                retry_waits=RETRY_WAITS,
            )
        )
    else:
        vecs = [None] * len(questions)

    results = []
    for item, query_vec in zip(questions, vecs):
        stages = search_stages(
            item["question"],
            top_n=top_k,
            project=project,
            topic=topic,
            query_vec=query_vec,
        )
        results.append(
            {
                "question": item["question"],
                "project": item["project"],
                "topic": item["topic"],
                "fused": stages["fused"],
            }
        )
    return {
        "n": len(results),
        "top_k": top_k,
        "project": project,
        "topic": topic,
        "results": results,
    }
