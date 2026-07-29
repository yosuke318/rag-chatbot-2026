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

## コンセプトと役割

出発点は「検索手法を観察し、パラメータをいじって挙動を確かめる学習ツール」だった。
そこから、**回答精度を主眼にした実用RAG**へ育てるにあたり、このシステムは2つの役割を担う。

### 役割1: AI開発者のための RAG 評価・検証アプリ

**登録 → 検索 → 回答生成 → 評価** を1つの画面で一周できる。RAGは「なぜこの回答になったのか」が
ブラックボックスになりがちだが、ここでは各工程を数値で開いて見せる。

- 検索の内訳（cos類似度 / 字面 / BM25 / RRF融合）を順位とスコアで確認（`/search`）
- 手法・数値パラメータ・LLMリランクを切り替え、**質問集全体で Hit@k / MRR がどう動くか**を測る（`/eval`）
- 回答に 👍/👎 を付けて評価データへ還流（`/feedback`）

「なぜこの回答か」を説明でき、改善サイクル（変更→測定→比較）を回せることが、
汎用チャットボットに対する差別化点になる。

### 役割2: 文書に基づく回答の API 提供基盤

同じ検索・回答生成を **API として外部から呼べる**。社内規程・手順書などの文書を入れておき、
問い合わせ対応やドキュメントQAに組み込む使い方を想定する。文書は**プロジェクト・トピックごとに分離**でき、
混ざらない形にする（文書・評価用の質問集とも `project` / `topic` の2軸を持つ）。

### 商用化の狙い

- **社内問い合わせの削減**: 総務・経理・情シスへの定型質問を一次回答で吸収する
- **説明可能性・監査性**: 「どの文書のどこを根拠にしたか」を示せる（規程・コンプライアンス領域で有効）
- **マルチモーダルへの発展**: PDF内の図表・チャートの読解支援（判断は人に残す）

> **実装状況**: 役割1（評価・検証アプリ）は実装済み。役割2のうち、検索・回答・原本ダウンロードは
> API として動作するが、**公開API（APIキー認証・レート制限・バージョニング）と検索のプロジェクト・トピック分離は
> 未実装（予定）**。下記「提供API」に現状と予定を分けて示す。

## 提供API

FastAPI なので OpenAPI スキーマ（`/openapi.json`）が自動生成され、そのまま外部提供の土台になる。

### 現状（実装済み）

| API | 役割 | キー |
|---|---|---|
| `POST /ingest` | 文書登録（テキスト→チャンク→埋め込み→保存、原本はS3へ） | Voyage |
| `POST /ingest-file` | ファイル登録（PDF/xlsx/pptx。本文テキストに加え**文書内画像**も抽出・索引化してS3へ） | Voyage（案Aは + Anthropic） |
| `GET /search` | 検索の内訳（ベクトル/字面/BM25/RRF） | Voyage |
| `POST /chat` | 回答生成（チャンク単位の根拠＋原本URL付き・会話履歴対応） | Voyage + Anthropic |
| `POST /chat/stream` | 同上をSSEで逐次返す（根拠は本文より先に届く） | Voyage + Anthropic |
| `GET /eval` | 質問集で Hit@k / MRR を測定 | Voyage（リランク時 Anthropic） |
| `GET,POST /eval-questions` | 評価用質問の一覧・登録（プロジェクト・トピックで分離可） | 不要 |
| `POST /feedback` | 回答への 👍/👎 記録 | 不要 |
| `GET /files/{source}` | 登録した原本のダウンロード（S3/MinIO） | 不要 |

### 予定（未実装）

| API / 機能 | 内容 |
|---|---|
| 公開API（`/v1/...`） | APIキー認証・レート制限・利用ログ・バージョニング |
| 検索のプロジェクト・トピック分離 | `documents.project` / `topic` は登録済み。検索・回答をこの軸で絞る対応が未実装 |
| マルチモーダル | 画像の抽出・S3保管・**検索対象化**（自動キャプション / マルチモーダル埋め込みを切り替えて比較可）まで実装済み。**原本画像を根拠にした回答生成**とチャート読解支援が未実装 |

（ロードマップの詳細は本ファイル末尾の「開発ロードマップ」と Linear を参照）

## 画面

![全体像](docs/images/overview.png)

パネルが、そのまま「登録 → 検索 → 回答 → 評価」の各工程に対応している。

| パネル | 対応するAPI | 必要なAPIキー |
|---|---|---|
| ① 文書を登録 | `POST /ingest` | Voyage（埋め込み） |
| ② 検索の内訳を見る | `GET /search` | Voyage のみ |
| ③ 質問する | `POST /chat` | Voyage + Anthropic |
| ④ 評価する | `GET /eval` | Voyage（リランク時 Anthropic） |

**検索だけならAnthropicキーは要らない**。回答生成を挟まずに検索の挙動だけを追えるので、
チューニングの試行錯誤は ② と ④ で完結する。

> ※ 上のスクリーンショットは ①〜③ の頃のもの。④ 評価パネルと出典のダウンロードリンクは
> 追加後に再取得予定（`cd frontend && npm run screenshot`）。

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

### 文書内の図表を引く

PDF/xlsx/pptx を `/ingest-file` で登録すると、本文テキストとは別に**文書内の画像**も
取り出して索引に載せる（PDFはページ全体を画像化するので、ベクタ描画のチャートも残る）。
「どうやってテキストの質問で絵を引くか」には2つの流儀があり、**切り替えて比較できる**
ようにしてある（`IMAGE_INDEX_METHOD`）。

| 方式 | やること | 引くときの手法 |
|---|---|---|
| `caption`（既定） | Claude に画像の説明文を書かせ、その文を普通のチャンクとして埋め込む | `vector` / `trgm` / `bm25`（既存のまま） |
| `multimodal` | `voyage-multimodal-3` で画像を直接ベクトル化 | `image`（専用の4本目） |
| `none` | 索引を作らず保管だけ | （引けない） |

方式を変えたら、原本画像はS3にあるので**ファイルを上げ直さずに索引だけ作り直せる**:

```bash
curl -X POST "http://localhost:8000/admin/reindex-images?method=multimodal"
```

> どちらが良いかは eval で決める前提。実測の手順は下記「検索精度を測る」を参照。
> なお `caption` は「説明文に書かれなかったことは後から問えない」という弱点を持つ。
> それを解消する（回答生成には原本画像そのものを渡す）のは次段の課題。

## 検索精度を測る（eval）

「チューニングで良くなった気がする」を数字に変えるための評価ハーネス。
検索が**正解の文書を上位に拾えているか**を、固定の質問集に対して測る。

```bash
cd backend
python -m app.eval --seed                     # サンプル質問をDBへ初期投入（初回だけ）
python -m app.eval                            # DBの質問で評価
python -m app.eval --project 社内規程 --topic 労務   # プロジェクト・トピックで絞って評価
python -m app.eval --retrievers vector,bm25   # 手法を変えて比較
python -m app.eval --rerank                   # リランク有りで比較（既定は Voyage rerank-2）
python -m app.eval --rerank --rerank-method llm  # プロンプト式リランクと比較（要 Anthropic）
python -m app.eval --gen                      # 回答生成まで走らせて目視（要 Anthropic）
```

出力する指標は2つ:

| 指標 | 意味 |
|---|---|
| **Hit@k** | 上位k件に正解文書が入った質問の割合（拾えたか） |
| **MRR** | 正解が何位に来たかの逆数の平均。1位=1.0 / 2位=0.5 / 圏外=0（どれだけ上位に置けたか） |

ポイント:

- **検索評価だけなら Anthropic キーは不要**（質問のベクトル化に Voyage は要る）。
  `--gen`、および `--rerank --rerank-method llm` のときだけ Claude を呼ぶ
  （既定の Voyage リランクは Anthropic キー不要）。
- `--retrievers` や `--rerank` を切り替えると数字が動くので、
  「BM25を足すと上がるか」「リランクは効くか」「どの方式のリランクが効くか」を
  **同じ質問集で公平に比較**できる。
- リランクは質問1件につきAPI 1リクエスト。Voyage 無料枠（3 RPM）では4問目で
  429 になるので、評価を回すなら支払い方法を登録して上限を緩和しておく。

### 図表の索引方式を A/B で比べる

```bash
# 案A: 自動キャプション
curl -X POST "http://localhost:8000/admin/reindex-images?method=caption"
python -m app.eval --retrievers vector,trgm,bm25

# 案B: マルチモーダル埋め込み
curl -X POST "http://localhost:8000/admin/reindex-images?method=multimodal"
python -m app.eval --retrievers vector,trgm,bm25,image
```

**比較が成立するのは「画像にしか答えが無い質問」を質問集に入れたときだけ**。
本文テキストでも答えられる質問ばかりだと、どちらの方式でも同じ数字が出る
＝ どちらの効果も測れていない。`eval_questions` に図表根拠の設問を足すこと。
レポートの `image_index_method` に、そのとき何で索引したかが残る。
- 改良（チャンク分割の変更・リランク導入など）の**前後で回して差分を見る**のが本来の使い方。

質問と正解ラベルは **DB の `eval_questions` テーブル**に置く。プロジェクト・トピック
（`project` / `topic`）ごとに分けられるので、文書を同じ2軸で分ける方針（→ アーキテクチャ）と
評価の粒度が揃う。「そのプロジェクトの文書 × その質問」で評価できる。

- 初期データは `backend/seed_data/eval_questions.json`（fixture）にあり、
  `task seed` が `seed_docs/*.txt` の投入とセットでDBへ流し込む（冪等）
- 質問の追加はコード編集ではなく **`POST /eval-questions`** で行える（非エンジニアでも足せる）
- 一覧は `GET /eval-questions?project=...&topic=...`

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

## 開発コマンド（lint / テスト）

[go-task](https://taskfile.dev) 経由で、FE・BE をまとめて実行する。`task --list` で全一覧。

```bash
task lint    # FE + BE の lint を確認（CIと同じ内容）
task fmt     # lint の自動修正（import順の並べ替え・未使用importの削除など）
task test    # バックエンドの単体テスト（DB・外部API不要）
task test-front  # フロントの単体テスト + 型チェック
```

- 片側だけ回したいときは `task lint-back` / `task lint-front`（`fmt` も同様）
- BE は **ruff**（設定 `backend/ruff.toml`）、FE は **ESLint**（設定 `frontend/.eslintrc.json`）
- BE 側は使い捨てコンテナで実行するので、ホストに Python 環境は要らない
- 同じコマンドを GitHub Actions（`.github/workflows/test.yml`）でも PR ごとに回すため、
  手元で `task lint` が通っていれば CI で lint だけ落ちることはない
- `task fmt` が直せるのは意味の変わらない範囲だけ。行が長すぎる等は手で直す

## 開発ロードマップ

- [ ] Terraform: network → database → app → ingest → secrets（plan が通る状態まで）
- [x] backend/db: pgvector スキーマ（documents / chunks / conversations / messages）
- [ ] backend/ingest: S3取り込み → PDF構造化 → チャンク分割(contextual) → 埋め込み → UPSERT
- [ ] backend/retrieval: ハイブリッド検索（ベクトル + BM25 + RRF）→ LLMリランク
- [x] backend/chat: ストリーミング回答（SSE）＋会話履歴＋根拠のチャンク明示・原本URL添付
- [x] backend/eval: Hit@k / MRR による検索評価（`python -m app.eval`）※ Ragas等での回答忠実性評価は次段
- [ ] frontend: Next.js + Vercel AI SDK チャットUI
- [ ] ポートフォリオ: README仕上げ + 操作GIF
