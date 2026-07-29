// SSE(/chat/stream)の読み取りテスト。
//
// ★チャンクの切れ目を自分で決められるのがこのテストの価値★
//   実ブラウザではネットワーク任せで境界を再現できないため、目視では踏めない。
//   バイト単位で刻んで、どこで割れても壊れないことを固定する。
import { describe, expect, it } from "vitest";

import { readSSE } from "../app/sse";

/** 与えたバイト列の並びを、そのままチャンクとして流すストリームを作る。 */
function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(c);
      controller.close();
    },
  });
}

/** 文字列をUTF-8にして、size バイトずつに切ったチャンク列にする。 */
function chunked(text: string, size: number): Uint8Array[] {
  const bytes = new TextEncoder().encode(text);
  const out: Uint8Array[] = [];
  for (let i = 0; i < bytes.length; i += size) out.push(bytes.slice(i, i + size));
  return out;
}

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

async function collect(chunks: Uint8Array[]): Promise<[string, any][]> {
  const events: [string, any][] = [];
  await readSSE(streamOf(chunks), (name, data) => events.push([name, data]));
  return events;
}

const BODY =
  sse("meta", { conversation_id: 5, sources: ["有給休暇.txt"], citations: [] }) +
  sse("delta", { text: "入社6か月で" }) +
  sse("delta", { text: "10日です。[1]" }) +
  sse("done", { conversation_id: 5 });

describe("readSSE", () => {
  it("イベントを順番どおりに渡す", async () => {
    const events = await collect(chunked(BODY, 4096));

    expect(events.map(([name]) => name)).toEqual(["meta", "delta", "delta", "done"]);
    expect(events[0][1].conversation_id).toBe(5);
    expect(events[3][1]).toEqual({ conversation_id: 5 });
  });

  it("イベントの途中で分断されても組み立て直す", async () => {
    // 1バイトずつ ＝ あらゆる境界で割れる最悪ケース
    const events = await collect(chunked(BODY, 1));

    expect(events.map(([name]) => name)).toEqual(["meta", "delta", "delta", "done"]);
  });

  it("★マルチバイト文字がチャンク境界で割れても欠けない★", async () => {
    // 日本語は1文字3バイト。decode に stream:true を渡し忘れると、割れた文字が
    // 替え字(U+FFFD)になって回答が文字化けする。境界を総当たりして防ぐ。
    const body = sse("delta", { text: "有給は10日です" });
    for (const size of [1, 2, 3, 5, 7]) {
      const events = await collect(chunked(body, size));
      expect(events).toEqual([["delta", { text: "有給は10日です" }]]);
    }
  });

  it("終端の空行が無くても最後のイベントを取りこぼさない", async () => {
    const body = sse("delta", { text: "A" }) + 'event: done\ndata: {"conversation_id":1}';
    const events = await collect(chunked(body, 4096));

    expect(events.map(([name]) => name)).toEqual(["delta", "done"]);
  });

  it("event/data が揃わないブロックは無視する", async () => {
    // SSEのコメント行（keep-alive）や、data だけのブロックを混ぜる
    const body = ": ping\n\n" + "data: {}\n\n" + sse("done", { conversation_id: 1 });
    const events = await collect(chunked(body, 4096));

    expect(events).toEqual([["done", { conversation_id: 1 }]]);
  });

  it("何も流れてこなくても落ちない", async () => {
    expect(await collect([])).toEqual([]);
  });
});
