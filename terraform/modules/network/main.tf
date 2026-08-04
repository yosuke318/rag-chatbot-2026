# VPC / public+private subnet / Security Group
#
# 設計方針: ALB・NAT Gatewayは置かない（この2つで月$55かかる）。
#   - Fargateはpublic subnetに直接置き、パブリックIPを持たせる
#   - 到達できる相手は app_sg の「送信元IPホワイトリスト」で絞る
#   - RDSはprivate subnet、db_sgはapp_sgからの5432のみ許可（インターネットから不到達）
#
# アクセス制御をホワイトリストにした理由:
#   当初案の Tailscale サブネットルーター(EC2 t4g.nano)は、authkey発行とマシン承認という
#   「Terraformの外の手作業」が必ず残り、"mainへのマージだけでdevが立ち上がる"
#   という目的と噛み合わなかった。利用者10人・接続元が固定という前提なら、
#   SGの送信元IP許可リストでも「関係者以外は接続すら張れない」状態が作れて、
#   EC21台分の運用も月$3も要らなくなる。

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # 2AZに分散。RDSのsubnet groupが最低2AZを要求するため（Multi-AZにはしない）。
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # RDSのエンドポイント名を引くのに必要

  tags = { Name = var.project }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.project }
}

# --- public subnet: Fargateタスクを置く -----------------------------------
resource "aws_subnet" "public" {
  for_each = { for i, az in local.azs : az => i }

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value) # 10.0.0.0/24, 10.0.1.0/24

  # パブリックIPはECSサービス側で assign_public_ip = true として明示的に付ける。
  # subnetの既定に頼ると「どこでIPが付いているか」が読めなくなるため false のまま。
  map_public_ip_on_launch = false

  tags = { Name = "${var.project}-public-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# --- private subnet: RDSを置く（外向き経路なし＝NAT不要） -------------------
resource "aws_subnet" "private" {
  for_each = { for i, az in local.azs : az => i }

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value + 10) # 10.0.10.0/24, 10.0.11.0/24

  tags = { Name = "${var.project}-private-${each.key}" }
}

# 外向きルートを持たないルートテーブル。VPC内の通信だけができる。
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-private" }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# --- app_sg: ここがホワイトリストの本体 ------------------------------------
resource "aws_security_group" "app" {
  name        = "${var.project}-app"
  description = "Fargate tasks. reachable only from allowed_cidrs"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.project}-app" }
}

# 画面(Next.js)。allowed_cidrs の1件につき1ルールを作る。
# キー（誰のIPか）を description に残して、後から棚卸しできるようにする。
resource "aws_vpc_security_group_ingress_rule" "app_frontend" {
  for_each = var.allowed_cidrs

  security_group_id = aws_security_group.app.id
  description       = "frontend from ${each.key}"
  cidr_ipv4         = each.value
  from_port         = var.frontend_port
  to_port           = var.frontend_port
  ip_protocol       = "tcp"
}

# API(FastAPI)への直アクセス。curlや外部クライアントから /v1 を叩くとき用。
# 画面だけで足りるなら allow_direct_api = false で閉じられる。
resource "aws_vpc_security_group_ingress_rule" "app_api" {
  for_each = var.allow_direct_api ? var.allowed_cidrs : {}

  security_group_id = aws_security_group.app.id
  description       = "api from ${each.key}"
  cidr_ipv4         = each.value
  from_port         = var.api_port
  to_port           = var.api_port
  ip_protocol       = "tcp"
}

# 外向きは全許可。NAT無しなので、ECRからのイメージ取得・LLM APIの呼び出し・
# Secrets Managerの取得はすべてこの経路（IGW直）で出ていく。
resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "all outbound (ECR pull / LLM API / Secrets Manager)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# --- db_sg: app_sgからの5432のみ ------------------------------------------
resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "RDS. reachable only from app tasks"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${var.project}-db" }
}

# 送信元をCIDRではなくSGで指定する。タスクのIPは再デプロイのたびに変わるため。
resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "postgres from app tasks"
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
