"use client";

import { useState } from "react";

// バックエンドの応答型（backend/app/main.py の返り値に対応）
type ChatResponse = { answer: string; sources: string[] };
type Message = { role: "user" | "bot"; text: string; sources?: string[] };

// /search の応答型（backend/app/retrieval.py の search_stages に対応）
type Hit = { rank: number; id: number; source: string; preview: string };
type Fused = Hit & {
  score: number;
  vector_rank: number | null; // null = ベクトル検索には出てこなかった
  lexical_rank: number | null; // null = 字面検索には出てこなかった
};
type SearchStages = {
  question: string;
  vector_search: Hit[];
  lexical_search: Hit[];
  fused: Fused[];
};

export default function Home() {
  // --- 取り込みパネル（/ingest = 書き込みフロー）---
  const [source, setSource] = useState("");
  const [docText, setDocText] = useState("");
  const [ingestStatus, setIngestStatus] = useState("");

  // --- 検索パネル（/search = 検索の内訳。Claudeを呼ばない）---
  const [searchQ, setSearchQ] = useState("");
  const [stages, setStages] = useState<SearchStages | null>(null);
  const [searching, setSearching] = useState(false);

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

  async function runSearch() {
    const q = searchQ.trim();
    if (!q || searching) return;
    setSearching(true);
    try {
      const res = await fetch(`/api/backend/search?q=${encodeURIComponent(q)}`);
      setStages(await res.json());
    } catch (e) {
      setStages(null);
      alert(`検索エラー: ${String(e)}`);
    } finally {
      setSearching(false);
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

      {/* 検索の内訳: Claudeを呼ばないのでAnthropicキー不要 */}
      <section className="panel">
        <h2>② 検索の内訳を見る（/search・Claude不要）</h2>
        <div className="chat-input">
          <input
            placeholder="検索したい質問…（例: 有給は入社何ヶ月で何日？）"
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
          />
          <button onClick={runSearch} disabled={searching || !searchQ.trim()}>
            検索
          </button>
        </div>

        {stages && (
          <>
            <h3 className="stage-title">RRF融合後（最終順位）</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>順位</th>
                    <th>スコア</th>
                    <th>ベクトル順位</th>
                    <th>字面順位</th>
                    <th>出典</th>
                    <th>内容</th>
                  </tr>
                </thead>
                <tbody>
                  {stages.fused.map((f) => (
                    <tr key={f.id}>
                      <td>{f.rank}</td>
                      <td>{f.score}</td>
                      {/* null は「その検索には出てこなかった」= 片方だけのヒット */}
                      <td className={f.vector_rank === null ? "miss" : ""}>
                        {f.vector_rank ?? "—"}
                      </td>
                      <td className={f.lexical_rank === null ? "miss" : ""}>
                        {f.lexical_rank ?? "—"}
                      </td>
                      <td>{f.source}</td>
                      <td className="preview">{f.preview}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="hint">
              両方に順位が入っている＝2つの検索が揃って上位に挙げた → スコアが高い。
              「—」は片方の検索にしか出てこなかったチャンク。
            </p>

            <div className="two-col">
              <div>
                <h3 className="stage-title">① ベクトル検索（意味）</h3>
                <ol className="raw-list">
                  {stages.vector_search.map((h) => (
                    <li key={h.id}>
                      <code>#{h.id}</code> {h.preview}
                    </li>
                  ))}
                </ol>
              </div>
              <div>
                <h3 className="stage-title">① 字面検索（pg_trgm）</h3>
                <ol className="raw-list">
                  {stages.lexical_search.map((h) => (
                    <li key={h.id}>
                      <code>#{h.id}</code> {h.preview}
                    </li>
                  ))}
                </ol>
              </div>
            </div>
          </>
        )}
      </section>

      {/* 質問フロー: question → hybrid_search → rerank → Claude */}
      <section className="panel">
        <h2>③ 質問する（/chat・Anthropicキー必要）</h2>
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
          />
          <button onClick={ask} disabled={loading || !question.trim()}>
            送信
          </button>
        </div>
      </section>
    </div>
  );
}
