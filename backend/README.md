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

**フルスタック（推奨）はリポジトリ直下の `task` から**（[../README.md](../README.md) 参照）:

```bash
cp backend/.env.example backend/.env   # キー2つを記入
task up        # db + backend + frontend を起動
task seed      # seed_docs/ のデフォルト文書を投入
```

**バックエンドだけホストで動かして開発**（ホットリロード）:

```bash
docker compose up -d db          # DBだけ docker で
cp .env.example .env             # DATABASE_URL は localhost:5432 のまま
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload    # 起動時にスキーマ自動作成
python -m app.seed               # 文書投入
```

## 検索の中身を見る（Anthropicキー不要・Voyageキーは必要）

`/search` は Claude を呼ばないので **ANTHROPIC_API_KEY なしで動く**。
ただし質問をベクトル化するため **VOYAGE_API_KEY は必要**（文書登録と同じ埋め込みモデルを使う）。
`?retrievers=trgm,bm25` のようにベクトル検索を外せば、埋め込みAPIも呼ばずに動かせる。
ベクトル/字面それぞれの順位と、RRF融合後のスコアが返るので、
「両方の検索が上位に挙げたチャンクが融合で上に来る」挙動を実データで確認できる。

```bash
curl -s 'localhost:8000/search?q=有給は入社何ヶ月で何日？' | jq
```

```jsonc
{
  "vector_search": [ { "rank": 0, "id": 1, "preview": "年次有給休暇は…" } ],
  "lexical_search": [ { "rank": 0, "id": 7, "preview": "経費精算は翌月10日…" } ],
  "fused": [
    { "rank": 0, "id": 1, "score": 0.03252,
      "vector_rank": 0, "lexical_rank": 1,   // 両方に出た → 上位
      "preview": "年次有給休暇は…" }
  ]
}
```

`vector_rank` / `lexical_rank` が `null` = そのリストには出なかった（片方の検索だけがヒット）。

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
