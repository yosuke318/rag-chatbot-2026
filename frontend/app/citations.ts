// チャンク単位の根拠（引用）の扱い。描画から切り離した純ロジックだけを置く。
import type { components } from "./api-types";

export type Citation = components["schemas"]["Citation"];

// 根拠の原本URL。バックエンドは環境で2種類のURLを返す:
//   "/files/..."（ローカルMinIO。backend中継）→ Next経由のプロキシに載せ替える
//   "https://..."（実S3の署名URL）→ そのまま開く
export function citationHref(url: string): string {
  return url.startsWith("/") ? `/api/backend${url}` : url;
}

/** 回答本文の一片。citation が付いていればそこが引用マーカー [n]。 */
export type AnswerPart = {
  text: string;
  citation: Citation | null;
};

/**
 * 回答本文を「素の文」と「引用マーカー」に切り分ける。
 *
 * 該当する番号の根拠が無いマーカー（Claudeが番号を書き間違えた場合）は
 * citation=null のまま素の文字として残す ＝ 存在しない根拠へのリンクを作らない。
 */
export function splitAnswer(text: string, citations: Citation[]): AnswerPart[] {
  return text
    .split(/(\[\d+\])/g)
    .filter((part) => part !== "") // 連続するマーカーの間にできる空文字を捨てる
    .map((part) => {
      const m = /^\[(\d+)\]$/.exec(part);
      const citation = m
        ? citations.find((c) => c.n === Number(m[1])) ?? null
        : null;
      return { text: part, citation };
    });
}
