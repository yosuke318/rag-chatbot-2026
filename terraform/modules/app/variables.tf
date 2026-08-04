variable "project" { type = string }

variable "public_subnet_ids" { type = list(string) }

variable "app_sg_id" { type = string }

# --- コンテナイメージ（ワークフローが -var で渡す） -------------------------
variable "backend_image" {
  type        = string
  description = "ECRのイメージURI（タグ込み）。例: 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/rag-v2-backend:<sha>"
}

variable "frontend_image" {
  type = string
}

# --- Secrets Manager のARN --------------------------------------------------
variable "db_url_secret_arn" { type = string }

variable "anthropic_secret_arn" { type = string }

variable "voyage_secret_arn" { type = string }

variable "admin_token_secret_arn" {
  type        = string
  default     = null
  description = "null なら ADMIN_TOKEN を注入しない（＝/admin/* が素通しになる）"
}

# --- 原本文書バケット -------------------------------------------------------
variable "documents_bucket" { type = string }

variable "documents_bucket_arn" { type = string }

# --- サイズ・運用 -----------------------------------------------------------
variable "task_cpu" {
  type        = string
  default     = "512" # 0.5 vCPU
  description = "FargateのCPUユニット。memoryと組み合わせが決まっている点に注意"
}

variable "task_memory" {
  type        = string
  default     = "1024" # 1GB
  description = "MiB。cpu=512 なら 1024/2048/3072/4096 のいずれか"
}

variable "desired_count" {
  type        = number
  default     = 1
  description = "初回applyの値。以後は ignore_changes なのでCLIから変えてよい"
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "enable_execute_command" {
  type        = bool
  default     = true
  description = "aws ecs execute-command でコンテナに入れるようにするか（devの調査用）"
}

variable "backend_env" {
  type        = map(string)
  default     = {}
  description = <<-EOT
    backendコンテナに追加で渡す環境変数。モデル名やチャンク設定など
    backend/app/config.py が読むもの。秘密情報はここではなくSecrets Managerへ。
  EOT
}

variable "frontend_port" {
  type    = number
  default = 3000
}

variable "api_port" {
  type    = number
  default = 8000
}
