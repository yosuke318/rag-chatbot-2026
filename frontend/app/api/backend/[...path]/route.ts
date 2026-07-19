// FastAPI(:8000) への中継。BACKEND_URLはリクエストのたびに読むので、
// next.config.mjsのrewrites（ビルド時に固定される）と違い、compose起動時の値がそのまま効く。
import { NextRequest } from "next/server";

function backendUrl(path: string[], search: string) {
  const base = process.env.BACKEND_URL || "http://localhost:8000";
  return `${base}/${path.join("/")}${search}`;
}

async function proxy(req: NextRequest, path: string[]) {
  const url = backendUrl(path, req.nextUrl.search);
  const res = await fetch(url, {
    method: req.method,
    headers: { "content-type": "application/json" },
    body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.text(),
  });
  return new Response(await res.text(), {
    status: res.status,
    headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
  });
}

export async function GET(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}

export async function POST(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}
