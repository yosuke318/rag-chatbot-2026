# frontend — 最小チャットUI

Next.js (App Router + TypeScript)。バックエンドの2つのフローを1画面で叩ける。

```
① 文書を登録   → POST /ingest   （書き込みフロー）
② 質問する     → POST /chat     （質問フロー、根拠付きで回答表示）
```

## 構成

```
app/
├── layout.tsx     ルートレイアウト
├── page.tsx       ★UI本体（取り込みパネル + チャットパネル）
└── globals.css    スタイル
next.config.mjs    /api/backend/* → FastAPI(:8000) へプロキシ（CORS回避）
```

## 動かす手順

```bash
# 前提: バックエンドが localhost:8000 で起動していること（backend/README.md）

cd frontend
npm install
npm run dev          # http://localhost:3000
```

バックエンドが別ホストなら `BACKEND_URL` で指定:

```bash
BACKEND_URL=http://localhost:8000 npm run dev
```

## 設計メモ

- **Vercel AI SDK はまだ使っていない。** バックエンドの `/chat` が「回答を一括で返すJSON」なので、
  素の `fetch` で十分。回答をストリーミング表示にする段で Vercel AI SDK に載せ替える（設計書の次段）。
- **CORSを避けるため Next.js の rewrites でプロキシ**している。ブラウザは同一オリジンの
  `/api/backend/*` に投げ、Next.jsがサーバ側で FastAPI に中継する。
