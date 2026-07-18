"use client";

import { useState } from "react";

// バックエンドの応答型（backend/app/main.py の返り値に対応）
type ChatResponse = { answer: string; sources: string[] };
type Message = { role: "user" | "bot"; text: string; sources?: string[] };

export default function Home() {
  // --- 取り込みパネル（/ingest = 書き込みフロー）---
  const [source, setSource] = useState("");
  const [docText, setDocText] = useState("");
  const [ingestStatus, setIngestStatus] = useState("");

  // --- チャットパネル（/chat = 質問フロー）---
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);

  async function ingest() {
    if (!source.trim() || !docText.trim()) return;
    setIngestStatus("取り込み中…");
    try {
      const res = await fetch("/api/backend/ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ source, text: docText }),
      });
      const data = await res.json();
      setIngestStatus(`「${source}」を ${data.chunks_created} チャンクで登録しました`);
      setDocText("");
    } catch (e) {
      setIngestStatus(`エラー: ${String(e)}`);
    }
  }

  async function ask() {
    const q = question.trim();
    if (!q || loading) return;
    setMessages((m) => [...m, { role: "user", text: q }]);
    setQuestion("");
    setLoading(true);
    try {
      const res = await fetch("/api/backend/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const data: ChatResponse = await res.json();
      setMessages((m) => [
        ...m,
        { role: "bot", text: data.answer, sources: data.sources },
      ]);
    } catch (e) {
      setMessages((m) => [...m, { role: "bot", text: `エラー: ${String(e)}` }]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="container">
      <h1>社内文書RAG v2</h1>
      <p className="sub">文書を入れて質問すると、根拠付きで答える最小UI</p>

      {/* 書き込みフロー: text → chunk → embed → pgvector */}
      <section className="panel">
        <h2>① 文書を登録（/ingest）</h2>
        <input
          placeholder="文書名（例: 有給休暇.txt）"
          value={source}
          onChange={(e) => setSource(e.target.value)}
        />
        <textarea
          placeholder="本文を貼り付け…"
          value={docText}
          onChange={(e) => setDocText(e.target.value)}
        />
        <button onClick={ingest} disabled={!source.trim() || !docText.trim()}>
          登録する
        </button>
        {ingestStatus && <p className="hint">{ingestStatus}</p>}
      </section>

      {/* 質問フロー: question → hybrid_search → rerank → Claude */}
      <section className="panel">
        <h2>② 質問する（/chat）</h2>
        <div className="messages">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.text}
              {m.sources && m.sources.length > 0 && (
                <div className="sources">根拠: {m.sources.join(" / ")}</div>
              )}
            </div>
          ))}
          {loading && <div className="msg bot">考え中…</div>}
        </div>
        <div className="chat-input">
          <input
            placeholder="質問を入力…"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()}
          />
          <button onClick={ask} disabled={loading || !question.trim()}>
            送信
          </button>
        </div>
      </section>
    </div>
  );
}
