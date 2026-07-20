import type { Metadata } from "next";
import "./globals.css";

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
    <html lang="ja">
      <body>{children}</body>
    </html>
  );
}
