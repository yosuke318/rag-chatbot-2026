# RDS PostgreSQL 16 + pgvector（ベクトル / 全文検索 / 会話履歴 / メタデータを1DBに集約）
# 設計方針:
#   - db.t4g.micro / private subnet / backup_retention_period = 7
#   - skip_final_snapshot = true（destroy可能に。文書はS3原本から再取り込み可能）
#   - パスワードはSecrets Manager（db_secret_arn）から取得。平文で書かない
#   - 初回に CREATE EXTENSION vector; を流す（マイグレーションで管理）
# TODO: aws_db_subnet_group / aws_db_instance
