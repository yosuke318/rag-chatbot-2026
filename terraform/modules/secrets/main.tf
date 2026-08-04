# Secrets Manager: LLM APIキーと管理トークン
#
# 設計方針: LLMキーの「値」はTerraform管理外。tfstateに本物のキーを残さない。
#   ここでは枠だけ作り、初回だけプレースホルダを入れておく
#   （空のシークレットはECSタスクが参照できず起動に失敗するため）。
#   実キーの投入は README の手順どおり CLI で行い、その後の変更は
#   ignore_changes でTerraformが上書きしないようにしてある。
#
# DB認証情報(DATABASE_URL)はここではなく database モジュールが持つ。
# ホスト名が決まらないとURLを組めず、こちらに置くと循環参照になるため。

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

locals {
  # 論理名 => 説明。ECSタスクにはこの単位でARNを渡す。
  llm_keys = {
    "anthropic-api-key" = "回答生成・PDF読解に使う Claude のAPIキー"
    "voyage-api-key"    = "埋め込み・リランクに使う Voyage AI のAPIキー"
  }

  placeholder = "REPLACE_ME"
}

resource "aws_secretsmanager_secret" "llm" {
  for_each = local.llm_keys

  name                    = "${var.project}/${each.key}"
  description             = each.value
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "llm" {
  for_each = aws_secretsmanager_secret.llm

  secret_id     = each.value.id
  secret_string = local.placeholder

  # 実キーはCLIで入れる。Terraformが毎回プレースホルダに戻さないよう無視する。
  lifecycle {
    ignore_changes = [secret_string]
  }
}

# 管理用API(/admin/*)のトークン。backend は ADMIN_TOKEN が設定されている
# ときだけ認証を要求する（未設定だと誰でも再インデックスを叩けてしまい、
# 画像1枚ごとにLLMを呼ぶので費用にも効く）。
# 人が覚える必要はないので、ここで生成してSecrets Managerに置くだけにする。
resource "random_password" "admin_token" {
  count = var.create_admin_token ? 1 : 0

  length  = 48
  special = false # HTTPヘッダにそのまま載せるので英数のみ
}

resource "aws_secretsmanager_secret" "admin_token" {
  count = var.create_admin_token ? 1 : 0

  name                    = "${var.project}/admin-token"
  description             = "X-Admin-Token ヘッダに載せる管理API用トークン"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "admin_token" {
  count = var.create_admin_token ? 1 : 0

  secret_id     = aws_secretsmanager_secret.admin_token[0].id
  secret_string = random_password.admin_token[0].result
}
