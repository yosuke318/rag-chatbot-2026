output "vpc_id" { value = aws_vpc.this.id }

# subnetは for_each で作っているのでキー順（AZ名順）で安定する。
output "public_subnet_ids" { value = [for s in aws_subnet.public : s.id] }

output "private_subnet_ids" { value = [for s in aws_subnet.private : s.id] }

output "app_sg_id" { value = aws_security_group.app.id }

output "db_sg_id" { value = aws_security_group.db.id }
