"""デフォルト文書の一括投入。

backend/seed_docs/ に置いた .txt を順に取り込む。
文書を足したい場合は seed_docs/ に .txt を置くだけ（拡張可能）。

★埋め込みAPIのレート制限について★
  ingest_text は「1文書 = 1回の embed 呼び出し」なので、文書を間を置かずに
  回すと埋め込みAPIの分間リクエスト上限（Voyage の無料枠は 3 RPM）に当たる。
  ここはバッチ処理で待っても困らないため、429 を受けたら待って再試行する。
  API経由の /ingest は利用者を待たせたくないので、そちらは 429 を即返す
  （main.py の例外ハンドラ）ままにしてある。

使い方:
  python -m app.seed              # seed_docs/ の全 .txt を投入
  python -m app.seed foo.txt      # seed_docs/foo.txt だけ投入
"""
import logging
import os
import sys
from pathlib import Path

from app.db import init_db
from app.ingest import ingest_text

SEED_DIR = Path(__file__).resolve().parent.parent / "seed_docs"

# 429 を受けたときの再試行。無料枠(3 RPM)なら20秒待てば枠が空く。
# 有料枠なら制限に当たらないので、この待ちは実質発生しない。
RETRY_WAITS = [int(w) for w in os.getenv("SEED_RETRY_WAITS", "20,40,60").split(",")]


def main() -> None:
    # 再試行やフォールバックは llm.py が WARNING に出す。既定では表示されないので
    # CLI として動かすときだけロギングを有効にする（何が起きたか見えるように）。
    # WARNING 止まりにするのは、boto3/voyage の INFO ログで進捗が埋もれるため。
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    init_db()  # スキーマが無ければ作る（単体実行できるように）

    if len(sys.argv) > 1:
        files = [SEED_DIR / name for name in sys.argv[1:]]
    else:
        files = sorted(SEED_DIR.glob("*.txt"))

    if not files:
        print(f"投入対象がありません: {SEED_DIR}")
        return

    for f in files:
        if not f.exists():
            print(f"見つかりません: {f}")
            continue
        r = ingest_text(
            source=f.name,
            text=f.read_text(encoding="utf-8"),
            embed_retry_waits=RETRY_WAITS,
        )
        note = "（既存を置き換え）" if r["replaced"] else ""
        print(f"{f.name}: {r['chunks_created']} チャンク登録{note}", flush=True)


if __name__ == "__main__":
    main()
