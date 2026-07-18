# stateはS3バックエンド + DynamoDBロック。ローカルstate禁止。
# 初回のみ: 下記のbucket / dynamodb_table を手動 or 別スタックで作成してから init する。
terraform {
  backend "s3" {
    bucket         = "TODO-rag-v2-tfstate" # 一意なバケット名に変更
    key            = "prod/terraform.tfstate"
    region         = "ap-northeast-1"
    dynamodb_table = "TODO-rag-v2-tflock"
    encrypt        = true
  }
}
