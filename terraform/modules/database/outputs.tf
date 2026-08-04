output "endpoint" {
  value       = aws_db_instance.this.address
  description = "RDSのホスト名（ポートは別途 port）"
}

output "port" { value = aws_db_instance.this.port }

output "db_url_secret_arn" {
  value       = aws_secretsmanager_secret.db_url.arn
  description = "組み立て済みの DATABASE_URL を入れたシークレット。appモジュールがタスクに注入する"
}
