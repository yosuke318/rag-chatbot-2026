# 社内文書RAGチャットボット v2

社内文書（PDF / docx / xlsx）に自然言語で質問すると、根拠資料付きで回答するRAGチャットボット。
3年前に構築した旧版（LangChain 0.0.x + Pinecone + Slack Bot + AWS Lambda）を、2026年時点のアーキテクチャで再設計・再実装したもの。

📄 **設計の詳細**: [docs/design.md](docs/design.md)

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
