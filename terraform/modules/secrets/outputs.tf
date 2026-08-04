output "anthropic_secret_arn" { value = aws_secretsmanager_secret.llm["anthropic-api-key"].arn }

output "voyage_secret_arn" { value = aws_secretsmanager_secret.llm["voyage-api-key"].arn }

output "admin_token_secret_arn" {
  value       = var.create_admin_token ? aws_secretsmanager_secret.admin_token[0].arn : null
  description = "未作成なら null（appモジュールは null のとき ADMIN_TOKEN を注入しない）"
}

output "secret_arns" {
  value       = [for s in aws_secretsmanager_secret.llm : s.arn]
  description = "タスク実行ロールに読み取りを許可する対象（appモジュールへ渡す）"
}

output "llm_secret_names" {
  value       = [for s in aws_secretsmanager_secret.llm : s.name]
  description = "実キーを投入するときの `aws secretsmanager put-secret-value --secret-id` の値"
}
