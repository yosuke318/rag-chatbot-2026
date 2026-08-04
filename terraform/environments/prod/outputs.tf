output "documents_bucket" { value = module.ingest.documents_bucket }

output "db_endpoint" { value = module.database.endpoint }

output "log_group" { value = module.app.log_group }

output "app_url_lookup" {
  description = "画面のURLを調べるコマンド（ALB無しのためタスクのパブリックIPを引く）"
  value       = <<-EOT
    aws ecs list-tasks --cluster ${module.app.cluster_name} --service-name ${module.app.service_name} --query 'taskArns[0]' --output text \
      | xargs -I{} aws ecs describe-tasks --cluster ${module.app.cluster_name} --tasks {} \
          --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' --output text \
      | xargs -I{} aws ec2 describe-network-interfaces --network-interface-ids {} \
          --query 'NetworkInterfaces[0].Association.PublicIp' --output text
  EOT
}
