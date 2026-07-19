// FastAPI(:8000) へのプロキシは app/api/backend/[...path]/route.ts で行う。
// rewrites()はnext build時に評価されてルーティングに固定されてしまい、
// compose環境変数(BACKEND_URL)が起動時に変わっても反映されないため使わない。

/** @type {import('next').NextConfig} */
const nextConfig = {};

export default nextConfig;
