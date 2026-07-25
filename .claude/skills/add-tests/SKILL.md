---
name: add-tests
description: >-
  Use whenever implementing new backend logic (backend/app/*.py) or fixing a bug
  there — new functions, new/changed FastAPI endpoints, changed retrieval/ingest/
  parsing behavior — so the change ships with test coverage. Also use for
  frontend/app/api/backend/[...path]/route.ts proxy logic changes. Trigger on
  requests like "実装して", "追加して", "直して", "テスト追加して", "add tests"
  when they touch runtime logic. Do NOT use for docs, config-only, Dockerfile/CI,
  copy/wording, or pure-rename changes — nothing to assert there.
---

# テストを実装に添える（rag-chatbot-2026）

このリポジトリには現時点でテストランナーが入っていない
(`backend/requirements.txt` に pytest なし、`frontend/package.json` に test
スクリプトなし)。実装のたびに「後で入れる」で先送りにされがちなので、この
skill はロジックを実装/修正するたびに最小限のテストを一緒に足すためのもの。

## 対象を見極める

まずその変更に「ロジック」があるか確認する。ロジックが無ければテストは書かない
（`短い方が良い` — 意味の無いテストはノイズ）。

- **必ず書く**: 純粋関数の新規追加・変更（`keywords.py`, `ingest.chunk_text`,
  `retrieval._rrf_scores`, `retrieval.bm25_search` の統計計算部分, `parsers.py`
  のバイト列→テキスト抽出など）。入力→出力が決定的で、DBや外部APIを叩かない。
- **必ず書く**: DB依存のロジック（`vector_search`, `lexical_search`,
  `ingest_text`, 新しいSQL）。統合テストとして書く（下記参照）。
- **必ず書く**: 新規/変更したFastAPIエンドポイント（`main.py`）。
  `TestClient` でリクエスト/レスポンスの形と主要な分岐（正常系・バリデーション
  エラー・存在しないIDなど）を確認する。
- **必ず書く**: `frontend/app/api/backend/[...path]/route.ts` の分岐変更
  （content-type中継、GET/POST、バイナリ素通しなど）。過去に「JSON固定で
  multipartのboundaryが壊れた」「text()で読んでバイナリが壊れた」という実際の
  バグがこのファイルで起きている＝退行しやすい場所。
- **書かない**: README/docs、`.env.example`、Dockerfile、CI yaml、コメントの
  言い回し変更、UIの文言・見た目だけの変更、名前のリネームのみの変更。

## バックエンド: pytest

### 初回だけ: ブートストラップ

`backend/tests/` が無ければ作る。

1. `backend/requirements.txt` に `pytest` を追記（開発用。本番イメージに混ざる
   のが気になる場合は `backend/requirements-dev.txt` を切って Dockerfile 側は
   `requirements.txt` のみ入れる形にしてよい — 既存の `requirements.txt` が
   単一ファイル運用なので、まずはそこに足して様子を見る）。
2. `backend/tests/__init__.py`（空）と `backend/tests/conftest.py` を作る。
3. `backend/pyproject.toml` に最小の pytest 設定を足す（`[tool.pytest.ini_options]
   testpaths = ["tests"]`）。無ければファイルごと新規作成してよい。

### 何にDBを使うか

`db.py` の `get_conn()` は `DATABASE_URL` を見て毎回接続する薄い作りなので、
モックDBを作る価値は薄い。統合テストは **docker compose の `db` サービス
（pgvector入りPostgres）に実際に繋いで** 実行する方針にする。

- テストは `backend/.env` の `DATABASE_URL` をそのまま使う（`docker compose up
  -d db` が起動済みであることが前提）。CIで動かす場合は別途 `db` サービスを
  立てるジョブ設定が要るが、それはこの skill の範囲外（テストを書く時点では
  ローカルで通ることを確認すれば十分）。
- 各テストは自分が作った `documents`/`chunks` 行を **必ず後片付けする**
  （`yield` 後に `DELETE FROM documents WHERE source = %s` する fixture を
  `conftest.py` に用意する、など）。他のテストや seed データを壊さない。
- pgvectorの演算子キャストで一度事故っているので（`<=>` に生の `list` を渡すと
  `double precision[]` に化けて失敗した実例あり）、ベクトル検索のテストでは
  「結果が返る」だけでなく **距離の大小関係が意図通りか** も1件はアサートする。

### LLM/埋め込み呼び出しはモックする

`llm.py` の `embed_texts` / `embed_query` / `generate_answer` /
`rank_by_relevance` は実APIを叩く薄い関数として既に切り出されている。テストで
本物の Voyage/Anthropic を呼ぶとコスト・レイテンシ・APIキー要件が乗るので、
**呼び出し元のモジュールでこの関数をモックする**
（例: `monkeypatch.setattr(retrieval, "embed_query", lambda q: [0.1] * EMBED_DIM)`）。
実APIを叩く疎通確認がしたい場合は明示的に `@pytest.mark.integration` などで
分けて既定では実行しない。

### エンドポイントテスト

`from fastapi.testclient import TestClient` + `from app.main import app` で
`TestClient(app)` を使う。`llm.py` 側の関数をモックしておけば `/chat` も
APIキー無しでテストできる。

## フロントエンド: route.ts のテスト

`frontend/app/api/backend/[...path]/route.ts` はロジック（content-type中継・
GET/POST分岐・バイナリ素通し）を持つ数少ないファイル。ここを変更したら
Vitest でテストを足す。

- 初回のみ: `frontend/package.json` の devDependencies に `vitest` を追加し、
  `"test": "vitest run"` スクリプトを足す。
- `fetch` はモックし、`route.ts` の `proxy()` が「content-typeを転記する
  か」「GETでbodyを送らないか」「ダウンロード用の`content-disposition`を
  転記するか」を検証する。ブラウザやNext.jsのランタイムは不要、関数単体で
  テストできる形を保つ。

## 実行して確認する

- backend: `cd backend && python -m pytest`（`docker compose up -d db` 済み
  であること）
- frontend: `cd frontend && npm run test`

テストを書いたら必ず実行し、グリーンになってから実装完了として報告する。
