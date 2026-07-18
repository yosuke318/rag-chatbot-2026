import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "社内文書RAG v2",
  description: "文書を入れて質問すると根拠付きで答える最小UI",
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
