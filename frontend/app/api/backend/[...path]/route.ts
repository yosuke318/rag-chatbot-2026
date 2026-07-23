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
  // ボディはストリームのまま素通しする。以前は res.text() で読んでいたため、
  // ファイルダウンロード（バイナリ）が壊れていた。JSONもストリームで問題なく通る。
  const headers = new Headers();
  const ct = res.headers.get("content-type");
  if (ct) headers.set("content-type", ct);
  // ダウンロード用ヘッダ（ファイル名）も中継する。これが無いと添付保存にならない。
  const cd = res.headers.get("content-disposition");
  if (cd) headers.set("content-disposition", cd);
  return new Response(res.body, { status: res.status, headers });
}

export async function GET(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}

export async function POST(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}
