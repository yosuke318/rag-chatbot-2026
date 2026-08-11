"""貯めた 👍/👎 の読み出しと、記録済みの行への後追いの書き込み（理由・昇格）。
最初の記録（POST /feedback）だけは app.main 側にある。

なぜ要るか:
  feedback テーブルは長らく INSERT だけで、読む口が一つも無かった。評価の素材と
  して貯めているのに誰も見られないなら、記録していないのと変わらない。

★一覧は「改善のインプット」ではなく「調査対象を絞り込むフィルタ」★
  👎 1件からは、検索が外したのか・生成が外したのか・そもそも文書に答えが
  無かったのかを区別できない。だからここが返すのは結論ではなく、どこを見に
  行くかを決めるための材料（いつ・どの区分で・どんな条件で出た回答か）になる。
"""
from __future__ import annotations

from datetime import datetime

from app.db import get_conn

# 一覧の既定件数と上限。全件返す選択を取らないのは文書一覧と同じ理由
# （フィードバックは運用で増える一方）。上限は黙って効かせる。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 👎の理由の選択肢。★ここが正★ で、UIは GET /feedback/reasons で受け取る
# （画面に文言を焼くと、増やしたときに片方だけ古くなる）。
#
# ★1クリックで選べる短い並びにする★
#   自由記述だけにすると大半が無記入になり、選択肢を増やしすぎると読まずに
#   先頭を押される。数えて意味があるのは「どの工程を疑うか」が変わる粒度まで:
#     情報が古い       … 文書の更新漏れ（取り込み側の問題）
#     見つからない     … 根拠が引けていない（検索の問題）
#     内容が間違っている … 根拠はあるのに答えが違う（生成の問題）
#     読みにくい       … 中身は合っているが伝わらない（表現の問題）
REASONS = ["情報が古い", "見つからない", "内容が間違っている", "読みにくい"]

# 時系列の刻み。Postgres の date_trunc に渡す単位で、ここに無い文字列は受け付けない
# （単位はSQLに文字列として入るので、素通しにすると何を渡されるか分からなくなる）。
BUCKETS = ["day", "week", "month"]
DEFAULT_BUCKET = "day"

# JOIN は一覧・件数・集計で共通。区分なしの行を落とさないため LEFT。
_JOINS = (
    "FROM feedback f "
    "LEFT JOIN projects p ON p.id = f.project_id "
    "LEFT JOIN topics t ON t.id = f.topic_id "
)

# 👎率のもとになる2つの数。COUNT(*) FILTER は「その条件の行だけ数える」書き方で、
# 全体と👎を1回のスキャンで同時に出せる（2本撃つと期間の切り方がずれる余地が残る）。
_COUNTS = "COUNT(*), COUNT(*) FILTER (WHERE f.rating = -1)"


def _rate(total: int, down: int) -> dict:
    """件数から👎率を組み立てる。0件のときの率は null（0.0 ではない）。

    まだ誰も評価していない区分を「👎率0%＝良い」と読ませないため。0件と
    「10件あって0件が👎」は別の話で、後者だけが良い状態を意味する。
    """
    return {
        "total": total,
        "down": down,
        "down_rate": (down / total) if total else None,
    }


def _where(
    rating: int | None,
    project: str | None,
    topic: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[str, list]:
    """絞り込みの WHERE 句と値を組み立てる。一覧と件数で同じものを使う。

    区分は行が id で持つので、名前はマスタへの LEFT JOIN 側で比べる
    （app.saved_questions.load と同じ形。名前で絞ると区分なしの行は
    NULL と `=` が一致しないので自然に外れる）。
    """
    clauses: list[str] = []
    params: list = []
    if rating is not None:
        clauses.append("f.rating = %s")
        params.append(rating)
    if project is not None:
        clauses.append("p.name = %s")
        params.append(project)
    if topic is not None:
        clauses.append("t.name = %s")
        params.append(topic)
    # 期間は since 以上 until 未満（半開区間）。両端を含めると、月ごとに
    # 区切って眺めたときに境界の1件が両方の月に出て二重に数えられる。
    if since is not None:
        clauses.append("f.created_at >= %s")
        params.append(since)
    if until is not None:
        clauses.append("f.created_at < %s")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    return where, params


def load(
    rating: int | None = None,
    project: str | None = None,
    topic: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    """条件に合うフィードバックを新しい順で返す。

    指定しなかった軸は絞り込まない（app.saved_questions.load と同じ約束）。
    絞り込んだ結果の総件数も返す: ページ送りは offset で行うので、総数が無いと
    「次のページがあるか」も「今どこを見ているか」も画面に出せない。
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    where, params = _where(rating, project, topic, since, until)
    joins = _JOINS
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) {joins}{where}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT f.id, f.question, f.answer, f.sources, f.rating, f.reason, "
            "f.comment, f.created_at, p.name, t.name, f.conversation_id, "
            "f.message_id, f.retriever, f.top_k, f.reranked, f.chunk_ids, "
            "f.latency_ms, f.promoted_eval_question_id "
            f"{joins}{where}"
            # 新しい順。NULLS LAST は created_at を持たない古い行を末尾に送るため
            # （既定の DESC では NULL が先頭に来て、一番新しく見えてしまう）。
            "ORDER BY f.created_at DESC NULLS LAST, f.id DESC "
            "LIMIT %s OFFSET %s",
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "feedback": [
            {
                "id": r[0],
                "question": r[1],
                "answer": r[2],
                "sources": r[3],
                "rating": r[4],
                "reason": r[5],
                "comment": r[6],
                "created_at": r[7],
                "project": r[8],
                "topic": r[9],
                "conversation_id": r[10],
                "message_id": r[11],
                "retriever": r[12],
                "top_k": r[13],
                "reranked": r[14],
                "chunk_ids": r[15],
                "latency_ms": r[16],
                "promoted_eval_question_id": r[17],
            }
            for r in rows
        ],
    }


def stats(
    project: str | None = None,
    topic: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    bucket: str = DEFAULT_BUCKET,
) -> dict:
    """👎率を、全体・区分別・時系列・理由別に数える。

    ★rating では絞らない★
      率を出すには分母（その区分・その日の全評価）が要る。👎だけを渡されても
      「👎が3件」までしか言えず、それが多いのか少ないのか分からない。

    ★見るのは全体の値ではなく偏り★
      全体の👎率はまず動かない。意味があるのは「この区分だけ突出している」
      「この日から上がった」で、それが調べに行く先を1つに絞ってくれる。
      分母の小さい区分は率が跳ねるので、率と一緒に件数も返す。
    """
    if bucket not in BUCKETS:
        raise ValueError(f"未知の刻み: {bucket}")
    where, params = _where(None, project, topic, since, until)
    with get_conn() as conn:
        total, down = conn.execute(
            f"SELECT {_COUNTS} {_JOINS}{where}", params
        ).fetchone()
        by_scope = conn.execute(
            f"SELECT p.name, t.name, {_COUNTS} {_JOINS}{where}"
            "GROUP BY p.name, t.name "
            # 👎率の高い順。件数を第2キーにするのは、1件だけの区分が率100%で
            # 先頭に居座るのを少しでも抑えるため（それでも件数は目で見る）。
            "ORDER BY (COUNT(*) FILTER (WHERE f.rating = -1))::float / COUNT(*) "
            "DESC, COUNT(*) DESC",
            params,
        ).fetchall()
        # 日時を持たない古い行は時系列に置けないので外す（率の分母からも外れる）。
        period_where = (
            f"{where}AND f.created_at IS NOT NULL "
            if where
            else "WHERE f.created_at IS NOT NULL "
        )
        by_period = conn.execute(
            f"SELECT date_trunc(%s, f.created_at) AS bucket, {_COUNTS} "
            f"{_JOINS}{period_where}"
            "GROUP BY bucket ORDER BY bucket",
            [bucket, *params],
        ).fetchall()
        # 理由は👎にしか付かないので、ここだけ rating で絞る（分母は上の down）。
        by_reason = conn.execute(
            f"SELECT f.reason, COUNT(*) {_JOINS}"
            f"{where}{'AND' if where else 'WHERE'} f.rating = -1 "
            "GROUP BY f.reason ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
    return {
        **_rate(total, down),
        "bucket": bucket,
        "by_scope": [
            {"project": r[0], "topic": r[1], **_rate(r[2], r[3])} for r in by_scope
        ],
        "by_period": [
            {"period": r[0], **_rate(r[1], r[2])} for r in by_period
        ],
        # 理由を選ばなかった👎は reason=None のまま1行として出す（黙って
        # 落とすと「理由付きの👎しか無い」ように見え、収集率を見誤る）。
        "by_reason": [{"reason": r[0], "count": r[1]} for r in by_reason],
    }


def add_reason(
    feedback_id: int, reason: str | None, comment: str | None
) -> dict | None:
    """既に記録した評価に、後から理由と自由記述を足す。行が無ければ None。

    ★評価と理由を別の操作に分ける★
      👎 は押した瞬間に記録し、理由はその後で任意に足す。理由を選ぶまで送信を
      待つと、押しただけで画面を離れた人の👎が丸ごと消える（一番多い操作を
      一番落としやすい作りになる）。理由なしの👎はそのまま「理由なし」として残る。

    渡さなかった項目（None）は触らない。理由だけ選んで自由記述は書かない、が
    普通の使われ方なので、片方の指定でもう片方が消えると押し間違いが起きる。
    どちらも None なら書き換える先が無いので ValueError
    （app.scopes.create_project が空の名前を弾くのと同じ扱い）。

    ★理由を足せるのは👎だけ★
      REASONS は「👎の理由」で、stats の by_reason も👎だけを数える。👍に理由が
      入ると、集計の分母（👎の数）と理由の合計が合わなくなり、読んだ人が
      「理由なしの👎が何件か」を数え違える。条件をSQLに入れて弾くので、
      更新の直前に評価が変わっても👍に書き込まれることはない。
      自由記述(comment)は「👍だが一言ある」があり得るので制限しない。
    """
    sets: list[str] = []
    params: list = []
    only_down = ""
    if reason is not None:
        sets.append("reason = %s")
        params.append(reason)
        only_down = " AND rating = -1"
    if comment is not None:
        sets.append("comment = %s")
        params.append(comment)
    if not sets:
        raise ValueError("理由も自由記述も指定されていません")
    # 書き換えた後の値をそのまま返す（呼び出し側が「何が入ったか」を組み立て直すと、
    # 触らなかった項目の扱いを2か所で決めることになる）。
    with get_conn() as conn:
        row = conn.execute(
            f"UPDATE feedback SET {', '.join(sets)} WHERE id = %s{only_down} "
            "RETURNING id, rating, reason, comment",
            [*params, feedback_id],
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "rating": row[1], "reason": row[2], "comment": row[3]}


def promote(
    feedback_id: int,
    question: str,
    expected_source: str,
    expected_kind: str,
    expected_text: str | None,
    project_id: int | None,
    topic_id: int | None,
    note: str | None,
) -> int | None:
    """この👎を評価用質問(eval_questions)として登録し、昇格済みの印を付ける。

    作った質問のIDを返す。既に昇格済み、または行が無ければ None
    （どちらだったかは promoted_of で分ける。呼ぶのは失敗した後だけ）。

    ★機械的に流し込む口ではない★
      ここに渡す正解(expected_source)は人が選び直したもの。eval_questions は
      expected_source NOT NULL（正解必須）の設計で、「そもそも文書に答えが無い」
      質問を混ぜると、引けなくて当然のものを不正解として数えることになり、
      Hit@k / MRR そのものが読めなくなる。だから👎を一括で昇格する関数は作らない。

    ★登録と印付けをSQL1文で行う★
      2文に分けると、印を付ける前に落ちたときに「評価用質問だけが増え、元の👎は
      未昇格のまま」が残り、次に押すと同じ質問がもう1件できる。CTEにすると
      「未昇格の行が1件見つかったときだけ INSERT する」が不可分に決まる。
      FOR UPDATE で行を押さえるのは、同時に2回押されたときに後から来た方が
      条件（未昇格）を評価し直して0件になるようにするため。
    """
    with get_conn() as conn:
        row = conn.execute(
            "WITH target AS ("
            "    SELECT id FROM feedback"
            "     WHERE id = %s AND promoted_eval_question_id IS NULL"
            "     FOR UPDATE"
            "), created AS ("
            "    INSERT INTO eval_questions"
            "        (project_id, topic_id, question, expected_source,"
            "         expected_kind, expected_text, note)"
            "    SELECT %s::bigint, %s::bigint, %s::text, %s::text,"
            "           %s::text, %s::text, %s::text FROM target"
            "    RETURNING id"
            ")"
            "UPDATE feedback f SET promoted_eval_question_id = created.id"
            "  FROM created WHERE f.id = (SELECT id FROM target)"
            " RETURNING f.promoted_eval_question_id",
            (
                feedback_id,
                project_id,
                topic_id,
                question,
                expected_source,
                expected_kind,
                expected_text,
                note,
            ),
        ).fetchone()
    return None if row is None else row[0]


def promoted_of(feedback_id: int) -> int | None:
    """昇格済みなら、その評価用質問のID。未昇格・行なしは None。

    promote が0件だったときに「もう昇格されている」のか「そんな行は無い」のかを
    分けるために使う（rating_of と同じ役回りで、呼ぶのは失敗した後だけ）。
    既に作られている質問のIDを返せると、画面が「どれになったか」を指せる。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT promoted_eval_question_id FROM feedback WHERE id = %s",
            (feedback_id,),
        ).fetchone()
    return None if row is None else row[0]


def rating_of(feedback_id: int) -> int | None:
    """記録済みの評価（+1 / -1）。行が無ければ None。

    add_reason が0件だったときに「行が無い」のか「👍だったので弾かれた」のかを
    分けるためだけに使う。呼ぶのは失敗した後だけなので、通常の更新はSQL1発のまま。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rating FROM feedback WHERE id = %s", (feedback_id,)
        ).fetchone()
    return None if row is None else row[0]
