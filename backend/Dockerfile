FROM python:3.12-slim

WORKDIR /app

# 依存を先に入れてレイヤキャッシュを効かせる
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# 初期データ(fixture)。評価質問の初期セットなどコードではないがバージョン管理する。
# compose では同じパスをホストからマウントするので、編集は再ビルド無しで効く。
COPY seed_data ./seed_data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
