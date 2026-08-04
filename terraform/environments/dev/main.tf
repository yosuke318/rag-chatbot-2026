# このファイルはモジュール呼び出しのみ。実リソース定義は modules/ 側に置く。

data "aws_caller_identity" "current" {}

# ECRリポジトリは bootstrap スタックが作る（ワークフローが apply より先に
# イメージをpushするため）。ここでは参照するだけ。
data "aws_ecr_repository" "backend" {
  name = "${var.project}-backend"
}

data "aws_ecr_repository" "frontend" {
  name = "${var.project}-frontend"
}

locals {
  # -var 指定が無ければ :latest。coalesce は空文字も飛ばしてくれる。
  backend_image  = coalesce(var.backend_image, "${data.aws_ecr_repository.backend.repository_url}:latest")
  frontend_image = coalesce(var.frontend_image, "${data.aws_ecr_repository.frontend.repository_url}:latest")
}

module "network" {
  source = "../../modules/network"

  project  = var.project
  vpc_cidr = var.vpc_cidr

  # ここがアクセス制御の本体。載っていないIPは接続すら張れない。
  allowed_cidrs    = var.allowed_cidrs
  allow_direct_api = var.allow_direct_api
}

module "secrets" {
  source = "../../modules/secrets"

  project = var.project
  # LLMキーの値はTerraform管理外。枠だけ作り、実キーはCLIで投入する（README参照）。
}

module "database" {
  source = "../../modules/database"

  project            = var.project
  private_subnet_ids = module.network.private_subnet_ids
  db_sg_id           = module.network.db_sg_id
  instance_class     = var.db_instance_class
  # DB認証情報は database モジュールが生成し、DATABASE_URL として
  # Secrets Manager に置く（平文で受け渡さない）。
}

module "ingest" {
  source = "../../modules/ingest"

  project    = var.project
  account_id = data.aws_caller_identity.current.account_id
}

module "app" {
  source = "../../modules/app"

  project           = var.project
  public_subnet_ids = module.network.public_subnet_ids
  app_sg_id         = module.network.app_sg_id

  backend_image  = local.backend_image
  frontend_image = local.frontend_image

  db_url_secret_arn      = module.database.db_url_secret_arn
  anthropic_secret_arn   = module.secrets.anthropic_secret_arn
  voyage_secret_arn      = module.secrets.voyage_secret_arn
  admin_token_secret_arn = module.secrets.admin_token_secret_arn

  documents_bucket     = module.ingest.documents_bucket
  documents_bucket_arn = module.ingest.documents_bucket_arn

  task_cpu    = var.task_cpu
  task_memory = var.task_memory
  backend_env = var.backend_env
}
