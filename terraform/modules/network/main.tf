# VPC / public+private subnet / Security Group / Tailscaleサブネットルーター(EC2 t4g.nano)
# 設計方針: ALB・NAT Gatewayは置かない（コスト削減）。
#   - Fargateはpublic subnetに置き、app_sgでTailscale経由のみ許可
#   - RDSはprivate subnet、db_sgはapp_sgからの5432のみ許可
# TODO:
#   - aws_vpc / aws_subnet(public x2, private x2) / aws_internet_gateway / route_table
#   - aws_security_group.app（インバウンド: Tailscaleサブネットルーターからのみ）
#   - aws_security_group.db（インバウンド: app_sgからの5432のみ）
#   - aws_instance.tailscale_router（t4g.nano, Tailscale subnet router）
