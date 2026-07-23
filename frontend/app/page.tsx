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
type ParamSpec = components["schemas"]["ParamSpec"];

// 評価パネル用。api-types.ts は backend 起動下で `npm run gen:types` して再生成する
// ため、それまでの間はここで最小の型を持つ（再生成後は components["schemas"]["EvalReport"]
// に寄せられる）。
type EvalResult = {
  question: string;
  expected_source: string;
  hit: boolean;
  rank: number | null;
  retrieved: string[];
};
type EvalReport = {
  n: number;
  top_k: number;
  retrievers: string[] | null;
  rerank: boolean | null;
  rrf_k: number | null;
  params: Record<string, Record<string, number>> | null;
  hit_at_k: number;
  mrr: number;
  results: EvalResult[];
};

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
// question: 👍/👎 を送るとき評価対象を復元するため、bot回答に元の質問を持たせる。
//           これが入っている bot メッセージだけがフィードバック対象（エラーは対象外）。
// rating:   送信済みの評価。二重送信を防ぎ、選んだ側をハイライトする。
type Message = {
  role: "user" | "bot";
  text: string;
  sources?: string[];
  question?: string;
  rating?: 1 | -1;
};

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

// 出典名を、S3(ローカルはMinIO)にある原本のダウンロードリンクにする。
// この変更より前に登録した文書は原本が無く404になる（/admin/backfill-files で後埋め可）。
function SourceLink({ source }: { source: string }) {
  return (
    <a
      className="source-link"
      href={`/api/backend/files/${encodeURIComponent(source)}`}
      download={source}
    >
      {source}
    </a>
  );
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
  const [fusionParams, setFusionParams] = useState<ParamSpec[]>([]);
  // 入力値。空文字なら送らない = バックエンドの既定が使われる
  const [paramValues, setParamValues] = useState<Record<string, string>>({});

  useEffect(() => {
    fetch("/api/backend/retrievers")
      .then((r) => r.json())
      .then((d) => {
        setAvailable(d.available);
        setSelected(d.default); // 初期選択は .env の RETRIEVERS
        setEvalSelected(d.default); // 評価パネルも同じ既定から始める
        setFusionParams(d.fusion_params);
        // 入力欄に既定値を入れておく。空欄のままだとステッパー(▲▼)が
        // 既定値からの増減にならないため。
        const defaults: Record<string, string> = {};
        for (const sp of d.fusion_params) defaults[sp.name] = String(sp.default);
        for (const r of d.available)
          for (const sp of r.params)
            defaults[`${r.name}_${sp.name}`] = String(sp.default);
        setParamValues(defaults);
        setEvalParamValues(defaults); // 評価パネルも同じ既定から始める
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

  // --- 評価パネル（/eval = 質問集で Hit@k / MRR を測る）---
  const [evalSelected, setEvalSelected] = useState<string[]>([]);
  const [evalRerank, setEvalRerank] = useState(false);
  const [evalCompany, setEvalCompany] = useState("");
  const [evalDepartment, setEvalDepartment] = useState("");
  const [evalReport, setEvalReport] = useState<EvalReport | null>(null);
  const [evalRunning, setEvalRunning] = useState(false);
  const [evalError, setEvalError] = useState("");
  // 評価用の数値パラメータ。②と同じキー（rrf_k / trgm_min_similarity / bm25_k1 / bm25_b）
  const [evalParamValues, setEvalParamValues] = useState<Record<string, string>>({});

  // 評価用の質問を登録するフォーム（POST /eval-questions）
  const [newQ, setNewQ] = useState("");
  const [newExpected, setNewExpected] = useState("");
  const [newQCompany, setNewQCompany] = useState("");
  const [newQDepartment, setNewQDepartment] = useState("");
  const [newQNote, setNewQNote] = useState("");
  const [addQStatus, setAddQStatus] = useState("");
  const [addingQ, setAddingQ] = useState(false);

  async function addEvalQuestion() {
    if (addingQ) return;
    setAddingQ(true);
    setAddQStatus("");
    try {
      const res = await fetch("/api/backend/eval-questions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          question: newQ,
          expected_source: newExpected,
          // 空欄は送らない（＝共通の質問として登録）
          company: newQCompany.trim() || null,
          department: newQDepartment.trim() || null,
          note: newQNote.trim() || null,
        }),
      });
      // 空入力なら backend が 400 を返す。その message をそのまま表示する
      const err = await errorMessage(res);
      if (err) {
        setAddQStatus(err);
        return;
      }
      setAddQStatus(`「${newQ}」を登録しました（正解: ${newExpected}）`);
      setNewQ("");
      setNewExpected("");
      setNewQNote("");
    } catch (e) {
      setAddQStatus(`エラー: ${String(e)}`);
    } finally {
      setAddingQ(false);
    }
  }

  function toggleEvalRetriever(name: string) {
    setEvalSelected((prev) => {
      const next = prev.includes(name)
        ? prev.filter((n) => n !== name)
        : [...prev, name];
      return available.map((r) => r.name).filter((n) => next.includes(n));
    });
  }

  async function runEval() {
    if (evalRunning) return;
    setEvalRunning(true);
    setEvalError("");
    try {
      const params = new URLSearchParams({ top_k: "4" });
      if (evalSelected.length > 0) params.set("retrievers", evalSelected.join(","));
      if (evalRerank) params.set("rerank", "true");
      if (evalCompany.trim()) params.set("company", evalCompany.trim());
      if (evalDepartment.trim()) params.set("department", evalDepartment.trim());
      // 数値パラメータ。空欄は送らず backend の既定値を使わせる（②と同じ）。
      // trgm/bm25 のパラメータは、その手法を選んでいるときだけ送る。
      for (const [key, value] of Object.entries(evalParamValues)) {
        if (value === "") continue;
        const owner = key.split("_")[0]; // "trgm" / "bm25" / "rrf"(=rrf_k)
        if ((owner === "trgm" || owner === "bm25") && !evalSelected.includes(owner)) {
          continue;
        }
        params.set(key, value);
      }
      const res = await fetch(`/api/backend/eval?${params}`);
      const err = await errorMessage(res);
      if (err) {
        setEvalError(err);
        setEvalReport(null);
        return;
      }
      setEvalReport(await res.json());
    } catch (e) {
      setEvalReport(null);
      setEvalError(`通信に失敗しました: ${String(e)}`);
    } finally {
      setEvalRunning(false);
    }
  }

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
      // 空欄のものは送らず、バックエンドの既定値を使わせる
      for (const [key, value] of Object.entries(paramValues)) {
        if (value !== "") params.set(key, value);
      }
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
      // question を持たせておくと、この回答に 👍/👎 を付けられる（送信時に復元する）
      setMessages((m) => [
        ...m,
        { role: "bot", text: data.answer, sources: data.sources, question: q },
      ]);
    } catch (e) {
      setMessages((m) => [...m, { role: "bot", text: `エラー: ${String(e)}` }]);
    } finally {
      setLoading(false);
    }
  }

  /** 回答に 👍/👎 を送る。楽観的に印を付け、失敗したら戻す。 */
  async function sendFeedback(index: number, rating: 1 | -1) {
    const msg = messages[index];
    if (!msg || msg.role !== "bot" || !msg.question || msg.rating) return;
    // 先に印を付ける（二重送信を防ぐ）
    setMessages((m) =>
      m.map((x, i) => (i === index ? { ...x, rating } : x)),
    );
    try {
      const res = await fetch("/api/backend/feedback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          question: msg.question,
          answer: msg.text,
          sources: msg.sources ?? [],
          rating,
        }),
      });
      if (!res.ok) throw new Error(String(res.status));
    } catch {
      // 失敗したら印を戻して再送できるようにする
      setMessages((m) =>
        m.map((x, i) => (i === index ? { ...x, rating: undefined } : x)),
      );
    }
  }

  return (
    <div className="container">
      <h1>RAG Inspector（RAG検証ラボ）</h1>
      <p className="sub">
        埋め込み・検索・回答生成の挙動を観察するRAG検証ツール。
        文書を登録し、検索の内訳（cos類似度 / 字面類似度 / RRF融合）を確かめてから質問できる。
      </p>

      {/* どの操作にどのAPIキーが要るか。混同しやすいのでここで一度だけ説明する */}
      <div className="keys-note">
        <strong>APIキーの要否</strong>
        <ul>
          <li>
            <code>VOYAGE_API_KEY</code>（埋め込み）… <b>登録と検索の両方で必要</b>。
            文書も質問も同じモデルでベクトル化するため、検索のたびに1回呼ぶ
            （消費するのは質問文ぶんの数十トークン）
          </li>
          <li>
            <code>ANTHROPIC_API_KEY</code>（生成）… <b>回答生成とLLMリランクのみ</b>。
            検索の内訳を見るだけなら不要
          </li>
        </ul>
      </div>

      {/* 書き込みフロー: text → chunk → embed → pgvector */}
      <section className="panel">
        <h2>① 文書を登録（/ingest・Voyageキー必要）</h2>
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
        <h2>
          <Tip label="② 検索の内訳を見る">
            ここでは<strong>ハイブリッド検索</strong>を行う。
            性質の違う複数の検索を同時に走らせ、結果を1つの順位に統合する方式。
            <br />
            <br />
            <strong>1.</strong> 下のチェックボックスで<strong>選んだ手法だけ</strong>が実行される。
            各手法は着眼点が違う（意味の近さ / 字面の一致 / 単語の希少度）ので、
            それぞれ独立に別の順位を付ける。
            <br />
            <br />
            <strong>2.</strong> それらの順位を<strong>RRF</strong>で融合し、1つの最終順位にまとめる。
            複数の手法が揃って上位に挙げた文書ほど上に来る。
            <br />
            <br />
            <strong>3.</strong> 融合後の<strong>上位ほど質問に合う文書</strong>と判断される。
            1位が質問の内容と一致していれば検索は成功。
            この上位チャンクが、そのまま ③ の回答生成で根拠として使われる。
          </Tip>
          （/search・Voyageキー必要 / Anthropicキー不要）
        </h2>
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

        {/* 数式の定数。仕様(PARAM_SPECS)から生成するので画面に定数を持たない */}
        {(() => {
          const rows: { key: string; spec: ParamSpec; owner: string }[] = [];
          // 融合(RRF)は手法によらず常に効くので先頭に固定
          for (const sp of fusionParams) {
            rows.push({ key: sp.name, spec: sp, owner: "RRF融合" });
          }
          for (const r of available) {
            if (!selected.includes(r.name)) continue; // 選択中の手法だけ
            for (const sp of r.params) {
              rows.push({ key: `${r.name}_${sp.name}`, spec: sp, owner: r.label });
            }
          }
          if (rows.length === 0) return null;
          return (
            <div className="param-grid">
              {rows.map(({ key, spec, owner }) => (
                <label key={key} className="param-item">
                  <span className="param-label">
                    <span className="param-owner">{owner}</span>
                    <Tip label={spec.label}>{spec.description}</Tip>
                  </span>
                  <span className="param-input">
                    <input
                      type="number"
                      min={spec.min}
                      max={spec.max}
                      step={spec.step}
                      placeholder={`既定 ${spec.default}`}
                      value={paramValues[key] ?? ""}
                      onChange={(e) =>
                        setParamValues((prev) => ({ ...prev, [key]: e.target.value }))
                      }
                    />
                    {/* この項目だけ既定に戻す */}
                    <button
                      className="reset-one"
                      title={`既定 ${spec.default} に戻す`}
                      onClick={() =>
                        setParamValues((prev) => ({
                          ...prev,
                          [key]: String(spec.default),
                        }))
                      }
                      disabled={paramValues[key] === String(spec.default)}
                    >
                      ↺
                    </button>
                  </span>
                </label>
              ))}
            </div>
          );
        })()}

        {searchError && <p className="error-note">{searchError}</p>}

        {stages && (
          <>
            <h3 className="stage-title">
              RRF融合後（最終順位）
              <span className="applied">
                rrf_k={stages.applied_params.rrf_k}
                {Object.entries(stages.applied_params.retrievers).map(([r, ps]) =>
                  Object.entries(ps).map(([k, v]) => (
                    <span key={`${r}.${k}`}>
                      {" · "}
                      {r}.{k}={v}
                    </span>
                  )),
                )}
              </span>
            </h3>
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
                      <td>
                        <SourceLink source={f.source} />
                      </td>
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
          </>
        )}
      </section>

      {/* 質問フロー: question → hybrid_search → rerank → Claude */}
      <section className="panel">
        <h2>③ 質問する（/chat・Voyage + Anthropicキー必要）</h2>
        <div className="messages">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.text}
              {m.sources && m.sources.length > 0 && (
                <div className="sources">
                  根拠:{" "}
                  {m.sources.map((s, si) => (
                    <Fragment key={s}>
                      {si > 0 && " / "}
                      <SourceLink source={s} />
                    </Fragment>
                  ))}
                </div>
              )}
              {/* question を持つ bot回答だけ 👍/👎 を出す（エラー回答は対象外） */}
              {m.role === "bot" && m.question && (
                <div className="feedback">
                  <button
                    className={`fb ${m.rating === 1 ? "fb-on" : ""}`}
                    onClick={() => sendFeedback(i, 1)}
                    disabled={!!m.rating}
                    title="役に立った"
                    aria-label="役に立った"
                  >
                    👍
                  </button>
                  <button
                    className={`fb ${m.rating === -1 ? "fb-on" : ""}`}
                    onClick={() => sendFeedback(i, -1)}
                    disabled={!!m.rating}
                    title="的外れ"
                    aria-label="的外れ"
                  >
                    👎
                  </button>
                  {m.rating && <span className="fb-thanks">記録しました</span>}
                </div>
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

      {/* 評価フロー: 質問集(eval_questions) → 各問を検索 → Hit@k / MRR を集計 */}
      <section className="panel">
        <h2>
          <Tip label="④ 評価する">
            登録済みの<strong>質問集（正解ラベル付き）</strong>を一気に検索して、
            <strong>どれだけ正解文書を上位で拾えたか</strong>を集計する。
            <br />
            <br />
            ② が「1問を深く見る」のに対し、④ は「質問集<strong>全体</strong>で当たるか」を見る。
            手法やリランクを変えて<strong>数字が上がるか下がるか</strong>で改良の効果を判定できる。
            <br />
            <br />
            質問は会社・部署ごとに分けて登録できる（<code>POST /eval-questions</code>）。
            まだ無ければ <code>python -m app.eval --seed</code> でサンプルを投入。
          </Tip>
          （/eval・Voyageキー必要 / リランク時のみAnthropic）
        </h2>

        {/* 評価用の質問を登録する（正解ラベル付き） */}
        <div className="eval-add">
          <h3 className="stage-title">評価用の質問を登録（/eval-questions）</h3>
          <input
            placeholder="質問（例: 有給は入社何ヶ月で何日？）"
            value={newQ}
            onChange={(e) => setNewQ(e.target.value)}
          />
          <input
            placeholder="正解の文書名（例: 有給休暇.txt）"
            value={newExpected}
            onChange={(e) => setNewExpected(e.target.value)}
          />
          <div className="eval-add-row">
            <input
              placeholder="会社（任意）"
              value={newQCompany}
              onChange={(e) => setNewQCompany(e.target.value)}
            />
            <input
              placeholder="部署（任意）"
              value={newQDepartment}
              onChange={(e) => setNewQDepartment(e.target.value)}
            />
          </div>
          <input
            placeholder="メモ（任意・何を確かめる質問か）"
            value={newQNote}
            onChange={(e) => setNewQNote(e.target.value)}
          />
          <button onClick={addEvalQuestion} disabled={addingQ}>
            {addingQ ? "登録中…" : "質問を追加"}
          </button>
          {addQStatus && <p className="hint">{addQStatus}</p>}
        </div>

        {/* 評価対象の絞り込みと手法選択 */}
        <div className="eval-controls">
          <input
            placeholder="会社（任意）"
            value={evalCompany}
            onChange={(e) => setEvalCompany(e.target.value)}
          />
          <input
            placeholder="部署（任意）"
            value={evalDepartment}
            onChange={(e) => setEvalDepartment(e.target.value)}
          />
          <button onClick={runEval} disabled={evalRunning || evalSelected.length === 0}>
            {evalRunning ? "評価中…" : "検証する"}
          </button>
        </div>
        <div className="retriever-picker">
          {available.map((r) => (
            <span key={r.name} className="retriever-option">
              <label>
                <input
                  type="checkbox"
                  checked={evalSelected.includes(r.name)}
                  onChange={() => toggleEvalRetriever(r.name)}
                />
                {r.label}
              </label>
            </span>
          ))}
          <span className="retriever-option">
            <label>
              <input
                type="checkbox"
                checked={evalRerank}
                onChange={(e) => setEvalRerank(e.target.checked)}
              />
              LLMリランク（要Anthropic）
            </label>
          </span>
          {evalSelected.length === 0 && (
            <span className="picker-warn">手法を1つ以上選んでください</span>
          )}
        </div>

        {/* 数値パラメータ。②と同じ仕様(PARAM_SPECS)から生成し、同じキーで送る。
            これを変えて再検証すると Hit@k / MRR が動く＝パラメータの効果を数値化できる */}
        {(() => {
          const rows: { key: string; spec: ParamSpec; owner: string }[] = [];
          for (const sp of fusionParams) {
            rows.push({ key: sp.name, spec: sp, owner: "RRF融合" });
          }
          for (const r of available) {
            if (!evalSelected.includes(r.name)) continue;
            for (const sp of r.params) {
              rows.push({ key: `${r.name}_${sp.name}`, spec: sp, owner: r.label });
            }
          }
          if (rows.length === 0) return null;
          return (
            <div className="param-grid">
              {rows.map(({ key, spec, owner }) => (
                <label key={key} className="param-item">
                  <span className="param-label">
                    <span className="param-owner">{owner}</span>
                    <Tip label={spec.label}>{spec.description}</Tip>
                  </span>
                  <span className="param-input">
                    <input
                      type="number"
                      min={spec.min}
                      max={spec.max}
                      step={spec.step}
                      placeholder={`既定 ${spec.default}`}
                      value={evalParamValues[key] ?? ""}
                      onChange={(e) =>
                        setEvalParamValues((prev) => ({
                          ...prev,
                          [key]: e.target.value,
                        }))
                      }
                    />
                    <button
                      className="reset-one"
                      title={`既定 ${spec.default} に戻す`}
                      onClick={() =>
                        setEvalParamValues((prev) => ({
                          ...prev,
                          [key]: String(spec.default),
                        }))
                      }
                      disabled={evalParamValues[key] === String(spec.default)}
                    >
                      ↺
                    </button>
                  </span>
                </label>
              ))}
            </div>
          );
        })()}

        {evalError && <p className="error-note">{evalError}</p>}

        {evalReport &&
          (evalReport.n === 0 ? (
            <p className="empty-note">
              評価用の質問がありません。
              <code>python -m app.eval --seed</code> でサンプルを投入するか、
              <code>POST /eval-questions</code> で登録してください。
            </p>
          ) : (
            <>
              {/* 集計スコア（大きく表示） */}
              <div className="eval-score">
                <div className="eval-metric">
                  <span className="eval-metric-value">
                    {evalReport.hit_at_k.toFixed(3)}
                  </span>
                  <span className="eval-metric-label">
                    Hit@{evalReport.top_k}（上位{evalReport.top_k}件に正解が入った割合）
                  </span>
                </div>
                <div className="eval-metric">
                  <span className="eval-metric-value">
                    {evalReport.mrr.toFixed(3)}
                  </span>
                  <span className="eval-metric-label">
                    MRR（正解順位の逆数平均・1.0が満点）
                  </span>
                </div>
                <div className="eval-meta">
                  N={evalReport.n} ・ 手法=
                  {evalReport.retrievers ? evalReport.retrievers.join(",") : "既定"} ・
                  リランク=
                  {evalReport.rerank === null ? "既定" : evalReport.rerank ? "有効" : "無効"}
                  {evalReport.rrf_k != null && <> ・ rrf_k={evalReport.rrf_k}</>}
                  {evalReport.params &&
                    Object.entries(evalReport.params).map(([r, ps]) =>
                      Object.entries(ps).map(([k, v]) => (
                        <span key={`${r}.${k}`}>
                          {" · "}
                          {r}.{k}={v}
                        </span>
                      )),
                    )}
                </div>
              </div>

              {/* 1問ずつの結果。×の行が改善対象 */}
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>判定</th>
                      <th>順位</th>
                      <th>質問</th>
                      <th>正解</th>
                      <th>検索で引いた文書（上位順）</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evalReport.results.map((r, i) => (
                      <tr key={i} className={r.hit ? "" : "miss"}>
                        <td>{r.hit ? "○" : "×"}</td>
                        <td>{r.rank === null ? "圏外" : `${r.rank + 1}位`}</td>
                        <td className="preview">{r.question}</td>
                        <td>
                          <SourceLink source={r.expected_source} />
                        </td>
                        <td className="preview">
                          {r.retrieved.length === 0
                            ? "(なし)"
                            : r.retrieved.map((s, si) => (
                                <Fragment key={si}>
                                  {si > 0 && " / "}
                                  <SourceLink source={s} />
                                </Fragment>
                              ))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="hint">
                <strong>×</strong> の行は正解文書を上位{evalReport.top_k}件に拾えなかった質問。
                手法やリランクを変えて再検証し、Hit@k / MRR が上がるかで改良の効果を確かめる。
              </p>
            </>
          ))}
      </section>
    </div>
  );
}
