# stateはS3バックエンド + DynamoDBロック。ローカルstate禁止。
#
# bucket はグローバル一意（アカウントIDが入る）ためリポジトリに直書きしない。
# init のときに渡す:
#   terraform init -backend-config="bucket=$(cd ../../bootstrap && terraform output -raw tfstate_bucket)"
terraform {
  backend "s3" {
    key            = "prod/terraform.tfstate"
    region         = "ap-northeast-1"
    dynamodb_table = "rag-v2-tflock" # bootstrap が作るロックテーブル
    encrypt        = true
  }
}
