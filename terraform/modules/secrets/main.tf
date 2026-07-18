# Secrets Manager: LLM APIキー / DB認証情報
# 設計方針: 値はTerraform管理外（コンソール/CLIで投入）。tfstateに平文を残さない。
#   ここでは空のシークレット枠だけ作り、ARNを他モジュールへ渡す。
# TODO: aws_secretsmanager_secret.llm / aws_secretsmanager_secret.db
