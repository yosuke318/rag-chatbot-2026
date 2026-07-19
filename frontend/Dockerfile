FROM node:20-alpine

WORKDIR /app

# 依存を先に入れてレイヤキャッシュを効かせる
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

EXPOSE 3000
# BACKEND_URL は起動時(next start)に読まれる → compose の environment で注入
CMD ["npm", "run", "start"]
