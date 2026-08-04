variable "project" { type = string }

variable "private_subnet_ids" { type = list(string) }

variable "db_sg_id" { type = string }

variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_name" {
  type    = string
  default = "rag"
}

variable "db_username" {
  type    = string
  default = "rag"
}

variable "backup_retention_days" {
  type        = number
  default     = 7
  description = "自動バックアップの保持日数。0にすると自動バックアップ無効"
}

variable "secret_recovery_window_days" {
  type        = number
  default     = 0
  description = <<-EOT
    Secrets Manager の削除猶予日数。0 = 即時削除。
    destroy → 再apply をすぐやり直せるようにするため dev では 0。
    prod では 7〜30 に上げて、消し間違いから戻せるようにする。
  EOT
}
