import type { Metadata } from "next";
import "./globals.css";
import { THEME_INIT_SCRIPT } from "./theme";

export const metadata: Metadata = {
  title: "RAG Inspector（RAG検証ラボ）",
  description: "埋め込み・検索・回答生成の挙動を観察するRAG検証ツール",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // data-theme はこの下のスクリプトがクライアントで立てるため、サーバーの
    // 出力（属性なし）と一致しない。html要素だけ差分の警告を止める。
    <html lang="ja" suppressHydrationWarning>
      <head>
        {/* ★body より前に同期実行する★ React のマウントを待つと、
            ダーク選択時に一瞬ライトで描画されてから暗転してしまう。 */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
