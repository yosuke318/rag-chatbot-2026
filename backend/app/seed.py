"""デフォルト文書の一括投入。

backend/seed_docs/ に置いた .txt を順に取り込む。
文書を足したい場合は seed_docs/ に .txt を置くだけ（拡張可能）。

使い方:
  python -m app.seed              # seed_docs/ の全 .txt を投入
  python -m app.seed foo.txt      # seed_docs/foo.txt だけ投入
"""
import sys
from pathlib import Path

from app.db import init_db
from app.ingest import ingest_text

SEED_DIR = Path(__file__).resolve().parent.parent / "seed_docs"


def main() -> None:
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
        n = ingest_text(source=f.name, text=f.read_text(encoding="utf-8"))
        print(f"{f.name}: {n} チャンク登録")


if __name__ == "__main__":
    main()
