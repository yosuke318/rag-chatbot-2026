# RDS PostgreSQL 16 + pgvector（ベクトル / 全文検索 / 会話履歴 / メタデータを1DBに集約）
#
# 設計方針:
#   - db.t4g.micro / private subnet / backup_retention_period = 7
#   - skip_final_snapshot = true（destroy可能に。文書はS3原本から再取り込みできる）
#   - パスワードは random_password で生成し、Secrets Manager 経由でタスクへ渡す。
#     tfvarsにもコードにも書かない。
#   - CREATE EXTENSION vector / pg_trgm はアプリ起動時の init_db()（backend/app/db.py）が
#     流す。Terraform側では何もしない（1か所で完結させるため）。

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.project}-db"
  subnet_ids = var.private_subnet_ids

  tags = { Name = "${var.project}-db" }
}

# 接続文字列にそのまま埋めるので記号は使わない（URLエスケープ不要にする）。
# 英数40文字あればエントロピーは十分。
resource "random_password" "db" {
  length  = 40
  special = false
}

resource "aws_db_instance" "this" {
  identifier     = "${var.project}-db"
  engine         = "postgres"
  engine_version = "16" # マイナーはAWSに任せる（auto_minor_version_upgrade）
  instance_class = var.instance_class

  allocated_storage     = 20 # gp3の下限
  max_allocated_storage = 50 # 使った分だけ自動で伸ばす（上限を切って青天井を防ぐ）
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.db_sg_id]
  publicly_accessible    = false # private subnet + SG。インターネットからは不到達

  backup_retention_period = var.backup_retention_days
  # destroy を一発で通すための設定（設計書5節）。dev前提。
  skip_final_snapshot = true
  deletion_protection = false
  apply_immediately   = true

  auto_minor_version_upgrade = true

  tags = { Name = "${var.project}-db" }
}

# アプリは DATABASE_URL 一本しか読まない（backend/app/config.py）ので、
# ホスト・ユーザ・パスワードを個別に渡すのではなく、組み立て済みのURLを
# 1つのシークレットに入れてコンテナへ注入する。
#
# 注意: random_password の値はtfstateに平文で残る（Terraformの仕様）。
# state自体はS3の暗号化バケットにあり、bootstrapのIAMポリシーで
# デプロイロールしか読めないようにしてある。
resource "aws_secretsmanager_secret" "db_url" {
  name        = "${var.project}/database-url"
  description = "アプリに渡す DATABASE_URL（Terraformが生成・更新する）"

  # 0にすると destroy 直後に同名で作り直せる。既定(30日)のままだと
  # destroy → apply のやり直しが「まだ削除待ちです」で失敗する。
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "db_url" {
  secret_id     = aws_secretsmanager_secret.db_url.id
  secret_string = "postgresql://${var.db_username}:${random_password.db.result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${var.db_name}"
}
