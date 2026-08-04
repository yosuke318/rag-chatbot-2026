# 取り込みパイプラインのストレージ: 原本文書を置くS3バケット。
#
# 設計書では「S3 put → EventBridge → 取り込み用ECSタスク」まで書いてあるが、
# 現時点のアプリに "S3のオブジェクトを受け取って走る取り込みバッチ" の入口が無い
# （取り込みは backend の POST /ingest・/ingest-file、つまりHTTP経由。
#  backend/app/ingest.py にCLIエントリポイントは無い）。
# 存在しないコマンドを叩くEventBridgeルールを先に置いても、
# 失敗するだけで何の役にも立たないのでここでは作らない。
# バケットとタスクロールの権限だけを用意し、
# アプリは storage.py 経由で原本の保存・署名付きURL発行に使う。
# バッチ化する場合は backend 側に入口を足してから、このモジュールに
# aws_cloudwatch_event_rule / aws_ecs_task_definition(ingest) を足す。

resource "aws_s3_bucket" "documents" {
  # バケット名はグローバル一意。アカウントIDを混ぜて衝突を避ける。
  bucket = "${var.project}-documents-${var.account_id}"

  # 設計方針: destroy一発で消せること。中身が残っていても消す。
  # 原本はローカルから再アップロードできる前提（dev環境）。
  force_destroy = true

  tags = { Name = "${var.project}-documents" }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
