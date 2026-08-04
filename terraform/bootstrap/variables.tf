variable "project" {
  type        = string
  default     = "rag-v2"
  description = "リソース名のprefix"
}

variable "region" {
  type        = string
  default     = "ap-northeast-1"
  description = "東京リージョン固定：文書データをリージョン外に出さない"
}

variable "github_repository" {
  type        = string
  description = "OIDCで信頼するGitHubリポジトリ（owner/repo）"
  default     = "yosuke318/rag-chatbot-2026"
}

variable "github_allowed_refs" {
  type        = list(string)
  description = <<-EOT
    デプロイロールを引き受けられるGitHub側の実行元（sub クレームの後半）。
    ここに書いたものだけが AssumeRole できる＝実行元のホワイトリスト。
    例: "ref:refs/heads/main"（mainブランチのpush＝PRのマージ）
        "environment:dev"（Environment経由の実行に限定したい場合）
    deploy-dev.yml のトリガーと必ず揃える（ズレるとAssumeRoleで弾かれる）。
  EOT
  default     = ["ref:refs/heads/main"]
}

variable "create_oidc_provider" {
  type        = bool
  description = <<-EOT
    GitHub Actions用のOIDC providerをこのスタックで作るか。
    アカウントに既にある（他のリポジトリで作成済み）場合は false にする。
    providerはアカウントに1つしか作れず、二重作成は EntityAlreadyExists で失敗するため。
  EOT
  default     = true
}

variable "ecr_repositories" {
  type        = list(string)
  description = "作成するECRリポジトリ名（deploy-dev.yml が push する先）"
  default     = ["rag-v2-backend", "rag-v2-frontend"]
}
