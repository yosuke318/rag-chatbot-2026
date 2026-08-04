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

# --- アクセス制御（ホワイトリスト） ----------------------------------------
variable "allowed_cidrs" {
  type        = map(string)
  description = <<-EOT
    devにアクセスできる送信元IPの許可リスト。「名札 => CIDR」。
    terraform.tfvars（gitignore済み）か、CIでは TF_VAR_allowed_cidrs で渡す。
    ここに載っていないIPからはTCP接続そのものが張れない。
  EOT
}

variable "allow_direct_api" {
  type        = bool
  default     = true
  description = "FastAPI(8000)にも直接つなげるか。falseなら画面(3000)だけ"
}

# --- コンテナイメージ（ワークフローが渡す） --------------------------------
# deploy-dev.yml は commit sha のタグを -var で渡す（どのコミットが動いているか
# タスク定義から辿れるようにするため）。
# 手元から apply するときは空のままでよく、その場合は ECR の :latest を使う
# （ワークフローが sha と latest の両方を打っている）。
variable "backend_image" {
  type    = string
  default = ""
}

variable "frontend_image" {
  type    = string
  default = ""
}

# --- サイズ ---------------------------------------------------------------
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

variable "backend_env" {
  type        = map(string)
  default     = {}
  description = "backendコンテナに渡す追加の環境変数（モデル名・チャンク設定など）"
}
