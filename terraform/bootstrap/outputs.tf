# ここで出た値を GitHub 側（Secrets / Variables）と environments/dev の init に渡す。

output "tfstate_bucket" {
  value       = aws_s3_bucket.tfstate.id
  description = "GitHubのRepository variable TFSTATE_BUCKET に設定する"
}

output "tflock_table" {
  value       = aws_dynamodb_table.tflock.name
  description = "environments/*/backend.tf の dynamodb_table と一致していること"
}

output "deploy_role_arn" {
  value       = aws_iam_role.deploy.arn
  description = "GitHubのRepository secret AWS_DEPLOY_ROLE_ARN に設定する"
}

output "ecr_repository_urls" {
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
  description = "deploy-dev.yml がpushする先"
}
