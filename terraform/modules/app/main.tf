# ECS Fargate（Next.js + FastAPIを1タスクに同居）+ ECR
# 設計方針:
#   - public subnet, assign_public_ip = true, desired_count = 1
#   - APIキー/DB認証はSecrets Manager経由でコンテナに注入（secrets = [...]）
#   - 夜間停止: EventBridge Schedulerでdesired_countを0にできる構成に
# TODO: aws_ecr_repository / aws_ecs_cluster / aws_ecs_task_definition / aws_ecs_service
#       / aws_iam_role(task_execution, task) / aws_cloudwatch_log_group
