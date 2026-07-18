# backend — 最小RAG

文書を入れて質問すると、根拠付きで答える最小のRAG API。
`テキスト投入 → チャンク → 埋め込み(Voyage) → pgvector保存 → ベクトル検索 → 回答生成(Claude)` が通しで動く。

## 構成

```
app/
├── config.py     # 環境変数
├── db.py         # pgvector 接続 + スキーマ初期化
├── llm.py        # 埋め込み(Voyage) + 回答生成(Claude)
├── ingest.py     # テキスト→チャンク→埋め込み→保存
├── retrieval.py  # ★ハイブリッド検索（ベクトル + 字面 + RRF融合）
└── main.py       # FastAPI: /health /ingest /chat
```

## 動かす手順

```bash
# 1. pgvector 付き Postgres を起動
cd backend
docker compose up -d

# 2. 依存インストール（venv 推奨）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. 環境変数（APIキー2つ）
cp .env.example .env
#   ANTHROPIC_API_KEY と VOYAGE_API_KEY を .env に記入

# 4. 起動（起動時にスキーマ自動作成）
uvicorn app.main:app --reload
```

## 試す

```bash
# 文書を投入
curl -X POST localhost:8000/ingest -H 'content-type: application/json' -d '{
  "source": "有給休暇.txt",
  "category": "就業規則",
  "text": "年次有給休暇は入社6か月後に10日付与される。取得は前日までに申請すること。"
}'

# 質問する
curl -X POST localhost:8000/chat -H 'content-type: application/json' -d '{
  "question": "有給は入社何か月で何日もらえる?"
}'
# => {"answer":"入社6か月後に10日付与されます。","sources":["有給休暇.txt"]}
```

## この最小版で「やっていないこと」（＝次にやると学びが深い）

- **LLMリランク** … `retrieval.py` の `llm_rerank` はまだ TODO スタブ。
  まず RRF 版で精度を測り、リランク有り/無しを評価で比較するのが目的
- ハイブリッド検索の字面側は pg_trgm（トライグラム）で、厳密なBM25ではない。
  日本語BM25が欲しくなったら PGroonga 等の日本語対応エンジンに差し替える
- PDF/docx/xlsx 取り込み・図表のマルチモーダル文章化（今はプレーンテキストのみ）
- contextual retrieval（チャンクに文書要約を前置き）／差分再取り込み
- 回答のストリーミング（今は生成完了後に一括返却）
- 会話履歴の保持、評価（Ragas/promptfoo）
