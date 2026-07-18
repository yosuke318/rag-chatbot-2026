// FastAPI(:8000) へのプロキシ。ブラウザからは同一オリジンの /api/backend/* に投げ、
// Next.jsがサーバ側でバックエンドへ中継する → CORS設定が不要になる。
const backend = process.env.BACKEND_URL || "http://localhost:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      { source: "/api/backend/:path*", destination: `${backend}/:path*` },
    ];
  },
};

export default nextConfig;
