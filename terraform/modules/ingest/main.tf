# 取り込みパイプラインのインフラ: S3(原本文書) + EventBridge + 取り込み用ECSタスク定義
# フロー: S3 put → EventBridge → ECS RunTask → PDF構造化→チャンク→埋め込み→pgvector UPSERT
# 設計方針: S3は force_destroy = true（destroy可能に）
# TODO: aws_s3_bucket / aws_cloudwatch_event_rule / aws_cloudwatch_event_target(ECS)
#       / aws_ecs_task_definition(ingest) / aws_iam_role
