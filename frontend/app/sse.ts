// Server-Sent Events（/chat/stream）の読み取り。
//
// ここだけ独立させてあるのは、バイト列 → 文字列 → イベントへの組み立てが
// 「チャンクの切れ目」に依存する処理で、UIとは別に試せるようにするため。
// ストリームは境界が毎回変わるので、目視では踏めないバグが出る場所（実際に
// マルチバイト文字の分断で末尾が欠ける不具合をレビューで指摘された）。

export type SSEHandler = (event: string, data: any) => void;

/** SSEの1ブロック（"event: x\ndata: {...}"）を解釈してハンドラへ渡す。 */
function dispatch(block: string, onEvent: SSEHandler): void {
  const lines = block.split("\n");
  const name = lines.find((l) => l.startsWith("event: "))?.slice(7);
  const raw = lines.find((l) => l.startsWith("data: "))?.slice(6);
  // event/data が揃わないブロック（コメント行や keep-alive）は無視する
  if (!name || !raw) return;
  onEvent(name, JSON.parse(raw));
}

/**
 * ストリームを最後まで読み、届いたイベントを順に onEvent へ渡す。
 *
 * ★2つの「境界」を吸収するのがこの関数の仕事★
 *   1. 文字の境界: UTF-8のマルチバイト文字はチャンクをまたいで割れる。
 *      TextDecoder に stream:true を渡して端数を持ち越す（渡さないと割れた文字が
 *      替え字になる）。読み終わりの引数なし decode() は保険で、正常なSSEなら
 *      フレームがASCII(`}`や改行)で終わるため残らない。接続が文字の途中で
 *      切れたときだけ端数が出るので、黙って捨てずに吐き出しておく。
 *   2. イベントの境界: SSEの区切りは空行。最後の断片は次のチャンクと繋ぐため
 *      バッファに残し、ストリーム終了後に残りがあれば1ブロックとして処理する。
 */
export async function readSSE(
  body: ReadableStream<Uint8Array>,
  onEvent: SSEHandler,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) dispatch(block, onEvent);
    if (done) break;
  }
  // 終端の空行が無いまま終わった場合の取りこぼしを拾う
  if (buffer.trim()) dispatch(buffer, onEvent);
}
