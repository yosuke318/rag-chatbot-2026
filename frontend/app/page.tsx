"use client";

import { Fragment, useEffect, useState } from "react";

// ★型はバックエンドの OpenAPI スキーマから自動生成したものを使う★
//   再生成: npm run gen:types （backend が :8000 で起動している状態で）
//   手書きしないことで、BEの型を変えたらここで型エラーになりズレに気づける。
import type { components } from "./api-types";

type ChatResponse = components["schemas"]["ChatResponse"];
type SearchStages = components["schemas"]["SearchResponse"];
type ApiError = components["schemas"]["ErrorResponse"];
type RetrieverInfo = components["schemas"]["RetrieverInfo"];

// 検索手法ごとの説明（表ヘッダーのツールチップ）。手法を足したらここに1件追加する。
const RETRIEVER_TIPS: Record<string, React.ReactNode> = {
  vector: (
    <>
      質問と文書のベクトルが<strong>どれだけ同じ向きか</strong>。1に近いほど意味が近い。
      <br />
      <br />
      pgvectorの <code>&lt;=&gt;</code> が返すコサイン<em>距離</em>を{" "}
      <code>1 - 距離</code> で類似度に直した値。言葉が違っても意味が近い文書を拾える。
    </>
  ),
  trgm: (
    <>
      <strong>名詞だけ</strong>を取り出して、文字トライグラム（3文字組）の重なりを見た値。
      0〜1で1に近いほど字面が一致。
      <br />
      <br />
      式は <code>|T(A)∩T(B)| / |T(A)∪T(B)|</code>。分母に文書側の長さが効くため、
      長い文書ほど値が下がる（名詞に絞ったのはこの分母を小さくするため）。
    </>
  ),
  bm25: (
    <>
      単語の一致を<strong>希少度(IDF)で重み付け</strong>したスコア。
      どの文書にも出る語より、珍しい語の一致を高く評価する。
    </>
  ),
};

// UI内部だけで使う型（APIには存在しない）
type Message = { role: "user" | "bot"; text: string; sources?: string[] };

/** レスポンスがエラーならUI表示用の文字列を返す。正常なら null。 */
async function errorMessage(res: Response): Promise<string | null> {
  if (res.ok) return null;
  try {
    const e: ApiError = await res.json();
    return e.hint ? `${e.message}\n${e.hint}` : e.message;
  } catch {
    return `エラーが発生しました（HTTP ${res.status}）`;
  }
}

// 見出しにカーソルを当てると説明が出る。tabIndexでキーボード操作でも開く。
function Tip({ label, children }: { label?: string; children: React.ReactNode }) {
  return (
    <span className={label ? "tip" : "tip tip-bare"} tabIndex={0}>
      {label}
      <span className="tip-mark">?</span>
      <span className="tip-body">{children}</span>
    </span>
  );
}

export default function Home() {
  // --- 取り込みパネル（/ingest = 書き込みフロー）---
  const [source, setSource] = useState("");
  const [docText, setDocText] = useState("");
  const [ingestStatus, setIngestStatus] = useState("");

  // --- 検索パネル（/search = 検索の内訳。Claudeを呼ばない）---
  const [searchQ, setSearchQ] = useState("");
  const [stages, setStages] = useState<SearchStages | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  // 選択可能な検索手法（起動時に /retrievers から取得）
  const [available, setAvailable] = useState<RetrieverInfo[]>([]);
  const [selected, setSelected] = useState<string[]>([]);

  useEffect(() => {
    fetch("/api/backend/retrievers")
      .then((r) => r.json())
      .then((d) => {
        setAvailable(d.available);
        setSelected(d.default); // 初期選択は .env の RETRIEVERS
      })
      .catch(() => {});
  }, []);

  function toggleRetriever(name: string) {
    setSelected((prev) => {
      const next = prev.includes(name)
        ? prev.filter((n) => n !== name)
        : [...prev, name];
      // 選択した順ではなく、常にチェックボックスの並び順に揃える。
      // これを省くと「あとから入れ直した手法」が表の右端に来て、
      // チェックボックスの並びと列順がズレる。
      return available.map((r) => r.name).filter((n) => next.includes(n));
    });
  }

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
      const err = await errorMessage(res);
      if (err) {
        setIngestStatus(err);
        return;
      }
      const data = await res.json();
      // replaced > 0 = 同名の既存文書を置き換えた
      const note = data.replaced ? "（同名の既存文書を置き換えました）" : "";
      setIngestStatus(
        `「${source}」を ${data.chunks_created} チャンクで登録しました${note}`,
      );
      setDocText("");
    } catch (e) {
      setIngestStatus(`エラー: ${String(e)}`);
    }
  }

  async function runSearch() {
    const q = searchQ.trim();
    if (!q || searching) return;
    setSearching(true);
    setSearchError("");
    try {
      const params = new URLSearchParams({ q });
      if (selected.length > 0) params.set("retrievers", selected.join(","));
      const res = await fetch(`/api/backend/search?${params}`);
      const err = await errorMessage(res);
      if (err) {
        setSearchError(err); // レート制限・認証エラーなどをそのまま表示
        setStages(null);
        return;
      }
      setStages(await res.json());
    } catch (e) {
      setStages(null);
      setSearchError(`通信に失敗しました: ${String(e)}`);
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
      const err = await errorMessage(res);
      if (err) {
        // Anthropicキー未設定などをチャット欄にそのまま出す
        setMessages((m) => [...m, { role: "bot", text: err }]);
        return;
      }
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
      <h1>RAG Inspector（RAG検証ラボ）</h1>
      <p className="sub">
        埋め込み・検索・回答生成の挙動を観察するRAG検証ツール。
        文書を登録し、検索の内訳（cos類似度 / 字面類似度 / RRF融合）を確かめてから質問できる。
      </p>

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
          <button
            onClick={runSearch}
            disabled={searching || !searchQ.trim() || selected.length === 0}
          >
            検索
          </button>
        </div>

        {/* 使う検索手法を選ぶ。RRFは可変長なので何本でも融合できる */}
        <div className="retriever-picker">
          {available.map((r) => (
            <span key={r.name} className="retriever-option">
              <label>
                <input
                  type="checkbox"
                  checked={selected.includes(r.name)}
                  onChange={() => toggleRetriever(r.name)}
                />
                {r.label}
              </label>
              {/* 手法の説明。表ヘッダーと同じ内容を使い回す */}
              <Tip>{RETRIEVER_TIPS[r.name] ?? "この手法が計算した生スコア。"}</Tip>
            </span>
          ))}
          {selected.length === 0 && (
            <span className="picker-warn">手法を1つ以上選んでください</span>
          )}
        </div>

        {searchError && <p className="error-note">{searchError}</p>}

        {stages && (
          <>
            <h3 className="stage-title">RRF融合後（最終順位）</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>順位</th>
                    <th>
                      <Tip label="RRFスコア">
                        <strong>Reciprocal Rank Fusion</strong>（逆数・順位・融合）。
                        各検索での順位を <code>1/(60+順位)</code> に変換して足し合わせた値。
                        <br />
                        <br />
                        使うのは<strong>順位だけ</strong>。だから生スコアのスケールが
                        まるで違う手法同士でも公平に混ぜられる。
                        複数の検索が上位に挙げたチャンクほど逆数が重ねて足され、高スコアになる。
                      </Tip>
                    </th>
                    {/* 検索手法ごとに2列（順位・生スコア）。手法が増えれば列も増える */}
                    {stages.stages.map((st) => (
                      <th key={st.name} colSpan={2} className="group-head">
                        {st.label}
                      </th>
                    ))}
                    <th>出典</th>
                    <th>内容</th>
                  </tr>
                  <tr className="sub-head">
                    <th />
                    <th />
                    {stages.stages.map((st) => (
                      <Fragment key={st.name}>
                        <th>順位</th>
                        <th>
                          <Tip label={st.metric_label}>
                            {RETRIEVER_TIPS[st.name] ?? "この手法が計算した生スコア。"}
                          </Tip>
                        </th>
                      </Fragment>
                    ))}
                    <th />
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {stages.fused.map((f) => (
                    <tr key={f.id}>
                      <td>{f.rank}</td>
                      <td>{f.score}</td>
                      {f.contributions.map((c) => (
                        <Fragment key={c.retriever}>
                          {/* rank が null = その手法のリストに出てこなかった */}
                          <td className={c.rank === null ? "miss" : ""}>
                            {c.rank ?? "—"}
                            {c.rrf_term !== null && (
                              <span className="term">+{c.rrf_term}</span>
                            )}
                          </td>
                          <td className={c.metric_value === null ? "miss" : ""}>
                            {c.metric_value ?? "—"}
                          </td>
                        </Fragment>
                      ))}
                      <td>{f.source}</td>
                      <td className="preview">{f.preview}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="hint">
              各手法の「順位」の下にある <span className="term">+0.0164</span> が
              <strong>その手法がRRFスコアに足した分</strong>。
              複数の手法が票を投じたチャンクほど合計が大きくなる。
              「—」はその手法のリストに出てこなかったことを示す。
            </p>

            {/* 融合前：各手法の生ランキング */}
            <div className="two-col">
              {stages.stages.map((st) => (
                <div key={st.name}>
                  <h3 className="stage-title">
                    {st.label}（{st.metric_label}）
                  </h3>
                  {st.hits.length === 0 ? (
                    <p className="empty-note">
                      ヒットなし。この手法は票を投じないので、
                      <strong>RRFは他の手法の順位だけで決まっています</strong>。
                      {st.name === "trgm" &&
                        `（閾値 ${stages.lexical_min_similarity} 未満は除外）`}
                    </p>
                  ) : (
                    <ol className="raw-list">
                      {st.hits.map((h) => (
                        <li key={h.id}>
                          <code>#{h.id}</code>{" "}
                          <span className="metric">{h.metric_value}</span> {h.preview}
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              ))}
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
