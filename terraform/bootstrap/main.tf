# 一度きりの下ごしらえ（bootstrap）。
#
# なぜ環境スタック（environments/dev）と分けるか:
#   dev のstateを置くS3バケットを dev 自身のstateで管理すると鶏卵問題になる。
#   同じく「Terraformを実行するためのIAMロール」もTerraformで作れない（実行できないので）。
#   そこで "state置き場・実行ロール・イメージ置き場" だけをこのスタックに切り出し、
#   ローカルstateで一度だけ手元から apply する。以後の変更は environments 側で回る。
#
# 実行手順は同ディレクトリの README.md を参照。

data "aws_caller_identity" "current" {}

# --- Terraform state: S3 + DynamoDBロック --------------------------------
# バケット名はグローバル一意なのでアカウントIDを混ぜて衝突を避ける。
resource "aws_s3_bucket" "tfstate" {
  bucket = "${var.project}-tfstate-${data.aws_caller_identity.current.account_id}"

  # stateは消えると復旧が面倒なので、うっかり destroy されないようにしておく。
  # 本当に消すときだけ、この行を消してから destroy する。
  lifecycle {
    prevent_destroy = true
  }
}

# 壊れたstateから戻せるようにバージョニングは必須。
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tflock" {
  name         = "${var.project}-tflock"
  billing_mode = "PAY_PER_REQUEST" # ロック取得しか使わないので従量で十分（実質無料）
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

# --- GitHub Actions から引き受けるIAMロール（OIDC・アクセスキー無し） --------
# 既にアカウントにproviderがある場合は create_oidc_provider = false で参照だけする。
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"] # AWS側で検証済みの値に自動更新される
}

data "aws_iam_openid_connect_provider" "existing" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.existing[0].arn

  # 「どのリポジトリの・どの実行元なら引き受けてよいか」の許可リスト。
  # ワイルドカード（repo:owner/repo:*）にすると、そのリポジトリの
  # あらゆるブランチ・PRからデプロイできてしまう。ここは必ず列挙で絞る。
  allowed_subs = [for ref in var.github_allowed_refs : "repo:${var.github_repository}:${ref}"]
}

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.allowed_subs
    }
  }
}

resource "aws_iam_role" "deploy" {
  name = "${var.project}-github-deploy"
  # IAMのdescriptionはASCII/Latin-1しか受け付けない（日本語だとCreateRoleが400で落ちる）。
  # 説明はこのコメント側に書く: deploy-dev.yml がOIDCで引き受けるデプロイ用ロール。
  description        = "Deploy role assumed by GitHub Actions (deploy-dev.yml) via OIDC"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
}

data "aws_iam_policy_document" "deploy" {
  # ECRログイン。GetAuthorizationToken はリソース指定不可（アカウント単位）。
  statement {
    sid       = "EcrLogin"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # イメージのpush/pullは、このプロジェクトのリポジトリだけ。
  statement {
    sid    = "EcrPush"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for r in aws_ecr_repository.this : r.arn]
  }

  # Terraform state の読み書きとロック。
  statement {
    sid       = "TerraformState"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [aws_s3_bucket.tfstate.arn, "${aws_s3_bucket.tfstate.arn}/*"]
  }

  statement {
    sid       = "TerraformLock"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.tflock.arn]
  }

  # 環境スタックが触るサービス群。リージョンを東京に固定して、
  # 万一ロールが漏れても他リージョンには作れないようにする。
  statement {
    sid    = "InfraManagement"
    effect = "Allow"
    actions = [
      "ec2:*",            # VPC / subnet / route table / security group
      "ecs:*",            # cluster / task definition / service
      "rds:*",            # DBインスタンス / subnet group
      "logs:*",           # CloudWatch Logs
      "secretsmanager:*", # APIキー・DB認証情報
      "s3:*",             # 原本文書バケット
      "ecr:*",            # リポジトリ定義（ライフサイクル等）
      "application-autoscaling:*",
      "events:*", # EventBridge（夜間停止スケジュール等）
      "scheduler:*",
      "kms:Describe*",
      "kms:List*",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }

  # ECSタスクロール等の作成。プロジェクト名で始まるロールだけに限定する
  # （このロールを使って無関係な管理者ロールを作られないようにするため）。
  statement {
    sid    = "IamForServiceRoles"
    effect = "Allow"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:PassRole",
      "iam:PutRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
    ]
    resources = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.project}-*"]
  }

  # ECS/RDSが自分用に作るサービスリンクロール（初回のみ必要）。
  statement {
    sid       = "ServiceLinkedRoles"
    effect    = "Allow"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["ecs.amazonaws.com", "rds.amazonaws.com", "secretsmanager.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.project}-github-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# --- ECR: ワークフローがイメージをpushする先 ------------------------------
# environments 側ではなくここに置く理由: deploy-dev.yml は
# 「イメージをpush → terraform apply」の順で走るため、apply より先に存在している必要がある。
resource "aws_ecr_repository" "this" {
  for_each = toset(var.ecr_repositories)

  name         = each.value
  force_delete = true # 設計方針: destroy一発で消せること

  image_scanning_configuration {
    scan_on_push = true
  }
}

# 古いイメージが溜まるとストレージ課金が効いてくるので直近10世代だけ残す。
resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep only the last 10 images" # 直近10世代だけ残す
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
