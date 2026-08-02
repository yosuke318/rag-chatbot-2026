"use client";

import { Fragment, useEffect, useRef, useState } from "react";

// ★型はバックエンドの OpenAPI スキーマから自動生成したものを使う★
//   再生成: npm run gen:types （backend が :8000 で起動している状態で）
//   手書きしないことで、BEの型を変えたらここで型エラーになりズレに気づける。
import type { components } from "./api-types";
// 描画から切り離した純ロジック（引用の切り分け・SSEの読み取り）は別ファイル。
// ストリームの境界やマーカーの対応付けは目視で試しにくいので、単体テストを付けてある。
import { citationHref, splitAnswer, type Citation } from "./citations";
import { readSSE } from "./sse";

type SearchStages = components["schemas"]["SearchResponse"];
type VerifyReport = components["schemas"]["VerifyReport"];
type ApiError = components["schemas"]["ErrorResponse"];
type RetrieverInfo = components["schemas"]["RetrieverInfo"];
type ParamSpec = components["schemas"]["ParamSpec"];

// 評価パネル用。api-types.ts は backend 起動下で `npm run gen:types` して再生成する
// ため、それまでの間はここで最小の型を持つ（再生成後は components["schemas"]["EvalReport"]
// に寄せられる）。
type EvalResult = {
  question: string;
  expected_source: string;
  // 正解チャンクに含まれるべき語句。値があれば★チャンク単位★で採点した設問
  // （null は従来どおり文書単位）。
  expected_text?: string | null;
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
  image: (
    <>
      質問と<strong>文書内の画像そのもの</strong>のベクトルがどれだけ近いか。
      voyage-multimodal-3 が画像とテキストを同じ空間に埋め込むので、
      言語化を挟まずに図表を直接引ける。
      <br />
      <br />
      当たるのは <code>IMAGE_INDEX_METHOD=multimodal</code> で索引した画像だけ。
      自動キャプション方式で運用しているときは常に空になり、
      図表は上の3手法（説明文のチャンク）の側で引っかかる。
    </>
  ),
};

// UI内部だけで使う型（APIには存在しない）
// question: 👍/👎 を送るとき評価対象を復元するため、bot回答に元の質問を持たせる。
//           これが入っている bot メッセージだけがフィードバック対象（エラーは対象外）。
// rating:   送信済みの評価。二重送信を防ぎ、選んだ側をハイライトする。
// citations: チャンク単位の根拠。回答本文の [n] と citations[n-1] が対応する。
type Message = {
  role: "user" | "bot";
  text: string;
  sources?: string[];
  citations?: Citation[];
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

// 回答本文の [1] [2] を、対応する根拠へジャンプするボタンに変える。
// 切り分けの規則（存在しない番号は素の文字のまま残す等）は splitAnswer 側にある。
function AnswerText({
  text,
  citations,
  onCite,
}: {
  text: string;
  citations: Citation[];
  onCite: (n: number) => void;
}) {
  return (
    <>
      {splitAnswer(text, citations).map((part, i) => {
        const cite = part.citation;
        if (!cite) return <Fragment key={i}>{part.text}</Fragment>;
        return (
          <button
            key={i}
            // type を明示しないと <form> 配下に置かれたとき submit になる
            type="button"
            className="cite-mark"
            onClick={() => onCite(cite.n)}
            title={`根拠 [${cite.n}] ${cite.source}`}
            // 表示は数字だけなので、読み上げには何のボタンかを補って伝える
            aria-label={`根拠 ${cite.n} を表示（${cite.source}）`}
          >
            {cite.n}
          </button>
        );
      })}
    </>
  );
}

/** そのプロジェクト配下のトピック候補を取ってくる（GET /topics?project=）。
 *
 * パネルごとに選んでいるプロジェクトが違うので、パネル単位で呼ぶ。
 * reloadKey は「登録して区分が増えたら取り直す」ための合図（値が変わると再取得）。
 */
function useTopics(project: string, reloadKey: number): string[] {
  const [topics, setTopics] = useState<string[]>([]);
  useEffect(() => {
    const p = project.trim();
    const query = p ? `?project=${encodeURIComponent(p)}` : "";
    // 選び直しが速いと応答が前後するので、古い結果は捨てる
    let current = true;
    fetch(`/api/backend/topics${query}`)
      .then((r) => r.json())
      .then((d) => {
        if (current) setTopics(d.topics ?? []);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [project, reloadKey]);
  return topics;
}

/** 選択肢に無い値（他パネルで打った新規の区分など）も候補に残す。 */
function withCurrent(options: string[], value: string): string[] {
  const v = value.trim();
  return v && !options.includes(v) ? [...options, v] : options;
}

type ScopeProps = {
  project: string;
  topic: string;
  projects: string[];
  topics: string[];
  onProject: (v: string) => void;
  onTopic: (v: string) => void;
};

/** 検索・質問・評価で使う区分の絞り込み。未選択（すべて）＝絞り込まない。
 *
 * ここは「既存の区分から選ぶ」場面なので select にしてある（打ち間違いで
 * 0件になるのを防ぐ）。新しい区分を作れるのは登録側だけ（ScopeInput）。
 * プロジェクトを変えたらトピックは外す: 別プロジェクトのトピックが残ると
 * 存在しない組み合わせになり、黙って0件になるため。
 */
function ScopeSelect({
  project,
  topic,
  projects,
  topics,
  onProject,
  onTopic,
}: ScopeProps) {
  return (
    <div className="scope-row">
      <label className="scope-field">
        <span className="scope-label">プロジェクト（任意）</span>
        <select
          value={project}
          onChange={(e) => {
            onProject(e.target.value);
            onTopic("");
          }}
        >
          <option value="">すべて</option>
          {withCurrent(projects, project).map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </label>
      <label className="scope-field">
        <span className="scope-label">トピック（任意）</span>
        <select value={topic} onChange={(e) => onTopic(e.target.value)}>
          <option value="">すべて</option>
          {withCurrent(topics, topic).map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

/** 登録側の区分入力。既存の候補を出しつつ、新しい区分も打てる（datalist）。
 *
 * 登録は「まだ無いプロジェクトを作る」入口でもあるので select にはできない。
 * idPrefix: datalist の id はページ内で一意である必要があるため、置く場所ごとに変える。
 */
function ScopeInput({
  project,
  topic,
  projects,
  topics,
  onProject,
  onTopic,
  idPrefix,
}: ScopeProps & { idPrefix: string }) {
  return (
    <div className="scope-row">
      <input
        list={`${idPrefix}-projects`}
        placeholder="プロジェクト（任意）"
        value={project}
        onChange={(e) => onProject(e.target.value)}
      />
      <datalist id={`${idPrefix}-projects`}>
        {projects.map((p) => (
          <option key={p} value={p} />
        ))}
      </datalist>
      <input
        list={`${idPrefix}-topics`}
        placeholder="トピック（任意）"
        value={topic}
        onChange={(e) => onTopic(e.target.value)}
      />
      <datalist id={`${idPrefix}-topics`}>
        {topics.map((t) => (
          <option key={t} value={t} />
        ))}
      </datalist>
    </div>
  );
}

/** 左サイドバーのタブ。順番がそのまま画面の並びになる。
 *
 * ①〜⑤ の番号は「文書を入れる → 検索を見る → 質問する → 数字で測る」という
 * 想定の順路。番号を振っておくと、説明文から他タブを指すときに短く書ける。
 */
const TABS = [
  { id: "ingest", label: "① 文書を登録", hint: "/ingest-file" },
  { id: "search", label: "② 検索の内訳", hint: "/search" },
  { id: "chat", label: "③ 質問する", hint: "/chat" },
  { id: "eval", label: "④ 評価する", hint: "/eval" },
  { id: "verify", label: "⑤ 保管質問の検証", hint: "/verify" },
] as const;

type TabId = (typeof TABS)[number]["id"];

/** 機能を切り替える左サイドバー。
 *
 * 1ページに縦積みしていた頃は、目的の機能まで延々スクロールする必要があった。
 * 選択中のタブ以外は描画しないが、★stateはページ直下に置いたまま★なので
 * 切り替えても検索結果・チャット履歴・評価レポートは消えない。
 */
function Sidebar({
  tab,
  onTab,
}: {
  tab: TabId;
  onTab: (id: TabId) => void;
}) {
  return (
    <nav className="sidebar" aria-label="機能">
      {/* ページの見出しはここ1つ。本文側は各機能の h2 から始まる */}
      <div className="sidebar-brand">
        <h1>RAG Inspector</h1>
        <span>RAG検証ラボ</span>
      </div>
      <ul className="sidebar-tabs">
        {TABS.map((t) => (
          <li key={t.id}>
            <button
              type="button"
              className={t.id === tab ? "sidebar-tab active" : "sidebar-tab"}
              aria-current={t.id === tab ? "page" : undefined}
              onClick={() => onTab(t.id)}
            >
              <span className="sidebar-tab-label">{t.label}</span>
              <code className="sidebar-tab-hint">{t.hint}</code>
            </button>
          </li>
        ))}
      </ul>
    </nav>
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
  // --- 表示中の機能（左サイドバーのタブ）---------------------------------
  // ★stateはすべてこのコンポーネント直下に置く★
  // タブ切り替えで消えるのは描画だけなので、検索結果・チャット履歴・評価
  // レポートは戻ってきたときにそのまま残る。
  const [tab, setTab] = useState<TabId>("ingest");

  // --- 区分（project / topic）の候補 -------------------------------------
  // プロジェクトはページで1つ持ち、トピックはパネルごとに「選択中のプロジェクト
  // 配下だけ」を引く（useTopics）。scopeVersion を増やすと両方を取り直す
  // ＝登録で新しい区分が増えたら、他パネルのセレクタにもすぐ出る。
  const [projects, setProjects] = useState<string[]>([]);
  const [scopeVersion, setScopeVersion] = useState(0);

  useEffect(() => {
    fetch("/api/backend/projects")
      .then((r) => r.json())
      .then((d) => setProjects(d.projects ?? []))
      .catch(() => {});
  }, [scopeVersion]);

  // --- 取り込みパネル（/ingest-file = 書き込みフロー）---
  // 文書の区分。登録するファイルすべてに付く。
  // 空欄は送らない＝区分なし(NULL)の共通文書として登録。
  const [docProject, setDocProject] = useState("");
  const [docTopic, setDocTopic] = useState("");
  const docTopics = useTopics(docProject, scopeVersion);
  const [ingestStatus, setIngestStatus] = useState("");
  // 「区分だけ登録」の結果表示（文書の取り込み結果とは別に出す）
  const [scopeStatus, setScopeStatus] = useState("");
  // ファイルのドラッグ&ドロップ登録（/ingest-file）
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  // D&D/選択したファイルは即アップロードせず、一旦ここに溜めて（＝ステージ）
  // 「登録する」で確定、「キャンセル」で取り消せるようにする（誤ドロップ対策）。
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // --- 検索パネル（/search = 検索の内訳。Claudeを呼ばない）---
  const [searchQ, setSearchQ] = useState("");
  // 検索対象の区分。未選択＝全文書から探す
  const [searchProject, setSearchProject] = useState("");
  const [searchTopic, setSearchTopic] = useState("");
  const searchTopics = useTopics(searchProject, scopeVersion);
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
  // 回答本文の [n] を押したときに光らせる根拠。"メッセージ番号:引用番号" で持つ
  // （同じ引用番号が別の回答にもあるため、メッセージまで込みで一意にする）。
  const [activeCite, setActiveCite] = useState<string | null>(null);
  // 続きの質問で履歴を効かせるための会話ID。null = 次の質問で新しい会話を始める。
  const [conversationId, setConversationId] = useState<number | null>(null);
  // 回答の根拠にする文書の区分。未選択＝全文書から探す
  const [chatProject, setChatProject] = useState("");
  const [chatTopic, setChatTopic] = useState("");
  const chatTopics = useTopics(chatProject, scopeVersion);

  // --- 評価パネル（/eval = 質問集で Hit@k / MRR を測る）---
  const [evalSelected, setEvalSelected] = useState<string[]>([]);
  const [evalRerank, setEvalRerank] = useState(false);
  const [evalProject, setEvalProject] = useState("");
  const [evalTopic, setEvalTopic] = useState("");
  const evalTopics = useTopics(evalProject, scopeVersion);

  // --- 保管質問の検証（⑤ /verify = ②で検索した質問をまとめて引き直す）---
  // ★評価(④)とは別タブ・別state★
  //   扱うテーブルが違う（saved_questions / eval_questions）うえ、正解ラベルの
  //   要否も出力も別物なので機能として分けてある。区分セレクタを④と共用すると、
  //   タブを跨いだ相手の選択が見えないまま結果が変わることになるため独立させる。
  const [verifyProject, setVerifyProject] = useState("");
  const [verifyTopic, setVerifyTopic] = useState("");
  const verifyTopics = useTopics(verifyProject, scopeVersion);
  const [verifyReport, setVerifyReport] = useState<VerifyReport | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [verifyError, setVerifyError] = useState("");
  // その区分に何件保管されているか（検証は質問数だけ検索するので先に見せる）。
  // 件数が変わるのは「②で検索したとき」だけなので、そこで savedVersion を上げて
  // 取り直す。検証(/verify)は件数を変えないので、その結果には反応させない。
  const [savedCount, setSavedCount] = useState<number | null>(null);
  const [savedVersion, setSavedVersion] = useState(0);

  useEffect(() => {
    const params = new URLSearchParams();
    if (verifyProject.trim()) params.set("project", verifyProject.trim());
    if (verifyTopic.trim()) params.set("topic", verifyTopic.trim());
    let current = true;
    fetch(`/api/backend/saved-questions?${params}`)
      .then((r) => r.json())
      .then((d) => {
        if (current) setSavedCount(d.questions?.length ?? 0);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [verifyProject, verifyTopic, savedVersion]);

  async function runVerify() {
    if (verifying) return;
    setVerifying(true);
    setVerifyError("");
    try {
      const params = new URLSearchParams({ top_k: "4" });
      if (verifyProject.trim()) params.set("project", verifyProject.trim());
      if (verifyTopic.trim()) params.set("topic", verifyTopic.trim());
      const res = await fetch(`/api/backend/verify?${params}`);
      const err = await errorMessage(res);
      if (err) {
        setVerifyError(err);
        setVerifyReport(null);
        return;
      }
      setVerifyReport(await res.json());
    } catch (e) {
      setVerifyReport(null);
      setVerifyError(`通信に失敗しました: ${String(e)}`);
    } finally {
      setVerifying(false);
    }
  }
  const [evalReport, setEvalReport] = useState<EvalReport | null>(null);
  const [evalRunning, setEvalRunning] = useState(false);
  const [evalError, setEvalError] = useState("");
  // 評価用の数値パラメータ。②検索と同じキー（rrf_k / trgm_min_similarity / bm25_k1 / bm25_b）
  const [evalParamValues, setEvalParamValues] = useState<Record<string, string>>({});

  // 評価用の質問を登録するフォーム（POST /eval-questions）
  const [newQ, setNewQ] = useState("");
  const [newExpected, setNewExpected] = useState("");
  // 正解チャンクに必ず含まれる語句（任意）。入れるとチャンク単位の判定になる
  const [newExpectedText, setNewExpectedText] = useState("");
  const [newQProject, setNewQProject] = useState("");
  const [newQTopic, setNewQTopic] = useState("");
  const newQTopics = useTopics(newQProject, scopeVersion);
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
          // 空欄は送らない（＝文書単位で判定する従来どおりの質問になる）
          expected_text: newExpectedText.trim() || null,
          // 空欄は送らない（＝共通の質問として登録）
          project: newQProject.trim() || null,
          topic: newQTopic.trim() || null,
          note: newQNote.trim() || null,
        }),
      });
      // 空入力なら backend が 400 を返す。その message をそのまま表示する
      const err = await errorMessage(res);
      if (err) {
        setAddQStatus(err);
        return;
      }
      setAddQStatus(
        `「${newQ}」を登録しました（正解: ${newExpected}` +
          (newExpectedText.trim()
            ? ` / 語句「${newExpectedText.trim()}」を含むチャンクで判定）`
            : "・文書単位で判定）"),
      );
      setScopeVersion((v) => v + 1); // 新しい区分が増えていれば他パネルの候補にも出す
      setNewQ("");
      setNewExpected("");
      setNewExpectedText("");
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
      if (evalProject.trim()) params.set("project", evalProject.trim());
      if (evalTopic.trim()) params.set("topic", evalTopic.trim());
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

  /** 入力欄の区分だけをマスタに登録する（文書は入れない）。
   *
   * ★文書が無くても区分を作れるようにするための入口★
   * 以前は選択肢が「文書か質問に実在する値」だったので、先に区分だけ用意して
   * おくことができなかった。トピックだけ打った場合は、その上のプロジェクト
   * （空ならプロジェクトなし）の配下に作る。
   */
  async function createScope() {
    const project = docProject.trim();
    const topic = docTopic.trim();
    if (!project && !topic) return;
    setScopeStatus("登録中…");
    try {
      // トピックを作るときは親のプロジェクトも一緒に送るので、
      // プロジェクト単独の登録が要るのは「プロジェクトだけ打った」ときだけ。
      const res = topic
        ? await fetch("/api/backend/topics", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ name: topic, project: project || null }),
          })
        : await fetch("/api/backend/projects", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ name: project }),
          });
      const err = await errorMessage(res);
      if (err) {
        setScopeStatus(err);
        return;
      }
      const data = await res.json();
      const label = topic ? `${project || "（プロジェクトなし）"} / ${topic}` : project;
      // created=false は「既にあった」＝エラーではない
      setScopeStatus(
        data.created ? `区分「${label}」を作りました` : `区分「${label}」は既にあります`,
      );
      setScopeVersion((v) => v + 1); // 各パネルのセレクタに出す
    } catch (e) {
      setScopeStatus(`エラー: ${String(e)}`);
    }
  }

  /** D&D/選択したファイルをステージに追加する（即アップロードはしない）。
   * 同じファイル（名前・サイズ・更新日時が一致）は重複追加しない。 */
  function addPendingFiles(files: File[]) {
    if (files.length === 0) return;
    setPendingFiles((prev) => {
      const key = (f: File) => `${f.name}:${f.size}:${f.lastModified}`;
      const seen = new Set(prev.map(key));
      const added = files.filter((f) => !seen.has(key(f)));
      return [...prev, ...added];
    });
  }

  function removePendingFile(index: number) {
    setPendingFiles((prev) => prev.filter((_, i) => i !== index));
  }

  /** ステージ中のファイルを順に取り込む（「登録する」で呼ぶ）。
   *
   * FormData で送るので content-type は指定しない（ブラウザが multipart の
   * boundary 付きで自動設定する。プロキシはそれを素通しする）。
   * Voyageの埋め込みAPIを各ファイルで呼ぶため、レート制限を避けて逐次実行する。
   * 成功したファイルはステージから外し、失敗したものだけ残して再試行できるようにする。
   */
  async function uploadPending() {
    if (uploading || pendingFiles.length === 0) return;
    setUploading(true);
    const results: string[] = [];
    const failed: File[] = [];
    for (const file of pendingFiles) {
      setIngestStatus(
        [...results, `「${file.name}」を取り込み中…`].join("\n"),
      );
      try {
        const fd = new FormData();
        fd.append("file", file);
        // 空欄は送らない（FormDataは空文字も送ってしまうため明示的に分岐する）
        if (docProject.trim()) fd.append("project", docProject.trim());
        if (docTopic.trim()) fd.append("topic", docTopic.trim());
        const res = await fetch("/api/backend/ingest-file", {
          method: "POST",
          body: fd,
        });
        const err = await errorMessage(res);
        if (err) {
          // 改行はまとめて1行にして一覧を読みやすく保つ
          results.push(`✗ ${file.name}: ${err.replace(/\n/g, " / ")}`);
          failed.push(file);
          continue;
        }
        const data = await res.json();
        // 文書内画像（PDFはページ画像、xlsx/pptxは貼られた図）を取り出せた枚数。
        // 本文が同じでスキップした場合も保存されるので、両方の分岐に付ける。
        const images = data.images_stored
          ? `・画像${data.images_stored}枚`
          : "";
        if (data.skipped) {
          // 内容が同じ＝埋め込みをやり直していない（差分検知）
          results.push(
            `✓ ${file.name}: 内容に変更なし（${data.chunks_created}チャンクのまま${images}）`,
          );
        } else {
          const note = data.replaced ? "（同名を置き換え）" : "";
          results.push(
            `✓ ${file.name}: ${data.chunks_created}チャンク${images}で登録${note}`,
          );
        }
      } catch (e) {
        results.push(`✗ ${file.name}: ${String(e)}`);
        failed.push(file);
      }
      setIngestStatus(results.join("\n"));
    }
    setPendingFiles(failed); // 成功分は消し、失敗分だけ残す
    if (failed.length < pendingFiles.length) {
      setScopeVersion((v) => v + 1); // 1件でも入れば区分が増えている可能性がある
    }
    setUploading(false);
  }

  async function runSearch() {
    const q = searchQ.trim();
    if (!q || searching) return;
    setSearching(true);
    setSearchError("");
    try {
      const params = new URLSearchParams({ q });
      if (selected.length > 0) params.set("retrievers", selected.join(","));
      // 区分は未選択なら送らない（＝全文書が対象。空文字を送ると空文字での絞り込み）
      if (searchProject.trim()) params.set("project", searchProject.trim());
      if (searchTopic.trim()) params.set("topic", searchTopic.trim());
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
      // 検索が通ると質問が保管されるので、⑤の件数を取り直す
      setSavedVersion((v) => v + 1);
    } catch (e) {
      setStages(null);
      setSearchError(`通信に失敗しました: ${String(e)}`);
    } finally {
      setSearching(false);
    }
  }

  /** 最後のメッセージ（＝生成中の回答）だけを書き換える。 */
  function patchLastMessage(patch: Partial<Message>) {
    setMessages((m) =>
      m.map((x, i) => (i === m.length - 1 ? { ...x, ...patch } : x)),
    );
  }

  async function ask() {
    const q = question.trim();
    if (!q || loading) return;
    // 質問と「これから埋まる空の回答」を同時に置く。以降 delta が届くたびに
    // 末尾（＝この空の回答）を書き換えていく。
    // question を持たせておくと、この回答に 👍/👎 を付けられる（送信時に復元する）。
    setMessages((m) => [
      ...m,
      { role: "user", text: q },
      { role: "bot", text: "", question: q },
    ]);
    setQuestion("");
    setLoading(true);
    try {
      const res = await fetch("/api/backend/chat/stream", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // conversation_id を渡すと直近の履歴を踏まえて答える（null=新しい会話）
        // 区分を選んでいれば、その区分の文書だけを根拠にする（未選択は null=全体）
        body: JSON.stringify({
          question: q,
          conversation_id: conversationId,
          project: chatProject.trim() || null,
          topic: chatTopic.trim() || null,
        }),
      });
      const err = await errorMessage(res);
      if (err) {
        // Anthropicキー未設定などをチャット欄にそのまま出す
        patchLastMessage({ text: err, question: undefined });
        return;
      }
      await readStream(res);
    } catch (e) {
      patchLastMessage({ text: `エラー: ${String(e)}`, question: undefined });
    } finally {
      setLoading(false);
    }
  }

  /** SSE(text/event-stream)を読み進め、届いたイベントを回答へ反映する。 */
  async function readStream(res: Response) {
    if (!res.body) throw new Error("ストリームを読み取れませんでした");
    let answer = "";

    await readSSE(res.body, (name, data) => {
      if (name === "meta") {
        // 根拠は生成より先に確定するので、本文が届く前に出せる
        setConversationId(data.conversation_id);
        patchLastMessage({ sources: data.sources, citations: data.citations });
      } else if (name === "delta") {
        answer += data.text;
        patchLastMessage({ text: answer });
      } else if (name === "error") {
        // 生成中の失敗。HTTPは200で流れているのでイベントで受け取る
        const message = data.hint ? `${data.message}\n${data.hint}` : data.message;
        patchLastMessage({
          text: answer ? `${answer}\n\n${message}` : message,
          question: undefined, // 失敗した回答は 👍/👎 の対象にしない
        });
      }
    });
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
    <div className="layout">
      <Sidebar tab={tab} onTab={setTab} />

      {/* 選択中のタブの中身。stateはこの外（Home直下）にあるので、
          切り替えで描画が消えても入力や結果は保持される。 */}
      <main className="container">
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
              <code>ANTHROPIC_API_KEY</code>（生成）… <b>回答生成のみ</b>。
              検索の内訳を見るだけなら不要（リランクも既定はVoyageの専用APIなので不要）
            </li>
          </ul>
        </div>

      {/* 書き込みフロー: text → chunk → embed → pgvector */}
      {tab === "ingest" && (
      <section className="panel">
        <h2>① 文書を登録（/ingest-file・Voyageキー必要）</h2>

        {/* 文書の区分。登録するファイルすべてに付くので、
            ドロップゾーンの中ではなくパネルの先頭に置く。 */}
        <ScopeInput
          idPrefix="doc"
          project={docProject}
          topic={docTopic}
          projects={projects}
          topics={docTopics}
          onProject={setDocProject}
          onTopic={setDocTopic}
        />
        <p className="hint">
          区分は下で<strong>登録するファイルすべて</strong>に付きます（空欄なら区分なし）。
          既存の区分は入力欄から選べます。新しい名前を打てばその区分が作られます。
        </p>
        {/* 文書を入れずに区分だけ用意する入口。
            「まず部署を作ってから資料を集める」という順で使えるようにするため。 */}
        <div className="verify-controls">
          <button
            onClick={createScope}
            disabled={!docProject.trim() && !docTopic.trim()}
          >
            区分だけ登録する
          </button>
          <span className="hint">
            <Tip label="文書が無くても区分を作れます">
              上の入力欄に打った区分を<strong>マスタにだけ</strong>登録します。
              文書やファイルは登録しません。作った区分は各パネルのセレクタに出るので、
              「先に部署を作っておいて、資料は後から入れる」という順で使えます。
            </Tip>
          </span>
        </div>
        {scopeStatus && <p className="hint ingest-status">{scopeStatus}</p>}

        {/* ファイルのドラッグ&ドロップ登録（/ingest-file）。
            D&D/クリックでファイルをステージに追加し、「登録する」で確定する。
            誤ってドロップしても「キャンセル」や個別×で取り消せる。複数可。

            本文を貼り付けて登録するフォーム（POST /ingest）は画面から外した。
            実文書はファイルで入るのが常で、貼り付けは使われないため。
            APIとしての /ingest は残してある（seed や外部スクリプトが使う）。 */}
        <div className="dz-divider">ファイルを登録</div>
        <div
          className={`dropzone${dragging ? " dragover" : ""}${uploading ? " busy" : ""}`}
          onClick={() => !uploading && fileInputRef.current?.click()}
          onKeyDown={(e) => {
            // role="button" 相当のキー操作。Enter/Space でファイル選択を開く。
            // Space は既定のスクロールを止める。
            if ((e.key === "Enter" || e.key === " ") && !uploading) {
              e.preventDefault();
              fileInputRef.current?.click();
            }
          }}
          onDragOver={(e) => {
            e.preventDefault();
            if (!uploading) setDragging(true);
          }}
          onDragLeave={(e) => {
            e.preventDefault();
            setDragging(false);
          }}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (!uploading) addPendingFiles(Array.from(e.dataTransfer.files));
          }}
          role="button"
          tabIndex={0}
          aria-disabled={uploading}
        >
          ここにファイルをドラッグ&ドロップ
          <br />
          <span className="dz-sub">
            またはクリックして選択（PDF / XLSX / PPTX / テキスト・複数可）
          </span>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".txt,.md,.csv,.tsv,.json,.log,.pdf,.xlsx,.pptx"
          hidden
          onChange={(e) => {
            if (e.target.files) addPendingFiles(Array.from(e.target.files));
            e.target.value = ""; // 同じファイルを連続で選べるようにリセット
          }}
        />

        {/* ステージ中のファイル一覧。登録前に個別に外せる。 */}
        {pendingFiles.length > 0 && (
          <div className="dz-pending">
            <ul className="dz-file-list">
              {pendingFiles.map((f, i) => (
                <li key={`${f.name}:${f.size}:${f.lastModified}`}>
                  <span className="dz-file-name">{f.name}</span>
                  <span className="dz-file-size">
                    {(f.size / 1024).toFixed(0)} KB
                  </span>
                  <button
                    className="dz-file-remove"
                    onClick={() => removePendingFile(i)}
                    disabled={uploading}
                    title="この1件を外す"
                    aria-label={`${f.name} を外す`}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
            <div className="dz-actions">
              <button onClick={uploadPending} disabled={uploading}>
                {uploading
                  ? "取り込み中…"
                  : `登録する（${pendingFiles.length}件）`}
              </button>
              <button
                className="dz-cancel"
                onClick={() => setPendingFiles([])}
                disabled={uploading}
              >
                キャンセル
              </button>
            </div>
          </div>
        )}
        {ingestStatus && <p className="hint ingest-status">{ingestStatus}</p>}
      </section>
      )}

      {/* 検索の内訳: Claudeを呼ばないのでAnthropicキー不要 */}
      {tab === "search" && (
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
            この上位チャンクが、そのまま <strong>③ 質問する</strong> の回答生成で根拠として使われる。
            <br />
            <br />
            ここで検索した質問は、区分と一緒に自動で保管される。まとめて引き直すのは
            <strong>⑤ 保管質問の検証</strong>。
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

        {/* 検索対象の区分。BM25の統計（IDF）もこの範囲で計算される */}
        <ScopeSelect
          project={searchProject}
          topic={searchTopic}
          projects={projects}
          topics={searchTopics}
          onProject={setSearchProject}
          onTopic={setSearchTopic}
        />
        <p className="hint">
          区分を選ぶと<strong>その区分の文書だけ</strong>が検索対象になります
          （「すべて」なら絞り込みなし）。
        </p>

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
      )}

      {/* 質問フロー: question → hybrid_search → rerank → Claude */}
      {tab === "chat" && (
      <section className="panel">
        <h2>③ 質問する（/chat/stream・Voyage + Anthropicキー必要）</h2>
        {/* 会話は続きものとして扱われる（直近のやり取りが回答生成に載る）。
            話題を変えるときは新しい会話にすると、前の話に引きずられない。 */}
        <div className="conversation-bar">
          {conversationId === null
            ? "次の質問から新しい会話を始めます"
            : `会話 #${conversationId}（続きの質問は履歴を踏まえて回答します）`}
          <button
            className="new-conversation"
            onClick={() => {
              setConversationId(null);
              setMessages([]);
            }}
            disabled={loading || (conversationId === null && messages.length === 0)}
          >
            新しい会話
          </button>
        </div>

        {/* 回答の根拠にする文書の区分。②と同じ絞り込みが検索に効く */}
        <ScopeSelect
          project={chatProject}
          topic={chatTopic}
          projects={projects}
          topics={chatTopics}
          onProject={setChatProject}
          onTopic={setChatTopic}
        />
        <p className="hint">
          区分を選ぶと<strong>その区分の文書だけ</strong>を根拠に回答します
          （「すべて」なら全文書から探します）。
        </p>

        <div className="messages">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.citations && m.citations.length > 0 ? (
                <AnswerText
                  text={m.text}
                  citations={m.citations}
                  onCite={(n) => setActiveCite(`${i}:${n}`)}
                />
              ) : (
                m.text
              )}
              {/* チャンク単位の根拠。回答中の [n] と番号で対応する */}
              {m.citations && m.citations.length > 0 && (
                <div className="citations">
                  <div className="citations-head">根拠にしたチャンク</div>
                  {m.citations.map((c) => (
                    <div
                      key={c.n}
                      className={`citation${
                        activeCite === `${i}:${c.n}` ? " citation-on" : ""
                      }`}
                    >
                      <div className="citation-head">
                        <span className="cite-n">[{c.n}]</span>
                        {c.file_url ? (
                          <a
                            className="source-link"
                            href={citationHref(c.file_url)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {c.source}
                          </a>
                        ) : (
                          <SourceLink source={c.source} />
                        )}
                        {c.image_label && (
                          <span className="cite-image-label">
                            図: {c.image_label}
                          </span>
                        )}
                        <span className="cite-id">chunk #{c.chunk_id}</span>
                      </div>
                      {/* 根拠が図表なら、回答生成に渡したのと同じ画像をそのまま見せる。
                          「この図のここが根拠」を利用者が自分の目で確かめられる */}
                      {c.image_url ? (
                        <a
                          href={citationHref(c.image_url)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {/* 原本画像。next/image は最適化サーバを挟むので使わない。
                              ★遅延読み込みにしない★ 1回答で最大4枚しか出ないうえ、
                              これは回答の根拠そのもの＝すぐ見たいもの。加えて
                              読み込み前は高さ0に潰れるため、遅延させると画面内に
                              入らず永久に読み込まれないことがある。 */}
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img
                            className="cite-image"
                            src={citationHref(c.image_url)}
                            alt={c.image_label ?? c.source}
                          />
                        </a>
                      ) : (
                        <div className="cite-preview">{c.preview}</div>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {/* 引用が付かない回答（エラー等）は従来どおり出典名だけ出す */}
              {!m.citations?.length && m.sources && m.sources.length > 0 && (
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
      )}

      {/* 評価フロー: 質問集(eval_questions) → 各問を検索 → Hit@k / MRR を集計 */}
      {tab === "eval" && (
      <section className="panel">
        <h2>
          <Tip label="④ 評価する">
            登録済みの<strong>質問集（正解ラベル付き）</strong>を一気に検索して、
            <strong>どれだけ正解文書を上位で拾えたか</strong>を集計する。
            <br />
            <br />
            <strong>② 検索の内訳</strong> が「1問を深く見る」のに対し、④ は
            「質問集<strong>全体</strong>で当たるか」を見る。
            手法やリランクを変えて<strong>数字が上がるか下がるか</strong>で改良の効果を判定できる。
            <br />
            <br />
            質問はプロジェクト・トピックごとに分けて登録できる（<code>POST /eval-questions</code>）。
            まだ無ければ <code>python -m app.eval --seed</code> でサンプルを投入。
            <br />
            <br />
            正解ラベルを用意する前に並びだけ見たいときは <strong>⑤ 保管質問の検証</strong>。
            あちらは②で貯まった質問を採点せずに一覧する。
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
          <input
            placeholder="正解チャンクに含まれる語句（任意・例: 1日2時間を超える場合）"
            value={newExpectedText}
            onChange={(e) => setNewExpectedText(e.target.value)}
          />
          <p className="hint">
            語句を入れると<strong>チャンク単位</strong>で採点する（その語句を含むチャンクを
            引けたときだけ正解）。空欄なら<strong>文書単位</strong>＝その文書のどのチャンクでも
            正解になり、分割やcontextualの改良は<strong>数字に出ない</strong>。
          </p>
          <ScopeInput
            idPrefix="newq"
            project={newQProject}
            topic={newQTopic}
            projects={projects}
            topics={newQTopics}
            onProject={setNewQProject}
            onTopic={setNewQTopic}
          />
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
          <ScopeSelect
            project={evalProject}
            topic={evalTopic}
            projects={projects}
            topics={evalTopics}
            onProject={setEvalProject}
            onTopic={setEvalTopic}
          />
          <button onClick={runEval} disabled={evalRunning || evalSelected.length === 0}>
            {evalRunning ? "評価中…" : "評価する"}
          </button>
        </div>
        <p className="hint">
          区分を選ぶと<strong>その区分の質問だけ</strong>で評価し、
          <strong>検索対象の文書も同じ区分</strong>に絞ります（「すべて」なら全件）。
        </p>

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
              リランク（既定: Voyage rerank-2）
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
                          {/* 語句がある行はチャンク単位で採点した設問。粒度が混ざった
                              質問集で「○」の重みを読み違えないよう明示する */}
                          {r.expected_text ? (
                            <div className="hint">
                              チャンク単位「{r.expected_text}」
                            </div>
                          ) : (
                            <div className="hint">文書単位</div>
                          )}
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
      )}

      {/* 保管質問の検証フロー: saved_questions → 各問を検索 → 上位k件を一覧
          ★④の評価とは別物★ あちらは eval_questions（正解ラベル必須）を数値で
          採点する。こちらは②の検索で自動的に貯まった質問を、正解ラベル無しで
          「今の設定だと何が上位に来るか」目視で確かめるための道具。 */}
      {tab === "verify" && (
      <section className="panel">
        <h2>
          <Tip label="⑤ 保管質問を検証する">
            ② で検索すると、そのときの<strong>プロジェクト・トピックと一緒に質問が
            自動で保管</strong>されます（同じ区分の同じ質問は重ねません）。
            <br />
            <br />
            ここでは保管済みの質問を<strong>まとめて引き直し</strong>、各質問の上位4件と
            RRFスコアを一覧で確認できます。
            <br />
            <br />
            ④ の評価とは<strong>別のデータ</strong>を見ています。④ は正解ラベル付きの
            質問集（<code>eval_questions</code>）を Hit@k / MRR で採点するもの。
            こちらは正解ラベルを持たない実際に聞かれた質問（<code>saved_questions</code>）
            なので○×は付きません。正解を用意する前でも並びを確かめられるのが利点です。
          </Tip>
          （/verify・Voyageキー必要）
        </h2>

        <div className="eval-controls">
          <ScopeSelect
            project={verifyProject}
            topic={verifyTopic}
            projects={projects}
            topics={verifyTopics}
            onProject={setVerifyProject}
            onTopic={setVerifyTopic}
          />
          <button onClick={runVerify} disabled={verifying || savedCount === 0}>
            {verifying ? "検証中…" : "保管質問を検証する"}
          </button>
        </div>
        <p className="hint">
          区分を選ぶと<strong>その区分の質問だけ</strong>を、
          <strong>同じ区分の文書</strong>に対して引き直します（「すべて」なら全件）。
          {savedCount === null
            ? ""
            : savedCount === 0
              ? "　この区分に保管された質問はまだありません（② 検索の内訳 で検索すると貯まります）"
              : `　この区分に ${savedCount} 件の質問が保管されています`}
        </p>

        {verifyError && <p className="error-note">{verifyError}</p>}

        {verifyReport && verifyReport.n > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>質問</th>
                  <th>順位</th>
                  <th>RRFスコア</th>
                  <th>出典</th>
                  <th>内容</th>
                </tr>
              </thead>
              <tbody>
                {verifyReport.results.map((r, ri) =>
                  r.fused.length === 0 ? (
                    <tr key={ri}>
                      <td className="preview">{r.question}</td>
                      <td colSpan={4} className="miss">
                        （ヒットなし）
                      </td>
                    </tr>
                  ) : (
                    r.fused.map((f, fi) => (
                      <tr key={`${ri}:${f.id}`}>
                        {/* 質問は1問につき1回だけ出し、下の行は上位2位以下 */}
                        {fi === 0 ? (
                          <td className="preview" rowSpan={r.fused.length}>
                            {r.question}
                          </td>
                        ) : null}
                        <td>{f.rank + 1}位</td>
                        <td>{f.score}</td>
                        <td>
                          <SourceLink source={f.source} />
                        </td>
                        <td className="preview">{f.preview}</td>
                      </tr>
                    ))
                  ),
                )}
              </tbody>
            </table>
          </div>
        )}
        {verifyReport && verifyReport.n === 0 && (
          <p className="empty-note">
            この区分に保管された質問がありません。
            <strong>② 検索の内訳</strong> で検索すると貯まります。
          </p>
        )}
      </section>
      )}
      </main>
    </div>
  );
}
