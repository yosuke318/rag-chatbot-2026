output "documents_bucket" {
  value       = module.ingest.documents_bucket
  description = "原本文書のS3バケット"
}

output "db_endpoint" {
  value       = module.database.endpoint
  description = "RDSのホスト名（private subnetなのでVPC外からは引けない）"
}

output "log_group" {
  value       = module.app.log_group
  description = "aws logs tail <この値> --follow でアプリのログが見られる"
}

# ALBを置いていないので、画面のURLは「今動いているタスクのパブリックIP」。
# タスクを置き換えるたびに変わるため、固定URLではなく引き方を出力する。
# 固定URLが要るようになったら NLB か Route53 の付け替えを検討する（設計書参照）。
output "app_url_lookup" {
  description = "画面のURLを調べるコマンド"
  value       = <<-EOT
    aws ecs list-tasks --cluster ${module.app.cluster_name} --service-name ${module.app.service_name} --query 'taskArns[0]' --output text \
      | xargs -I{} aws ecs describe-tasks --cluster ${module.app.cluster_name} --tasks {} \
          --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text \
      | xargs -I{} aws ec2 describe-network-interfaces --network-interface-ids {} \
          --query 'NetworkInterfaces[0].Association.PublicIp' --output text
  EOT
}

output "set_llm_keys" {
  description = "LLM APIキーの投入コマンド（値はTerraform管理外）"
  value = join("\n", [
    for name in module.secrets.llm_secret_names :
    "aws secretsmanager put-secret-value --secret-id ${name} --secret-string '<キーをここに>'"
  ])
}

output "get_admin_token" {
  description = "/admin/* を叩くときの X-Admin-Token の値を取り出すコマンド"
  value       = module.secrets.admin_token_secret_arn == null ? "（admin token 未作成）" : "aws secretsmanager get-secret-value --secret-id ${module.secrets.admin_token_secret_arn} --query SecretString --output text"
}
