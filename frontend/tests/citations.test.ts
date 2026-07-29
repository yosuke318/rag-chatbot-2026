// 引用（チャンク単位の根拠）の切り分けとURLのテスト。
//
// 「回答本文の [n] が本当にその根拠を指すか」はこの機能の前提そのもの。
// 番号がズレたり、存在しない番号がリンクになったりしないことを固定する。
import { describe, expect, it } from "vitest";

import { citationHref, splitAnswer, type Citation } from "../app/citations";

const CITATIONS: Citation[] = [
  {
    n: 1,
    chunk_id: 134,
    source: "有給休暇.txt",
    preview: "年次有給休暇は…",
    file_url: "/files/%E6%9C%89%E7%B5%A6.txt",
  },
  {
    n: 2,
    chunk_id: 203,
    source: "経費精算.txt",
    preview: "経費は翌月5日までに…",
    file_url: null,
  },
];

describe("splitAnswer", () => {
  it("マーカーをその番号の根拠に結び付ける", () => {
    const parts = splitAnswer("10日です。[1] 経費は翌月5日。[2]", CITATIONS);

    expect(parts.map((p) => p.text)).toEqual([
      "10日です。",
      "[1]",
      " 経費は翌月5日。",
      "[2]",
    ]);
    expect(parts[1].citation?.chunk_id).toBe(134);
    expect(parts[3].citation?.chunk_id).toBe(203);
    // 素の文にはマーカーが付かない
    expect(parts[0].citation).toBeNull();
  });

  it("★存在しない番号はリンクにしない★", () => {
    // Claudeが渡していない番号を書くことがある。開けない根拠へ誘導しない。
    const parts = splitAnswer("根拠なし。[9]", CITATIONS);

    expect(parts.map((p) => p.text)).toEqual(["根拠なし。", "[9]"]);
    expect(parts[1].citation).toBeNull();
  });

  it("連続したマーカーをそれぞれ結び付ける", () => {
    const parts = splitAnswer("両方が根拠です。[1][2]", CITATIONS);

    expect(parts.map((p) => p.text)).toEqual(["両方が根拠です。", "[1]", "[2]"]);
    expect(parts.map((p) => p.citation?.n)).toEqual([undefined, 1, 2]);
  });

  it("マーカーが無い回答はそのまま1つの文になる", () => {
    const parts = splitAnswer("資料からは分かりません。", CITATIONS);

    expect(parts).toEqual([{ text: "資料からは分かりません。", citation: null }]);
  });

  it("根拠が空でも落ちない（マーカーは素の文字として残る）", () => {
    const parts = splitAnswer("10日です。[1]", []);

    expect(parts.every((p) => p.citation === null)).toBe(true);
  });

  it("ストリーミング途中の未完成なマーカーは切り出さない", () => {
    // "[1]" が届く前の "[1" の段階で誤ってボタン化しないこと
    const parts = splitAnswer("10日です。[1", CITATIONS);

    expect(parts).toEqual([{ text: "10日です。[1", citation: null }]);
  });
});

describe("citationHref", () => {
  it("backend中継のパスはNextのプロキシに載せ替える", () => {
    // ローカル(MinIO)経由。ブラウザからは /api/backend/... でしか届かない
    expect(citationHref("/files/a.pdf")).toBe("/api/backend/files/a.pdf");
  });

  it("署名URL（実S3）はそのまま開く", () => {
    const signed = "https://bucket.s3.amazonaws.com/a.pdf?X-Amz-Signature=abc";
    expect(citationHref(signed)).toBe(signed);
  });
});
