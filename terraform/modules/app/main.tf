# ECS Fargate: Next.js(画面) と FastAPI(API) を1タスク2コンテナで動かす。
#
# 設計方針:
#   - public subnet + assign_public_ip = true、desired_count = 1（ALB無し）
#   - 到達制御は network モジュールの app_sg（送信元IPホワイトリスト）
#   - APIキー/DB認証はSecrets Manager経由で注入（環境変数に平文で置かない）
#   - 夜間停止: desired_count は ignore_changes にしてあるので、
#     `aws ecs update-service --desired-count 0` や EventBridge Scheduler で
#     0に落としても次のapplyで1に戻されない
#
# ECRリポジトリは terraform/bootstrap 側にある。ワークフローは
# 「イメージpush → terraform apply」の順に走るので、apply時には既に必要なため。

data "aws_region" "current" {}

locals {
  # 同一タスク内のコンテナはlocalhostで話せる（awsvpcモード＝ネットワーク名前空間を共有）。
  backend_origin = "http://localhost:${var.api_port}"

  # 実S3に向けるための設定。backend/app/storage.py は S3_ENDPOINT_URL と
  # S3_BUCKET の両方が揃ったときだけ原本保存を有効にするので、
  # AWS上でもエンドポイントを明示的に渡す必要がある。
  s3_endpoint = "https://s3.${data.aws_region.current.name}.amazonaws.com"

  backend_secrets = concat(
    [
      { name = "DATABASE_URL", valueFrom = var.db_url_secret_arn },
      { name = "ANTHROPIC_API_KEY", valueFrom = var.anthropic_secret_arn },
      { name = "VOYAGE_API_KEY", valueFrom = var.voyage_secret_arn },
    ],
    var.admin_token_secret_arn == null ? [] : [
      { name = "ADMIN_TOKEN", valueFrom = var.admin_token_secret_arn },
    ],
  )

  # タスク実行ロールが読めるべきシークレット（上で注入している分だけ）。
  secret_arns = [for s in local.backend_secrets : s.valueFrom]
}

resource "aws_ecs_cluster" "this" {
  name = var.project

  setting {
    name  = "containerInsights"
    value = "disabled" # devでは追加課金を避ける。必要になったら enhanced に上げる
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project}"
  retention_in_days = var.log_retention_days
}

# --- IAMロール2種 ----------------------------------------------------------
# 実行ロール: ECS本体がイメージ取得・ログ出力・シークレット取得に使う（起動時）。
# タスクロール: コンテナの中のアプリがAWSを呼ぶときに使う（実行中）。
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.project}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# 注入するシークレットだけを読めるようにする（Secrets Manager全体には広げない）。
data "aws_iam_policy_document" "execution_secrets" {
  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.secret_arns
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.project}-read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${var.project}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  # 原本文書バケットの読み書き（storage.py）。
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${var.documents_bucket_arn}/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.documents_bucket_arn]
  }

  # `aws ecs execute-command` でコンテナに入るための権限。
  # ALBもSSHもない構成なので、中を見る手段はこれしかない。
  dynamic "statement" {
    for_each = var.enable_execute_command ? [1] : []
    content {
      effect = "Allow"
      actions = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.project}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- タスク定義: backend(FastAPI) + frontend(Next.js) ----------------------
resource "aws_ecs_task_definition" "this" {
  family                   = var.project
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # GitHub Actions(ubuntu-latest)でビルドしたイメージはamd64。
  # 明示しないとアーキテクチャ不一致で起動に失敗することがある。
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = var.backend_image
      essential = true

      portMappings = [{ containerPort = var.api_port, protocol = "tcp" }]

      environment = concat(
        [
          { name = "S3_BUCKET", value = var.documents_bucket },
          { name = "S3_ENDPOINT_URL", value = local.s3_endpoint },
          # ECSはリージョンを環境変数で渡してくれないので明示する。
          # 無いと boto3 が署名に使うリージョンを決められず NoRegionError になる。
          { name = "AWS_REGION", value = data.aws_region.current.name },
          { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name },
          # ログをバッファせず即CloudWatchへ出す
          { name = "PYTHONUNBUFFERED", value = "1" },
        ],
        [for k, v in var.backend_env : { name = k, value = v }],
      )

      secrets = local.backend_secrets

      # イメージにcurlが入っていない(python:3.12-slim)ので標準ライブラリで叩く。
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:${var.api_port}/health')\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60 # 起動時に init_db() がスキーマを作るぶんの猶予
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "backend"
        }
      }
    },
    {
      name      = "frontend"
      image     = var.frontend_image
      essential = true

      portMappings = [{ containerPort = var.frontend_port, protocol = "tcp" }]

      # 同一タスクなのでlocalhost。composeの BACKEND_URL=http://backend:8000 に相当。
      environment = [
        { name = "BACKEND_URL", value = local.backend_origin },
      ]

      # backendが健康になってから起動する（起動直後の500を減らす）。
      dependsOn = [{ containerName = "backend", condition = "HEALTHY" }]

      healthCheck = {
        command     = ["CMD-SHELL", "wget -q --spider http://localhost:${var.frontend_port}/ || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "frontend"
        }
      }
    },
  ])
}

resource "aws_ecs_service" "this" {
  name            = var.project
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [var.app_sg_id]
    assign_public_ip = true # NAT無しでECRとLLM APIに出るために必須
  }

  # タスク1つを置き換えるだけなので、一時的に0になるのを許して
  # 「新旧2タスクが同時に立ち上がってDBを取り合う」のを避ける。
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  # 夜間・週末停止で 0 に落としたものを、次のapplyで1に戻さない。
  lifecycle {
    ignore_changes = [desired_count]
  }
}
