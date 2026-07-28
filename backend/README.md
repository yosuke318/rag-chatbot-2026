# backend — 最小RAG

文書を入れて質問すると、根拠付きで答える最小のRAG API。
`テキスト投入 → チャンク → 埋め込み(Voyage) → pgvector保存 → ベクトル検索 → 回答生成(Claude)` が通しで動く。

## 構成

```
app/
├── config.py     # 環境変数
├── db.py         # pgvector 接続 + スキーマ初期化
├── llm.py        # 埋め込み(Voyage) + 文脈生成/回答生成(Claude)
├── chunking.py   # ★チャンク分割（見出し・条文の構造で切る）
├── ingest.py     # テキスト→チャンク→文脈付与→埋め込み→保存
├── eval.py       # 検索評価（Hit@k / MRR）
├── compare.py    # contextual有無のA/B測定
├── retrieval.py  # ★ハイブリッド検索（ベクトル + 字面 + RRF融合）
└── main.py       # FastAPI: /health /ingest /chat

tests/            # 単体テスト（DB・外部APIを使わない純ロジック）
├── test_keywords.py    # 名詞抽出
├── test_parsers.py     # PDF/XLSX/PPTX 抽出
├── test_chunking.py    # 構造分割（条文境界・最小/最大サイズ）
├── test_contextual.py  # 文脈付与とプロンプトキャッシュの並び
├── test_compare.py     # contextual有無のA/B測定（比較の公平性）
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

## チャンク分割と文脈付与（`chunking.py` / `ingest.py`）

検索精度の上限はここで決まる。文字数で機械的に切ると「第5条」の途中で切れ、
条件と例外が別チャンクに割れてどちらを引いても答えられなくなる。

- **構造で切る**（`chunking.split_chunks`）… 見出し（`# `）・章節（`第N章`/`第N節`）・
  条番号（`第N条`）・箇条書き（`1.`）の行を境界にして節へ分ける。
  `CHUNK_MIN_CHARS` 未満の節は次とくっつけ（断片化防止）、`CHUNK_MAX_CHARS` を
  超えた節だけ文の切れ目で二次分割する（オーバーラップはここだけ）。
- **文脈を付ける**（contextual retrieval, `llm.generate_chunk_contexts`）…
  「これを超える場合は所属長の承認を要する」のような断片は、単体では主語も
  金額も分からず検索に当たらない。文書全体を Claude に読ませて
  「文書内での位置づけ」を1〜2文で書かせ、**埋め込む直前に前置する**。
  回答生成に渡す本文（`chunks.content`）は原文のまま残し、生成文は
  `chunks.context` に別で持つ（生成した文脈が回答の根拠に混ざらないように）。
  文書部分にプロンプトキャッシュを効かせているので、2チャンク目以降は入力が安い。

`USE_CONTEXTUAL_CHUNKING=false` にすると Claude を呼ばず、見出しの階層
（`第2章 休暇 > 第5条 年次有給休暇`）を前置する。

### 差分検知（再取り込みで埋め込みを省く）

同じ文書を入れ直すたびに埋め込みAPIを呼ぶのは無駄で、Voyage の無料枠（3 RPM）
にもすぐ当たる。取り込み時に `documents.content_hash` を突き合わせ、一致したら
分割も文脈生成も埋め込みもせずに戻る（レスポンスの `skipped: true`）。

ハッシュには本文だけでなく **埋め込み結果を左右する入力すべて** を混ぜる
（`ingest.content_hash`）: contextual の有無・`EMBED_MODEL`・チャンクのサイズ設定・
`CHUNKING_VERSION`。本文だけで判定すると、設定を変えて入れ直したのに古い
埋め込みが残ったり、`app.compare` の A/B が2回目でスキップされて成立しなくなる。

- `app.chunking` の**分割ロジックを変えたら `config.CHUNKING_VERSION` を上げる**
  （上げないと本文が同じ既存文書は作り直されない）
- 本文が同じで区分(project/topic)だけ変えた再登録は、埋め込みを使い回して
  `documents` の行だけ更新する
- この機能より前に入った文書は `content_hash` が NULL なので、一度だけ入れ直される

### 効果を測る（A/B測定）

有り/無しを手で切り替えて測ると条件がずれるので、専用のCLIを用意してある:

```bash
task compare-contextual          # = docker compose exec backend python -m app.compare
python -m app.compare --project 社内規程 --top-k 4
```

seed_docs を **contextual なし → あり** の2通りで取り込み直し、同じ質問集で
Hit@k・MRR と「順位が動いた質問」を並べて出す。公平に測るための決めごとは3つ:

1. **質問のベクトルは最初に1回だけ作り、両方の評価で使い回す**
   → 差が「文書側の作り方」だけに由来すると言い切れる（埋め込みAPIも1回で済む）
2. 検索手法・パラメータは両構成で同一（引数をそのまま両方へ渡す）
3. 取り込み直すのは `seed_docs/*.txt` のみ。APIで別途入れた文書は両方で同じ状態のまま残り、共通の妨害文書として働く

**DBを書き換える点に注意**（同名文書は置き換わる）。設定 `USE_CONTEXTUAL_CHUNKING` と
同じ構成を最後に回すので、実行後のDBは設定どおりの状態で残る。

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
  "project": "社内規程", "topic": "労務",
  "text": "年次有給休暇は入社6か月後に10日付与される。取得は前日までに申請すること。"
}'

# 質問する
curl -X POST localhost:8000/chat -H 'content-type: application/json' -d '{
  "question": "有給は入社何か月で何日もらえる?"
}'
# => {"answer":"入社6か月後に10日付与されます。","sources":["有給休暇.txt"]}
```

## この最小版で「やっていないこと」（＝次にやると学びが深い）

- **リランクのチューニング** … `retrieval.py` の `rerank_candidates` は実装済みで、
  方式を2つ持つ（`voyage`=Voyage rerank-2 / `llm`=Claudeに番号を並べ替えさせる
  プロンプト式）。既定は off なので、`python -m app.eval --rerank` で
  「なし / llm / voyage」の3条件を比較して効果を測るところから
- ハイブリッド検索の字面側は pg_trgm（トライグラム）で、厳密なBM25ではない。
  日本語BM25が欲しくなったら PGroonga 等の日本語対応エンジンに差し替える
- PDF/docx/xlsx 取り込み・図表のマルチモーダル文章化（今はプレーンテキストのみ）
- 回答のストリーミング（今は生成完了後に一括返却）
- 会話履歴の保持、評価（Ragas/promptfoo）
