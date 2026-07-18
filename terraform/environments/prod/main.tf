# このファイルはモジュール呼び出しのみ。実リソース定義は modules/ 側に置く。

module "network" {
  source   = "../../modules/network"
  vpc_cidr = var.vpc_cidr
  project  = var.project
}

module "secrets" {
  source  = "../../modules/secrets"
  project = var.project
  # TODO: LLM APIキー等はコンソール/CLIで値を投入し、ここではARN参照のみ扱う
}

module "database" {
  source             = "../../modules/database"
  project            = var.project
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  db_sg_id           = module.network.db_sg_id
  # DB認証情報はSecrets Manager経由（module.secrets）。平文で渡さない。
  db_secret_arn = module.secrets.db_secret_arn
}

module "app" {
  source            = "../../modules/app"
  project           = var.project
  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  app_sg_id         = module.network.app_sg_id
  db_endpoint       = module.database.endpoint
  llm_secret_arn    = module.secrets.llm_secret_arn
  db_secret_arn     = module.secrets.db_secret_arn
}

module "ingest" {
  source         = "../../modules/ingest"
  project        = var.project
  db_endpoint    = module.database.endpoint
  llm_secret_arn = module.secrets.llm_secret_arn
  db_secret_arn  = module.secrets.db_secret_arn
  # TODO: S3(原本) + EventBridge + 取り込みECSタスク定義
}
