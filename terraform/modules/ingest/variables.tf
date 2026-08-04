variable "project" { type = string }

variable "account_id" {
  type        = string
  description = "バケット名を一意にするために混ぜるAWSアカウントID"
}
