// FastAPI への中継（app/api/backend/[...path]/route.ts）のテスト。
//
// ★Next.js は「エクスポートしたHTTPメソッド」しか proxy まで通さない★
//   足し忘れると 405 になり、しかもブラウザのコンソールを見るまで気づかない
//   （画面上は「押したのに何も起きない」だけ）。実際に👎の理由を足す PATCH で
//   これを踏んだので、対応しているメソッドが揃っていることをここで固定する。
//
// 中継の中身（content-type の転記・GETで本文を送らない・素通し）も、過去に
// multipart の boundary 消失やバイナリ破損を起こした場所なので一緒に見る。
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DELETE, GET, PATCH, POST } from "../app/api/backend/[...path]/route";

type Captured = { url: string; init: RequestInit };

function stubFetch(status = 200, body = '{"ok":true}'): Captured[] {
  const calls: Captured[] = [];
  vi.stubGlobal("fetch", (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(
      new Response(body, {
        status,
        headers: { "content-type": "application/json" },
      }),
    );
  });
  return calls;
}

/** route.ts が受け取るのと同じ形のリクエスト（nextUrl を持つ NextRequest）。 */
function request(method: string, path: string, body?: string) {
  return new NextRequest(`http://localhost:3000/api/backend/${path}`, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body,
  });
}

function params(path: string) {
  return { params: { path: path.split("?")[0].split("/") } };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  process.env.BACKEND_URL = "http://backend:8000";
});

describe("バックエンドへの中継", () => {
  it("PATCH を中継する（👎に後から理由を足す口）", async () => {
    const calls = stubFetch();
    const res = await PATCH(
      request("PATCH", "feedback/7", '{"reason":"情報が古い"}'),
      params("feedback/7"),
    );

    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://backend:8000/feedback/7");
    // メソッドを POST に丸めないこと（丸めると新しい👎が作られてしまう）
    expect(calls[0].init.method).toBe("PATCH");
    expect(calls[0].init.body).toBeDefined();
  });

  it("DELETE を中継し、本文（消す対象のid）を落とさない", async () => {
    const calls = stubFetch();
    const res = await DELETE(
      request("DELETE", "documents", '{"ids":[1,2]}'),
      params("documents"),
    );

    expect(res.status).toBe(200);
    expect(calls[0].url).toBe("http://backend:8000/documents");
    expect(calls[0].init.method).toBe("DELETE");
    // 本文が落ちると「1件も指定されていない」扱いになり、消えずに 400 になる
    expect(calls[0].init.body).toBeDefined();
  });

  it("content-type をそのまま渡す（固定するとmultipartのboundaryが消える）", async () => {
    const calls = stubFetch();
    await POST(
      request("POST", "feedback", '{"rating":-1}'),
      params("feedback"),
    );

    const headers = new Headers(calls[0].init.headers);
    expect(headers.get("content-type")).toBe("application/json");
  });

  it("GET では本文を送らない", async () => {
    const calls = stubFetch();
    await GET(request("GET", "feedback"), params("feedback"));

    expect(calls[0].init.method).toBe("GET");
    expect(calls[0].init.body).toBeUndefined();
  });

  it("エラーのステータスをそのまま返す（400/404を200に化けさせない）", async () => {
    stubFetch(404, '{"error":"feedback_not_found"}');
    const res = await PATCH(
      request("PATCH", "feedback/999", '{"reason":"読みにくい"}'),
      params("feedback/999"),
    );

    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "feedback_not_found" });
  });
});
