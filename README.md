# RAG Inspector（RAG検証ラボ）

**埋め込み・検索・回答生成の挙動を観察するためのRAG検証ツール。**

文書を入れて質問すると根拠付きで回答する、という点では普通のRAGチャットボットだが、
主眼は「答えを出すこと」ではなく **パイプラインの各段で何が起きているかを可視化すること** にある。

- 質問と文書がどんなベクトルになり、どれくらい近いのか（cos類似度）
- 字面の一致はどう効くのか（名詞抽出 + トライグラム類似度）
- 単語の希少度で重み付けするとどうなるのか（BM25）
- 3つの検索結果がRRFでどう融合され、順位がどう決まるのか
- LLMリランクを挟むと順位がどう変わるのか

これらを **数値と順位で並べて確認できる** ようにしてある（`/search` と UI の「検索の内訳」パネル）。
3年前に構築した社内文書RAG（LangChain 0.0.x + Pinecone + Slack Bot + AWS Lambda）を、
2026年時点のアーキテクチャで再設計・再実装したものが土台になっている。

📄 **設計の詳細**: [docs/design.md](docs/design.md)

## 画面

![全体像](docs/images/overview.png)

3つのパネルが、そのままRAGの3工程に対応している。

| パネル | 対応するAPI | 必要なAPIキー |
|---|---|---|
| ① 文書を登録 | `POST /ingest` | Voyage（埋め込み） |
| ② 検索の内訳を見る | `GET /search` | Voyage のみ |
| ③ 質問する | `POST /chat` | Voyage + Anthropic |

**検索だけならAnthropicキーは要らない**。回答生成を挟まずに検索の挙動だけを追えるので、
チューニングの試行錯誤はこのパネルで完結する。

### 検索の内訳 ― このツールの主役

![検索の内訳](docs/images/search-breakdown.png)

複数トピックにまたがる質問（「リモートワークで休暇ってどんな扱い？休暇の間は経費は？」）を投げた例。
**3つの検索手法が、それぞれ違う順位を付けている**のが読み取れる。

| 文書 | ベクトル（意味） | 字面（トライグラム） | BM25 |
|---|---|---|---|
| リモートワーク規程 | **0位** (0.5066) | **0位** (0.1286) | **0位** (2.6984) |
| 経費精算 | 2位 (0.4544) | **1位** (0.0263) | **1位** (1.8418) |
| 有給休暇 | **1位** (0.4787) | 2位 (0.0259) | 2位 (1.672) |
| test.txt | 3位 (0.2959) | — | — |

注目すべきは **2位と3位で手法の判断が割れている** こと。
ベクトル検索は「有給休暇」を2位に置いたが、字面検索とBM25は「経費精算」を2位に置いた。

RRFはこれを **多数決のように統合する**。結果、2票を得た「経費精算」が最終2位になった。

```
リモートワーク規程  0.04918  ← 3手法すべてが1位（3票）
経費精算            0.04813  ← 字面とBM25が2位（2票が上位）
有給休暇            0.04788  ← ベクトルだけが2位
test.txt            0.01562  ← ベクトルにしか出ない（1票のみ）
```

読み方のポイント:

- **順位の下の `+0.01639`** … その手法がRRFスコアに足した分。合計がRRFスコアになる
- **赤い `—`** … その手法のリストに出てこなかった（票を投じていない）。
  `test.txt` は意味的にしか引っかからないので1票しか持てず、最下位に沈む
- **上部の入力欄** … RRFの `k`、字面の閾値、BM25の `k1`/`b` をその場で変更して再検索できる。
  数式の定数を変えると順位がどう動くかを体感できる

**1位が質問の内容と一致していれば検索は成功**で、この上位チャンクがそのまま ③ の回答生成で根拠として使われる。

> スクリーンショットは `cd frontend && npm run screenshot` で再生成できる（要: backend/frontend 起動）。

## アーキテクチャ

**モジュラーモノリス + マネージドサービス**（東京リージョン / ログインなし・Tailscaleで閉域 / Terraform 100%）

```
社内ユーザー ─ Tailscale ─► ECS Fargate (Next.js + FastAPI) ─► RDS PostgreSQL + pgvector
                                       │
                            S3(原本) ─ 取り込みバッチ ─► RDS
                                       │
                                       └─► LLM API（生成・埋め込み・PDF読解・リランク）
```

- **DBは1つに集約**: ベクトル / 全文検索(BM25) / 会話履歴 / メタデータを全部Postgresへ
- **検索は自作**: ハイブリッド検索（ベクトル + BM25 + RRF）→ LLMリランク → 回答生成
- **ALB / NAT Gatewayなし**: 10人規模向けにコスト最適化（月 ~$40）
- **destroy可能・停止可能**: `terraform destroy` 一発撤去、夜間停止で更に半減

旧版との差分は [docs/design.md](docs/design.md) 第9章を参照。

## ディレクトリ構成

```
.
├── docs/            # 設計書
├── terraform/       # IaC（environments と modules を分離）
├── backend/         # FastAPI（ingest / retrieval / chat / eval のモジュール分割）
└── frontend/        # Next.js + Vercel AI SDK
```

## セットアップ（TODO: 実装しながら埋める）

```bash
# 1. インフラ
cd terraform/environments/prod
terraform init
terraform plan
terraform apply

# 2. バックエンド
cd backend
# TODO

# 3. フロント
cd frontend
# TODO
```

## 開発ロードマップ

- [ ] Terraform: network → database → app → ingest → secrets（plan が通る状態まで）
- [ ] backend/db: pgvector スキーマ（documents / chunks / conversations）
- [ ] backend/ingest: S3取り込み → PDF構造化 → チャンク分割(contextual) → 埋め込み → UPSERT
- [ ] backend/retrieval: ハイブリッド検索（ベクトル + BM25 + RRF）→ LLMリランク
- [ ] backend/chat: ストリーミング回答 + 根拠S3署名URL添付
- [ ] backend/eval: Ragas / promptfoo による評価
- [ ] frontend: Next.js + Vercel AI SDK チャットUI
- [ ] ポートフォリオ: README仕上げ + 操作GIF
