// FastAPI(:8000) への中継。BACKEND_URLはリクエストのたびに読むので、
// next.config.mjsのrewrites（ビルド時に固定される）と違い、compose起動時の値がそのまま効く。
import { NextRequest } from "next/server";

function backendUrl(path: string[], search: string) {
  const base = process.env.BACKEND_URL || "http://localhost:8000";
  return `${base}/${path.join("/")}${search}`;
}

async function proxy(req: NextRequest, path: string[]) {
  const url = backendUrl(path, req.nextUrl.search);
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  // content-type は元リクエストのものをそのまま中継する。
  // 以前は "application/json" を固定していたため、ファイルアップロード
  // （multipart/form-data）の boundary が失われ、FastAPI がファイルとして
  // 受け取れなかった。テキスト登録(JSON)も自分の content-type を持っているので
  // 素通しで問題ない。
  const reqHeaders = new Headers();
  const reqCt = req.headers.get("content-type");
  if (reqCt) reqHeaders.set("content-type", reqCt);
  const res = await fetch(url, {
    method: req.method,
    headers: reqHeaders,
    // 本文は arrayBuffer で「バイト列のまま」渡す。以前は req.text() で
    // UTF-8 文字列にデコードしていたため、PDF/XLSX/PPTX などのバイナリが
    // 壊れていた。JSON はバイト列で渡してもそのまま通る。
    body: hasBody ? await req.arrayBuffer() : undefined,
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

// 一部だけ書き換える操作（👎に後から理由を足す等）。エクスポートしていないメソッドは
// Next.js が 405 を返して proxy まで届かないので、対応するメソッドごとに口が要る。
export async function PATCH(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}
