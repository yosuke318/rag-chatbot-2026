terraform {
  required_version = ">= 1.7"

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

provider "aws" {
  region = var.region # 東京リージョン固定：文書データをリージョン外に出さない

  default_tags {
    tags = {
      Project   = var.project
      Env       = "dev"
      ManagedBy = "terraform"
    }
  }
}
