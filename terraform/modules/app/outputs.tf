output "cluster_name" { value = aws_ecs_cluster.this.name }

output "service_name" { value = aws_ecs_service.this.name }

output "log_group" { value = aws_cloudwatch_log_group.app.name }

output "task_role_arn" { value = aws_iam_role.task.arn }
