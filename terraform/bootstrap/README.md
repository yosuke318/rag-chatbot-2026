# bootstrap — 一度きりの下ごしらえ

`main` へのマージで dev環境が立ち上がる状態にするために、**先に手元から1回だけ**
作るものをここに置いている。

| 作るもの | なぜ環境スタックに置けないか |
|---|---|
| stateバケット(S3) + ロックテーブル(DynamoDB) | devのstateの置き場をdevのstateで管理できない（鶏卵） |
| GitHub OIDC provider + デプロイ用IAMロール | Terraformを実行するための権限は、Terraformより先に要る |
| ECRリポジトリ | ワークフローは「イメージpush → apply」の順に走るので、applyより前に要る |

このスタックだけ **ローカルstate**（`terraform.tfstate` は `.gitignore` 済み）。
中身はほぼ変わらないので、それで困らない。

---

## 1. bootstrap を apply する

管理者権限のある認証情報（`aws configure` / SSO）を手元に用意してから:

```bash
cd terraform/bootstrap
terraform init
terraform apply
```

> アカウントに既に GitHub の OIDC provider がある場合は
> `terraform apply -var="create_oidc_provider=false"` にする
> （providerはアカウントに1つしか作れない）。

出力される値を控える:

```bash
terraform output
```

- `tfstate_bucket` … stateバケット名
- `deploy_role_arn` … GitHub Actions が引き受けるロール

## 2. GitHub のSecretsに登録する

リポジトリの Settings → Secrets and variables → Actions → **Secrets** に3つ:

| 名前 | 値 |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `terraform output -raw deploy_role_arn` |
| `TFSTATE_BUCKET` | `terraform output -raw tfstate_bucket` |
| `ALLOWED_CIDRS` | アクセスを許す送信元IPのJSON（下記） |

`ALLOWED_CIDRS` は「名札 => CIDR」のJSON。ここに載っていないIPからは
TCP接続そのものが張れない（＝これがホワイトリスト）。

```json
{"office":"203.0.113.10/32","maruyama-home":"198.51.100.24/32"}
```

自分のグローバルIPは `curl -s ifconfig.me` で確認できる（`/32` を付ける）。
IPが変わったらこのSecretを直して、ワークフローを再実行すれば反映される。

## 3. dev を初回applyする

イメージがまだECRに無いので、**先に一度ワークフローを回す**のが早い
（`main` にマージ、または Actions から `deploy-dev` を手動実行）。

手元から流したい場合は:

```bash
cd terraform/environments/dev
cp terraform.tfvars.example terraform.tfvars   # allowed_cidrs を自分のIPに直す
terraform init -backend-config="bucket=$(cd ../../bootstrap && terraform output -raw tfstate_bucket)"
terraform apply
```

## 4. LLM APIキーを投入する

キーの値はTerraform管理外（tfstateに本物を残さないため）。枠だけ作ってあるので
CLIで入れる。入れるまで、アプリは起動はするがLLM呼び出しで失敗する。

```bash
cd terraform/environments/dev
terraform output -raw set_llm_keys      # 実行するコマンドが出る
aws secretsmanager put-secret-value --secret-id rag-v2/anthropic-api-key --secret-string '<キー>'
aws secretsmanager put-secret-value --secret-id rag-v2/voyage-api-key    --secret-string '<キー>'
```

投入後、新しい値を読ませるためにタスクを入れ替える:

```bash
aws ecs update-service --cluster rag-v2 --service rag-v2 --force-new-deployment
```

## 5. アクセスする

ALBを置いていないので、URLは「今動いているタスクのパブリックIP」。
デプロイのたびに変わる（ワークフローのサマリにも出る）。

```bash
cd terraform/environments/dev
terraform output -raw app_url_lookup    # IPを引くコマンドが出る
```

画面は `http://<IP>:3000`、APIは `http://<IP>:8000`（`allow_direct_api = true` のとき）。

管理API(`/admin/*`)を叩くときのトークン:

```bash
terraform output -raw get_admin_token   # 取り出すコマンドが出る
```

---

## 運用メモ

### 止める・消す

```bash
# 夜間・週末に止める（RDSは別途 stop-db-instance。7日で自動再開する点に注意）
aws ecs update-service --cluster rag-v2 --service rag-v2 --desired-count 0

# 環境ごと消す（bootstrapは残す）
cd terraform/environments/dev && terraform destroy
```

`desired_count` は `ignore_changes` にしてあるので、0に落としても次のapplyで1に戻らない。

bootstrap 自体を消すときは、`main.tf` の `prevent_destroy` を外してから
`terraform destroy`（stateバケットごと消えるので、環境をdestroyした後にすること）。

### ログを見る

```bash
aws logs tail /ecs/rag-v2 --follow
```

### コンテナに入る

```bash
aws ecs execute-command --cluster rag-v2 --container backend --interactive \
  --command "/bin/sh" --task "$(aws ecs list-tasks --cluster rag-v2 --service-name rag-v2 --query 'taskArns[0]' --output text)"
```
