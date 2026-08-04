variable "project" { type = string }

variable "create_admin_token" {
  type        = bool
  default     = true
  description = "管理API(/admin/*)用トークンを生成してSecrets Managerに置くか"
}

variable "secret_recovery_window_days" {
  type        = number
  default     = 0
  description = "0 = 即時削除。destroy → 再apply をすぐやり直せるようにするため dev では 0"
}
