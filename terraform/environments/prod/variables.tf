variable "project" {
  type        = string
  default     = "rag-v2"
  description = "リソース名のprefix"
}

variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "allowed_cidrs" {
  type        = map(string)
  description = "アクセスできる送信元IPの許可リスト（名札 => CIDR）"
}

variable "allow_direct_api" {
  type    = bool
  default = false # prodは画面だけ開ける
}

variable "backend_image" {
  type    = string
  default = ""
}

variable "frontend_image" {
  type    = string
  default = ""
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "task_cpu" {
  type    = string
  default = "512"
}

variable "task_memory" {
  type    = string
  default = "1024"
}

variable "secret_recovery_window_days" {
  type        = number
  default     = 7
  description = "prodは消し間違いから戻せるように猶予を持たせる（devは0＝即時削除）"
}

variable "backend_env" {
  type    = map(string)
  default = {}
}
