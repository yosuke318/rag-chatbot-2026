# stateはS3バックエンド + DynamoDBロック。ローカルstate禁止。
#
# bucket だけ書いていないのは、バケット名にアカウントIDが入って環境ごとに違うため
# （S3のバケット名はグローバル一意なのでリポジトリに直書きできない）。
# 初期化のときに渡す:
#
#   terraform init -backend-config="bucket=$(cd ../../bootstrap && terraform output -raw tfstate_bucket)"
#
# CIでは GitHub の Repository variable TFSTATE_BUCKET から渡す（deploy-dev.yml 参照）。
terraform {
  backend "s3" {
    key            = "dev/terraform.tfstate"
    region         = "ap-northeast-1"
    dynamodb_table = "rag-v2-tflock" # bootstrap が作るロックテーブル
    encrypt        = true
  }
}
