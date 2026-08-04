terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # ここだけローカルstate。stateの置き場そのものを作るスタックなので、
  # S3バックエンドにすると「置き場を作るのに置き場が要る」鶏卵問題になる。
  # 作るのは一度きり・変更もほぼ無いため、terraform.tfstate をローカルに置き
  # （.gitignore済み）、必要なら後から `terraform init -migrate-state` でS3へ移す。
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}
