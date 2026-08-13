"""登録済み文書の削除。

取り込み（app.ingest）の逆側。ここだけ独立させているのは、消すときに
「DBの行を消す」以外にやることがあり、その手順に順序の制約があるため:

  1. 消す文書に紐づく画像のS3キーを先に控える（行を消すと辿れなくなる）
  2. documents を消す（chunks は ON DELETE CASCADE で一緒に消える）
  3. 確定後にS3の原本・画像を消す（S3にトランザクションが無いので最後）
  4. 正解ラベルが宙に浮いた評価質問を数えて呼び出し側に返す

★4 について★
  eval_questions.expected_source は documents への外部キーではなく、ただの
  文書名（TEXT）。文書を消しても評価質問は残り、その質問は以後どう検索しても
  正解に辿り着けない＝ Hit@k / MRR が黙って下がる。消させない・道連れに消す、
  という選択もあるが、正解ラベルは人が手で付けた資産なので勝手に消さず、
  「何件が宙に浮いたか」を必ず返して画面に出す方を選んでいる。
"""
from __future__ import annotations

from app import storage
from app.db import get_conn


def delete(ids: list[int]) -> dict:
    """documents.id で指定した文書を消す。

    戻り値は {"deleted", "sources", "missing_ids", "orphaned_questions",
    "orphaned_sources"}。

    ★source ではなく id で受ける★
      documents.source は UNIQUE ではなく、同名の行が2つある状態はこの画面が
      見せたい異常のひとつ。source で消すと「二重登録の片方だけ消す」ができない。

    S3の原本（キー = 出典名）を消すのは、その出典名の行が1つも残らなかった
    ときだけ。同名の行が他に残っているなら、その行の原本としてまだ使われている。
    """
    if not ids:
        return {
            "deleted": 0,
            "sources": [],
            "missing_ids": [],
            "orphaned_questions": 0,
            "orphaned_sources": [],
        }

    with get_conn() as conn:
        found = dict(
            conn.execute(
                "SELECT id, source FROM documents WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        )
        missing_ids = [i for i in ids if i not in found]
        if not found:
            return {
                "deleted": 0,
                "sources": [],
                "missing_ids": missing_ids,
                "orphaned_questions": 0,
                "orphaned_sources": [],
            }

        target_ids = list(found)
        sources = sorted(set(found.values()))
        # 行を消すと image_path を辿れなくなるので、消す前に控える
        image_keys = [
            r[0]
            for r in conn.execute(
                "SELECT image_path FROM chunks "
                "WHERE document_id = ANY(%s) AND image_path IS NOT NULL",
                (target_ids,),
            ).fetchall()
        ]

        with conn.transaction():
            deleted = conn.execute(
                "DELETE FROM documents WHERE id = ANY(%s)", (target_ids,)
            ).rowcount

        # 同名の行がまだ残っている出典名は、原本をこれからも使う
        remaining = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT source FROM documents WHERE source = ANY(%s)",
                (sources,),
            ).fetchall()
        }
        gone = sorted(set(sources) - remaining)

        # 正解ラベルが指す文書が1件も無くなった評価質問
        orphan_rows = conn.execute(
            "SELECT expected_source, count(*) FROM eval_questions "
            "WHERE expected_source = ANY(%s) GROUP BY expected_source",
            (gone,),
        ).fetchall()

    storage.delete_objects(image_keys + gone)

    return {
        "deleted": deleted,
        "sources": sources,
        "missing_ids": missing_ids,
        "orphaned_questions": sum(int(r[1]) for r in orphan_rows),
        "orphaned_sources": sorted(str(r[0]) for r in orphan_rows),
    }
