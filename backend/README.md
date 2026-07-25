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

tests/            # 単体テスト（DB・外部APIを使わない純ロジック）
├── test_keywords.py    # 名詞抽出
├── test_parsers.py     # PDF/XLSX/PPTX 抽出
└── test_retrieval.py   # RRF融合・手法解決・整形
```

## テスト

DB も外部API も使わない純ロジックだけを対象にした単体テスト。
リポジトリ直下から一発で回せる（稼働中スタックには触れない）:

```bash
task test
```

中身は「現在のホストソースをマウントした使い捨てコンテナで pytest」。
`app.retrieval` は import 時に psycopg 等のネイティブ依存を読むため、
それが入らない環境（ローカル arm64 で psycopg が x86 ビルド 等）では
`test_retrieval.py` は自動 skip される。コンテナ内では全件実行される。

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
各手法（vector / trgm / bm25）ごとの順位と生スコアが `stages` に、RRF融合後の順位と
「どの手法が何位に置き、いくら寄与したか」の内訳が `fused[].contributions` に入る。
これで「両方の検索が上位に挙げたチャンクが融合で上に来る」挙動を実データで確認できる。

```bash
curl -s 'localhost:8000/search?q=有給は入社何ヶ月で何日？' | jq
# 手法・定数を変えて比較: ?retrievers=vector,trgm,bm25&bm25_k1=2.0&rrf_k=10
```

```jsonc
{
  "question": "有給は入社何ヶ月で何日？",
  "retrievers": ["vector", "trgm"],           // この検索で使った手法の並び
  "applied_params": {                          // 実際に使われた定数（未指定は既定）
    "rrf_k": 60,
    "retrievers": { "vector": {}, "trgm": { "min_similarity": 0.005 } }
  },
  "stages": [                                  // 融合前：手法ごとのランキング
    {
      "name": "vector", "label": "ベクトル検索（意味）", "metric_label": "cos類似度",
      "hits": [ { "rank": 0, "id": 1, "source": "有給休暇.txt",
                  "metric_value": 0.6421, "preview": "年次有給休暇は…" } ]
    },
    {
      "name": "trgm", "label": "字面検索（名詞トライグラム）", "metric_label": "字面類似度",
      "hits": [ { "rank": 0, "id": 7, "source": "経費精算.txt",
                  "metric_value": 0.0102, "preview": "経費精算は翌月10日…" } ]
    }
  ],
  "fused": [                                   // 融合後：最終順位
    {
      "rank": 0, "id": 1, "source": "有給休暇.txt", "score": 0.03252,
      "preview": "年次有給休暇は…",
      "contributions": [                       // 手法ごとの寄与内訳
        { "retriever": "vector", "rank": 0, "metric_value": 0.6421, "rrf_term": 0.01639 },
        { "retriever": "trgm",   "rank": 1, "metric_value": 0.0087, "rrf_term": 0.01613 }
      ]
    }
  ]
}
```

`contributions[].rank` が `null` = その手法のリストには出なかった（片方の検索だけがヒット）。

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
