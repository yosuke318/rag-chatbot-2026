"""デフォルト文書の一括投入。

backend/seed_docs/ に置いた .txt を順に取り込む。
文書を足したい場合は seed_docs/ に .txt を置くだけ（拡張可能）。

文書の区分（project / topic）は seed_data/documents.json で指定する。
本文とは別ファイルにしてあるのは、seed_docs を「.txt を置くだけ」に保つため。
載っていないファイルは区分なし(NULL)＝どこにも属さない共通文書として入る。

★埋め込みAPIのレート制限について★
  ingest_text は「1文書 = 1回の embed 呼び出し」なので、文書を間を置かずに
  回すと埋め込みAPIの分間リクエスト上限（Voyage の無料枠は 3 RPM）に当たる。
  ここはバッチ処理で待っても困らないため、429 を受けたら待って再試行する。
  API経由の /ingest は利用者を待たせたくないので、そちらは 429 を即返す
  （main.py の例外ハンドラ）ままにしてある。

使い方:
  python -m app.seed              # seed_docs/ の全 .txt を投入
  python -m app.seed foo.txt      # seed_docs/foo.txt だけ投入
  python -m app.seed --corpus     # 評価専用コーパス(eval_corpus/docs)を投入

★デモ用の seed_docs と評価用コーパスを分けている理由★
  評価用コーパスは数百チャンクある（指標を飽和させないため意図的に大きい）。
  これを seed_docs に混ぜると、UIを触るための `task seed` も、contextual の
  比較評価も、毎回そのすべてを埋め込み直すことになり、時間とAPIの費用が常に
  かかる。コーパスは --corpus を付けたときだけ触る。
"""
import argparse
import json
import logging
import os
from pathlib import Path

from app.db import init_db
from app.ingest import ingest_text

_BACKEND = Path(__file__).resolve().parent.parent

SEED_DIR = _BACKEND / "seed_docs"
SCOPES_PATH = _BACKEND / "seed_data" / "documents.json"

# 評価専用コーパス。指標が飽和しない規模と紛らわしさを持たせた文書群。
CORPUS_DIR = _BACKEND / "eval_corpus" / "docs"
CORPUS_SCOPES_PATH = _BACKEND / "eval_corpus" / "documents.json"
# コーパスの文書と質問に付ける区分。評価はこの project で絞ることで
# 「コーパスの質問をコーパスの文書に対して引く」を揃える（デモ文書が混ざらない）。
CORPUS_PROJECT = "評価コーパス"

# 429 を受けたときの再試行。無料枠(3 RPM)なら20秒待てば枠が空く。
# 有料枠なら制限に当たらないので、この待ちは実質発生しない。
RETRY_WAITS = [int(w) for w in os.getenv("SEED_RETRY_WAITS", "20,40,60").split(",")]


def load_scopes(path: Path | None = None) -> dict[str, dict]:
    """文書名 → {"project", "topic"} の対応を読む。ファイルが無ければ空。"""
    path = SCOPES_PATH if path is None else path
    if not path.exists():
        return {}
    return {
        item["source"]: {
            "project": item.get("project"),
            "topic": item.get("topic"),
        }
        for item in json.loads(path.read_text(encoding="utf-8"))
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="文書の一括投入")
    parser.add_argument(
        "names",
        nargs="*",
        help="投入するファイル名（省略すると対象ディレクトリの全 .txt）",
    )
    parser.add_argument(
        "--corpus",
        action="store_true",
        help="評価専用コーパス(eval_corpus/docs)を投入する（既定は seed_docs）",
    )
    args = parser.parse_args()

    # 再試行やフォールバックは llm.py が WARNING に出す。既定では表示されないので
    # CLI として動かすときだけロギングを有効にする（何が起きたか見えるように）。
    # WARNING 止まりにするのは、boto3/voyage の INFO ログで進捗が埋もれるため。
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    init_db()  # スキーマが無ければ作る（単体実行できるように）
    directory = CORPUS_DIR if args.corpus else SEED_DIR
    scopes = load_scopes(CORPUS_SCOPES_PATH if args.corpus else SCOPES_PATH)

    if args.names:
        files = [directory / name for name in args.names]
    else:
        files = sorted(directory.glob("*.txt"))

    if not files:
        print(f"投入対象がありません: {directory}")
        return

    for f in files:
        if not f.exists():
            print(f"見つかりません: {f}")
            continue
        scope = scopes.get(f.name, {})
        r = ingest_text(
            source=f.name,
            text=f.read_text(encoding="utf-8"),
            project=scope.get("project"),
            topic=scope.get("topic"),
            embed_retry_waits=RETRY_WAITS,
        )
        where = " / ".join(filter(None, [scope.get("project"), scope.get("topic")]))
        # skipped = 内容が前回と同じで埋め込みをやり直していない（差分検知）。
        # 2回目以降の seed はここに来るので、登録との区別が付くよう文言を分ける。
        if r["skipped"]:
            body = f"{f.name}: 変更なし（{r['chunks_created']} チャンクのまま）"
        else:
            note = "（既存を置き換え）" if r["replaced"] else ""
            body = f"{f.name}: {r['chunks_created']} チャンク登録{note}"
        print(f"{body}{f' [{where}]' if where else ''}", flush=True)


if __name__ == "__main__":
    main()
