"use client";

import { AutoComplete, Modal, Select } from "antd";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import { LIST_HEIGHT } from "./antd";

// ★型はバックエンドの OpenAPI スキーマから自動生成したものを使う★
//   再生成: npm run gen:types （backend が :8000 で起動している状態で）
//   手書きしないことで、BEの型を変えたらここで型エラーになりズレに気づける。
import type { components } from "./api-types";
// 描画から切り離した純ロジック（引用の切り分け・SSEの読み取り）は別ファイル。
// ストリームの境界やマーカーの対応付けは目視で試しにくいので、単体テストを付けてある。
import { citationHref, splitAnswer, type Citation } from "./citations";
import { readSSE } from "./sse";
import {
  applyTheme,
  readThemeChoice,
  THEME_STORAGE_KEY,
  type ThemeChoice,
} from "./theme";

type SearchStages = components["schemas"]["SearchResponse"];
type VerifyReport = components["schemas"]["VerifyReport"];
type ApiError = components["schemas"]["ErrorResponse"];
type RetrieverInfo = components["schemas"]["RetrieverInfo"];
type RetrievalMeta = components["schemas"]["RetrievalMeta"];
type ParamSpec = components["schemas"]["ParamSpec"];
type DocumentSummary = components["schemas"]["DocumentSummary"];

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
//
// ★下4つは画面に出さない★
//   👍/👎 と一緒に「どういう条件で出た回答か」をサーバへ返すためだけに持つ。
//   /chat/stream の meta（検索条件）と done（回答ID・所要時間）で届いたものを
//   そのまま抱えておき、フィードバック送信時に添える。回答ごとに違う値なので、
//   画面の state ではなくメッセージに持たせないと、あとから押した👎に
//   別の回答の条件が付いてしまう。
type Message = {
  role: "user" | "bot";
  text: string;
  sources?: string[];
  citations?: Citation[];
  question?: string;
  rating?: 1 | -1;
  conversationId?: number;
  messageId?: number;
  retrieval?: RetrievalMeta;
  latencyMs?: number;
  // 質問したときに選んでいた区分。👍/👎 に添えて送るために回答ごとに抱える
  // （送信時に今のセレクタを読むと、聞いた後で切り替えた区分が記録される）。
  project?: string;
  topic?: string;
  // 記録した👍/👎のID。👎の理由を後から足す宛先になる（理由を選ぶまで送信を
  // 待たない代わりに、記録済みの行を指せるようにしておく）。
  feedbackId?: number;
  reason?: string;
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

/** 👎の理由の選択肢を取ってくる（GET /feedback/reasons）。
 *
 * 画面に文言を焼かないのは、記録される値と表示される文言を同じものに保つため。
 * 取れなければ空配列＝理由を聞く欄が出ないだけで、👎そのものは記録できる。
 */
function useFeedbackReasons(): string[] {
  const [reasons, setReasons] = useState<string[]>([]);
  useEffect(() => {
    fetch("/api/backend/feedback/reasons")
      .then((r) => r.json())
      .then((d) => setReasons(d.reasons ?? []))
      .catch(() => {});
  }, []);
  return reasons;
}

/** 👎を押した直後に理由を聞く欄。★押した時点で👎は記録済み★
 *
 * 理由を選ぶまで送信を待つと、押しただけで画面を離れた人の👎が消える。ここは
 * 記録済みの評価に後から足すだけなので、無視して先へ進んでも何も失われない。
 * 👍のときは出さない（催促を増やすと満足度が下がる）。
 */
function ReasonPicker({
  reasons,
  picked,
  onPick,
  onComment,
}: {
  reasons: string[];
  picked?: string;
  onPick: (reason: string) => void;
  onComment: (comment: string) => void;
}) {
  const [text, setText] = useState("");
  const [sent, setSent] = useState(false);
  if (reasons.length === 0) return null;
  return (
    <div className="fb-reason">
      <span className="fb-reason-label">
        {picked ? "ありがとうございます。" : "よければ理由を教えてください（任意）"}
      </span>
      <div className="fb-reason-choices">
        {reasons.map((r) => (
          <button
            key={r}
            className={`fb-chip ${picked === r ? "fb-chip-on" : ""}`}
            onClick={() => onPick(r)}
          >
            {r}
          </button>
        ))}
      </div>
      {/* 選択肢に無いことを書いてもらう欄。選択肢と別に持つのは、自由記述に
          混ぜると「情報が古い が増えている」のような数え方ができなくなるため。 */}
      <div className="fb-reason-note">
        <input
          placeholder="補足があれば（任意）"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setSent(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && text.trim()) {
              onComment(text.trim());
              setSent(true);
            }
          }}
        />
        <button
          className="fb-chip"
          disabled={!text.trim() || sent}
          onClick={() => {
            onComment(text.trim());
            setSent(true);
          }}
        >
          {sent ? "送信済み" : "送信"}
        </button>
      </div>
    </div>
  );
}

/** その区分に属する文書名を取ってくる（GET /documents）。
 *
 * 「評価用の質問の正解文書」を選ばせるための候補。★区分の絞り込みは検索側と
 * 同じ規則★（未指定＝絞らない、指定＝その区分だけで NULL の共通文書は含まない）
 * なので、ここに出る文書は「その区分で評価したときに実際に引ける文書」と一致する。
 * 出ない文書を正解に指定できてしまうと、その設問は永久に不正解になる。
 *
 * 取得中は null を返す（空配列＝「この区分に文書が無い」と区別する）。
 * 呼び出し側はこの2つで出す文言が変わる。
 */
function useDocuments(
  project: string,
  topic: string,
  reloadKey: number,
): string[] | null {
  const [sources, setSources] = useState<string[] | null>(null);
  useEffect(() => {
    // ★区分が変わった時点で候補を捨てる★
    //   取り直しを待つあいだ前の区分の候補が残っていると、その一瞬に
    //   「新しい区分では引けない文書」を正解として登録できてしまう
    //   （＝存在しない正解と同じで、その設問は永久に不正解になる）。
    setSources(null);
    const params = new URLSearchParams();
    if (project.trim()) params.set("project", project.trim());
    if (topic.trim()) params.set("topic", topic.trim());
    // 区分を続けて変えると応答が前後するので、古い結果は捨てる（useTopics と同じ）
    let current = true;
    const query = params.toString();
    fetch(`/api/backend/documents${query ? `?${query}` : ""}`)
      .then((r) => r.json())
      .then((d) => {
        if (current) {
          setSources(
            (d.documents ?? []).map((doc: { source: string }) => doc.source),
          );
        }
      })
      .catch(() => {
        // 取得に失敗したら「候補なし」に倒す。null のままだと読み込み中の
        // 表示で止まり、待てば出てくるように見えてしまう。
        if (current) setSources([]);
      });
    return () => {
      current = false;
    };
  }, [project, topic, reloadKey]);
  return sources;
}

/** 文書一覧（GET /documents/summary）。「入っている文書」の表用。
 *
 * ★useDocuments とは別のAPI★ あちらは ③ のセレクタを埋めるためのもので、
 * 同じ source を1件に潰す。こちらは「今どうなっているか」を見る画面なので
 * 潰さない（同名が2行あること自体が見せたい異常）。
 *
 * 取得中は null を返す（空配列＝「この区分に文書が無い」と区別する）。
 * 呼び出し側はこの2つで出す文言が変わる。
 */
function useDocumentSummaries(
  project: string,
  topic: string,
  reloadKey: number,
): { rows: DocumentSummary[] | null; truncated: boolean } {
  const [rows, setRows] = useState<DocumentSummary[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    // 区分を変えたら前の区分の行をすぐ捨てる。残しておくと、取り直しの一瞬
    // 「絞ったのに別区分の文書が入っている」ように見える（useDocuments と同じ）。
    setRows(null);
    setTruncated(false);
    const params = new URLSearchParams();
    if (project.trim()) params.set("project", project.trim());
    if (topic.trim()) params.set("topic", topic.trim());
    // 区分を続けて変えると応答が前後するので、古い結果は捨てる
    let current = true;
    const query = params.toString();
    fetch(`/api/backend/documents/summary${query ? `?${query}` : ""}`)
      .then((r) => r.json())
      .then((d) => {
        if (current) {
          setRows(d.documents ?? []);
          setTruncated(Boolean(d.truncated));
        }
      })
      .catch(() => {
        // 失敗したら「0件」に倒す。null のままだと読み込み中の表示で止まり、
        // 待てば出てくるように見えてしまう。
        if (current) setRows([]);
      });
    return () => {
      current = false;
    };
  }, [project, topic, reloadKey]);
  return { rows, truncated };
}

/** 一覧の並び替えのキー。既定は登録日時（新しい順＝APIが返す順）。 */
type DocSortKey = "source" | "created_at" | "chunk_count";

/** 一覧を指定のキーで並べ替える（表示用のコピーを返す）。
 *
 * 件数はAPI側の上限で頭打ちなので、サーバに撃ち直さず手元で並べ替える
 * （押すたびに読み込み待ちが入るほうが、この画面では邪魔になる）。
 */
function sortDocuments(
  rows: DocumentSummary[],
  key: DocSortKey,
  desc: boolean,
): DocumentSummary[] {
  const sorted = [...rows].sort((a, b) => {
    if (key === "source") return a.source.localeCompare(b.source, "ja");
    if (key === "chunk_count") return a.chunk_count - b.chunk_count;
    // 日時不明(null)は常に末尾へ。昇順・降順のどちらでも「不明が一番古い/
    // 新しい」と読めてしまうのを避ける。
    if (!a.created_at || !b.created_at) {
      if (a.created_at === b.created_at) return 0;
      return a.created_at ? -1 : 1;
    }
    return a.created_at.localeCompare(b.created_at);
  });
  if (!desc) return sorted;
  // null を末尾に固定したいので、逆順にするのは値を持つ行だけ。
  const known = sorted.filter((r) => key !== "created_at" || r.created_at);
  const unknown = sorted.filter((r) => key === "created_at" && !r.created_at);
  return [...known.reverse(), ...unknown];
}

/** 登録日時の表示。秒までは要らないので分まで。 */
function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "不明";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "不明";
  return d.toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 選択肢に無い値（他パネルで打った新規の区分など）も候補に残す。 */
function withCurrent(options: string[], value: string): string[] {
  const v = value.trim();
  return v && !options.includes(v) ? [...options, v] : options;
}

/** 文字列の候補を antd の options 形（{value,label}）にする。 */
function toOptions(values: string[]): { value: string; label: string }[] {
  return values.map((v) => ({ value: v, label: v }));
}

type ScopeProps = {
  project: string;
  topic: string;
  projects: string[];
  topics: string[];
  onProject: (v: string) => void;
  onTopic: (v: string) => void;
  /** 同じ画面に複数置くので、ラベルと入力欄を結ぶ id を置き場所ごとに変える。 */
  idPrefix: string;
};

/** 検索・質問・評価で使う区分の絞り込み。未選択（すべて）＝絞り込まない。
 *
 * ここは「既存の区分から選ぶ」場面なので Select にしてある（打ち間違いで
 * 0件になるのを防ぐ）。新しい区分を作れるのは登録側だけ（ScopeInput）。
 * プロジェクトを変えたらトピックは外す: 別プロジェクトのトピックが残ると
 * 存在しない組み合わせになり、黙って0件になるため。
 *
 * ★ネイティブの <select> ではなく antd の Select★
 *   候補（区分マスタ）は運用で増えていく一方だが、ネイティブのポップアップは
 *   OS が描くので高さも配色も指定できない——候補が増えると画面いっぱいに開き、
 *   ダークテーマでもリストだけ OS 側の色で出る。listHeight で上限を決めて
 *   スクロールさせられるのと、ConfigProvider の色が効くのがここでの利点。
 */
function ScopeSelect({
  project,
  topic,
  projects,
  topics,
  onProject,
  onTopic,
  idPrefix,
}: ScopeProps) {
  return (
    <div className="scope-row">
      <div className="scope-field">
        <label className="scope-label" htmlFor={`${idPrefix}-project`}>
          プロジェクト（任意）
        </label>
        <Select
          id={`${idPrefix}-project`}
          // 空文字は「すべて」= 絞り込まない、を意味する内部表現。Select には
          // 空の選択肢を作らず undefined を渡して placeholder を出す。
          value={project || undefined}
          placeholder="すべて"
          options={toOptions(withCurrent(projects, project))}
          onChange={(v?: string) => {
            onProject(v ?? "");
            onTopic("");
          }}
          onClear={() => {
            onProject("");
            onTopic("");
          }}
          allowClear
          showSearch
          listHeight={LIST_HEIGHT}
        />
      </div>
      <div className="scope-field">
        <label className="scope-label" htmlFor={`${idPrefix}-topic`}>
          トピック（任意）
        </label>
        <Select
          id={`${idPrefix}-topic`}
          value={topic || undefined}
          placeholder="すべて"
          options={toOptions(withCurrent(topics, topic))}
          onChange={(v?: string) => onTopic(v ?? "")}
          allowClear
          showSearch
          listHeight={LIST_HEIGHT}
        />
      </div>
    </div>
  );
}

/** 登録側の区分入力。既存の候補を出しつつ、新しい区分も打てる（AutoComplete）。
 *
 * 登録は「まだ無いプロジェクトを作る」入口でもあるので、選ぶだけの Select には
 * できない。絞り込み側（ScopeSelect）と見た目は揃え、★自由入力の可否だけ★を
 * 変えるために AutoComplete を使う。
 */
function ScopeInput({
  project,
  topic,
  projects,
  topics,
  onProject,
  onTopic,
  idPrefix,
}: ScopeProps) {
  return (
    <div className="scope-row">
      <div className="scope-field">
        <label className="scope-label" htmlFor={`${idPrefix}-project`}>
          プロジェクト（任意）
        </label>
        <AutoComplete
          id={`${idPrefix}-project`}
          value={project}
          placeholder="プロジェクト（新規も可）"
          options={toOptions(projects)}
          onChange={(v: string) => onProject(v ?? "")}
          // 既定は「打った文字で始まる候補」だけ。部分一致にしておかないと、
          // 「〇〇規程」のように後ろが同じ区分を探せない。
          filterOption={(input, option) =>
            (option?.value ?? "").toLowerCase().includes(input.toLowerCase())
          }
          allowClear
          listHeight={LIST_HEIGHT}
        />
      </div>
      <div className="scope-field">
        <label className="scope-label" htmlFor={`${idPrefix}-topic`}>
          トピック（任意）
        </label>
        <AutoComplete
          id={`${idPrefix}-topic`}
          value={topic}
          placeholder="トピック（新規も可）"
          options={toOptions(topics)}
          onChange={(v: string) => onTopic(v ?? "")}
          filterOption={(input, option) =>
            (option?.value ?? "").toLowerCase().includes(input.toLowerCase())
          }
          allowClear
          listHeight={LIST_HEIGHT}
        />
      </div>
    </div>
  );
}

/** 区分（プロジェクト / トピック）だけを作るパネル。
 *
 * ★文書登録フォームから切り離してある★
 *   以前は文書登録フォームの区分入力欄を流用し、その横に「区分だけ登録する」
 *   ボタンを置いていた。同じ入力欄が「これから入れる文書に付ける区分」と
 *   「新しく作る区分」の両方を兼ねていたので、打った名前がどちらの意味になるかが
 *   押すボタンでしか決まらず、画面からは読めなかった。作る操作は作る専用の
 *   入力欄を持たせて、独立した区分として置く。
 *
 * ★文書のパネルとは別の箱にして、その上に置く★
 *   文書タブの他の面（登録する / 何が入っているか見る）は文書を扱うが、
 *   ここが触るのは区分マスタだけで文書には触らない。同じ箱に並べると
 *   文書の作業の一部に見えるので、箱を分けて別の機能だと分かるようにする。
 *
 * ★入力値をこのコンポーネントの中で持つ理由★
 *   絞り込み（どの区分を見ているか）と違い、ここは送ったら用済みの入力。
 *   タブを離れて消えても困らないので、上に持ち上げない。
 */
function ScopeCreatePanel({
  onCreated,
}: {
  /** 作れたら呼ぶ。各パネルのセレクタを引き直させる。 */
  onCreated: () => void;
}) {
  const [project, setProject] = useState("");
  const [topic, setTopic] = useState("");
  // ★結果は3種類あり、色を分ける★
  //   作れた / 既にあった / 失敗した、の3つ。「既にあった」は失敗ではない
  //   （API も 200 で created=false を返す）ので、赤いエラーにはしない。
  //   かといって成功と同じ見た目だと「作られた」と読めてしまうため、
  //   注意の色で「重ねなかった」と分かるようにする。
  const [status, setStatus] = useState<{
    kind: "ok" | "exists" | "error";
    text: string;
  } | null>(null);
  const [saving, setSaving] = useState(false);

  /** 入力欄の区分をマスタに登録する（文書は入れない）。
   *
   * トピックを打っていればトピックを作る。その親としてプロジェクトも一緒に送るので、
   * プロジェクト単独の登録が要るのは「プロジェクトだけ打った」ときだけ。
   */
  async function createScope() {
    const p = project.trim();
    const t = topic.trim();
    if ((!p && !t) || saving) return;
    setSaving(true);
    setStatus(null);
    try {
      const res = t
        ? await fetch("/api/backend/topics", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ name: t, project: p || null }),
          })
        : await fetch("/api/backend/projects", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ name: p }),
          });
      const err = await errorMessage(res);
      if (err) {
        setStatus({ kind: "error", text: err });
        return;
      }
      const data = await res.json();
      const what = t ? "トピック" : "プロジェクト";
      const label = t ? `${p || "（プロジェクトなし）"} / ${t}` : p;
      // created=false は「既にあった」＝エラーではない。作らずに済ませただけ。
      setStatus(
        data.created
          ? { kind: "ok", text: `${what}「${label}」を作りました。` }
          : {
              kind: "exists",
              text:
                `${what}「${label}」はすでに存在します。` +
                "同じ名前は重ねて作られないので、そのまま各パネルのセレクタから選べます。",
            },
      );
      // 作れたときだけ入力を空にする。既にあった場合に消すと、
      // 何を打って既存扱いになったのかが画面から消えてしまう。
      if (data.created) {
        setProject("");
        setTopic("");
      }
      onCreated();
    } catch (e) {
      setStatus({ kind: "error", text: `エラー: ${String(e)}` });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel">
      <h2>1. 新たに区分を追加（/projects・/topics・APIキー不要）</h2>
      <p className="hint panel-note">
        文書が無くても区分だけ先に作れます。作った区分は各パネルのセレクタに出るので、
        「先に部署を作っておいて、資料は後から入れる」という順で使えます。
        トピックだけ打つと、上のプロジェクト（空ならプロジェクトなし）の配下に作ります。
      </p>
      {/* ★ここだけ候補の一覧を出さない★
          他の区分入力欄は「すでにある区分を選ぶ」ための候補を出すが、ここは
          新しい名前を作るための欄。既存の名前を選べても作れるものは無い
          （同じ名前は重ねて作られず「すでに存在します」で終わる）ので、
          選択肢を出すと押せて何も起きない操作が増えるだけになる。 */}
      <div className="scope-row">
        <div className="scope-field">
          <label className="scope-label" htmlFor="newscope-project">
            プロジェクト
          </label>
          <input
            id="newscope-project"
            className="scope-text"
            value={project}
            placeholder="作るプロジェクト名"
            onChange={(e) => setProject(e.target.value)}
          />
        </div>
        <div className="scope-field">
          <label className="scope-label" htmlFor="newscope-topic">
            トピック
          </label>
          <input
            id="newscope-topic"
            className="scope-text"
            value={topic}
            placeholder="作るトピック名"
            onChange={(e) => setTopic(e.target.value)}
          />
        </div>
      </div>
      <div className="verify-controls">
        <button
          onClick={createScope}
          disabled={saving || (!project.trim() && !topic.trim())}
        >
          {saving ? "登録中…" : "この区分を作る"}
        </button>
        <span className="hint">
          マスタにだけ登録します。文書やファイルは登録しません。
        </span>
      </div>
      {status && (
        // 読み上げにも届くようにする（押した結果が視覚の色だけで伝わる状態にしない）
        <p className={`result-note result-${status.kind}`} role="status">
          {status.text}
        </p>
      )}
    </section>
  );
}

/** 並び替えボタン付きの表の見出しセル（「入っている文書」の表用）。
 *
 * ★DocumentListPanel の中で定義しない★ 描画のたびに別のコンポーネントとして
 * 扱われ、Reactが中身を作り直す（＝押した直後にフォーカスが外れる）ため。
 */
function DocSortHeader({
  label,
  state,
  tip,
  onClick,
}: {
  label: string;
  /** "off" = この列では並べていない。 */
  state: "asc" | "desc" | "off";
  tip: string;
  onClick: () => void;
}) {
  const on = state !== "off";
  return (
    <th
      // 読み上げに「今どちらの向きで並んでいるか」を伝える
      aria-sort={
        state === "desc"
          ? "descending"
          : state === "asc"
            ? "ascending"
            : "none"
      }
    >
      <button
        type="button"
        className={on ? "doc-sort active" : "doc-sort"}
        onClick={onClick}
        title={tip}
      >
        {label}
        {/* 押していない列は「↕」。押せること自体を見せる。 */}
        <span className="doc-sort-mark" aria-hidden="true">
          {state === "desc" ? "▼" : state === "asc" ? "▲" : "↕"}
        </span>
      </button>
    </th>
  );
}

/** 「入っている文書」の面。登録済みの文書を区分で絞って表で見る。
 *
 * ★何のために要るか★
 *   これまで文書名が画面に出るのは検索結果（②）と評価結果（③）の中だけで、
 *   「そのプロジェクトに何が入っているか」を見る手段が無かった。結果として
 *   次の状態に気づけない:
 *     - 取り込んだつもりで入っていない（チャンク0）
 *     - 区分が付いていない（区分で絞った検索から丸ごと外れる）
 *     - 同じ内容を別名／同名で二重登録している
 *     - 評価コーパスとデモ用文書が混ざって見分けが付かない
 *   表の各列がそのままこの4つに対応している。
 *
 * 気づいた次にやることが「消す」なので、削除もここに置く（気づく場所と直す場所を
 * 分けない）。区分の付け替えはまだ扱わない。
 */
function DocumentListPanel({
  projects,
  scopeVersion,
  project,
  topic,
  onProject,
  onTopic,
  sortKey,
  sortDesc,
  onSort,
  onAddDocuments,
}: {
  projects: string[];
  scopeVersion: number;
  // ★絞り込みと並び順は Home が持つ★
  //   このパネルはタブを離れると描画ごと消えるので、ここで useState すると
  //   戻ったときに「すべて」に戻ってしまう。他パネル（検索結果・チャット履歴）
  //   と同じく、消えるのは描画だけ・選択は残る、に揃える。
  project: string;
  topic: string;
  onProject: (v: string) => void;
  onTopic: (v: string) => void;
  sortKey: DocSortKey;
  sortDesc: boolean;
  onSort: (key: DocSortKey, desc: boolean) => void;
  /** 0件のときに、同じ画面の上にある登録フォームへ送る（畳んでいれば開く）。 */
  onAddDocuments: () => void;
}) {
  const topics = useTopics(project, scopeVersion);
  // 手動の再読み込み。scopeVersion は登録が済むと増える（uploadPending）ので、
  // 「登録 → 一覧を見る」の順なら押さなくても最新になる。押す必要があるのは
  // 別の端末やCLIから入れたときなので、ボタン自体は残す。
  // ここは「押した回数」でしかなく残す価値が無いので、パネル内で持つ。
  const [reload, setReload] = useState(0);
  const { rows, truncated } = useDocumentSummaries(
    project,
    topic,
    scopeVersion + reload,
  );

  // 削除の選択状態。★行IDで持つ★ 文書名は一意ではない（同名の二重登録が
  // あり得る）ので、名前で持つと「片方だけ消す」ができない。
  const [selected, setSelected] = useState<number[]>([]);
  // 確認ダイアログを開いているか。取り消せない操作なので、押した直後には消さず
  // 「何を消すか」を見せてから確定させる。
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteNote, setDeleteNote] = useState<{
    kind: "ok" | "exists" | "error";
    text: string;
  } | null>(null);

  const shown = rows ?? [];
  const selectable = shown.map((d) => d.id);

  // ★表から消えた行の選択は落とす★
  //   残したままにすると、区分を絞って選ぶ → 別の区分を見る → 元に戻す、で
  //   前の選択が復活する。押す直前に画面で見ていた選択と、実際に消える文書が
  //   食い違うので、表に無くなった時点で選択も捨てる。
  useEffect(() => {
    // 取得中（null）は「0件になった」ではないので触らない。ここで落とすと
    // 再読み込みのたびに選択が消える。
    if (!rows) return;
    const visible = new Set(rows.map((d) => d.id));
    setSelected((prev) => {
      const next = prev.filter((id) => visible.has(id));
      // 中身が同じなら前の配列を返す（毎回新しい配列にすると再描画が止まらない）
      return next.length === prev.length ? prev : next;
    });
  }, [rows]);

  // 上の effect が走るまでの1描画ぶん、選択に表から消えた行が残りうる。
  // 消す対象は必ず今表に出ている行だけにする。
  const picked = selected.filter((id) => selectable.includes(id));
  const pickedRows = shown.filter((d) => picked.includes(d.id));
  const allPicked = selectable.length > 0 && picked.length === selectable.length;

  function togglePick(id: number) {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((v) => v !== id) : [...prev, id],
    );
  }

  function togglePickAll() {
    setSelected(allPicked ? [] : selectable);
  }

  /** 選択した文書を消す（確認ダイアログの「削除する」から呼ぶ）。 */
  async function runDelete() {
    if (deleting || picked.length === 0) return;
    setDeleting(true);
    try {
      const res = await fetch("/api/backend/documents", {
        method: "DELETE",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ids: picked }),
      });
      const err = await errorMessage(res);
      if (err) {
        setDeleteNote({ kind: "error", text: err });
        return;
      }
      const data = await res.json();
      // ★宙に浮いた評価質問は必ず出す★
      //   正解ラベル(expected_source)は文書名を指すだけなので、文書を消しても
      //   質問は残る。残った質問は以後どう検索しても当たらず、Hit@k / MRR が
      //   黙って下がる。数字だけが下がって理由が分からない、を防ぐ。
      const orphaned =
        data.orphaned_questions > 0
          ? `　※ ${data.orphaned_questions}件の評価用質問が、消えた文書（${data.orphaned_sources.join("・")}）を正解に指したままです。` +
            "③「質問を追加」で正解を付け替えるか、その質問を消してください。"
          : "";
      setDeleteNote({
        kind: data.orphaned_questions > 0 ? "exists" : "ok",
        text: `${data.deleted}件の文書を削除しました。${orphaned}`,
      });
      setSelected([]);
      setConfirming(false);
      setReload((v) => v + 1);
    } catch (e) {
      setDeleteNote({ kind: "error", text: `エラー: ${String(e)}` });
    } finally {
      setDeleting(false);
    }
  }

  function toggleSort(key: DocSortKey) {
    if (key === sortKey) {
      onSort(key, !sortDesc);
      return;
    }
    // 文書名は昇順（あ→ん）、数と日時は降順（多い順・新しい順）から始めるのが
    // それぞれ「まず見たい向き」。
    onSort(key, key !== "source");
  }

  const sorted = rows ? sortDocuments(rows, sortKey, sortDesc) : null;

  /** その列が並び替えの基準か、基準ならどちら向きか。 */
  const sortState = (key: DocSortKey) =>
    sortKey === key ? (sortDesc ? "desc" : "asc") : "off";

  return (
    <div className="panel-section">
      <h3 className="panel-section-title">
        3. 入っている文書（/documents/summary・APIキー不要）
      </h3>
      <p className="hint panel-note">
        登録済みの文書を区分で絞って一覧します。
        <strong>チャンク数が0の行は索引に載っていません</strong>
        （＝検索で絶対に引けない）。区分が「共通」の文書は、
        区分で絞った検索・評価の対象から外れます。
      </p>

      <ScopeSelect
        idPrefix="doclist"
        project={project}
        topic={topic}
        projects={projects}
        topics={topics}
        onProject={onProject}
        onTopic={onTopic}
      />

      <div className="verify-controls">
        <button onClick={() => setReload((v) => v + 1)}>再読み込み</button>
        {/* 削除は取り消せないので、選んでいないときは押せないままにする
            （0件で押せて「何も起きない」より、押せない理由が見えるほうがよい）。 */}
        <button
          className="danger-button"
          onClick={() => setConfirming(true)}
          disabled={picked.length === 0}
          title={
            picked.length === 0
              ? "消す文書を左のチェックボックスで選んでください"
              : `選んだ${picked.length}件を削除する`
          }
        >
          選んだ文書を削除（{picked.length}件）
        </button>
        <span className="hint">
          {sorted === null
            ? "読み込み中…"
            : `${sorted.length}件${truncated ? "（上限まで）" : ""}`}
        </span>
      </div>

      {deleteNote && (
        <p className={`result-note result-${deleteNote.kind}`} role="status">
          {deleteNote.text}
        </p>
      )}

      {/* 確認ダイアログ。★何件・どれを消すかを名前で出す★
          チェックボックスは押し間違えても見た目がほとんど変わらないので、
          「今から消えるもの」を一覧にして最後にもう一度目で確かめさせる。 */}
      <Modal
        open={confirming}
        onCancel={() => setConfirming(false)}
        title="この文書を削除しますか？"
        okText={deleting ? "削除中…" : `削除する（${picked.length}件）`}
        cancelText="やめる"
        okButtonProps={{ danger: true, disabled: deleting }}
        cancelButtonProps={{ disabled: deleting }}
        onOk={runDelete}
      >
        <p className="hint">
          <strong>この操作は取り消せません。</strong>
          文書本体・チャンク・図表・S3の原本ファイルが消えます。
          評価用質問がこの文書を正解に指していた場合、質問は残るので
          削除後に付け替えてください。
        </p>
        <ul className="doc-delete-list">
          {pickedRows.map((d) => (
            <li key={d.id}>
              {d.source}
              <span className="doc-null">
                {" "}
                （{d.project ?? "共通"} / {d.topic ?? "共通"}・
                {d.chunk_count}チャンク）
              </span>
            </li>
          ))}
        </ul>
      </Modal>

      {/* 上限で切れたことは黙らせない。続きがあるのに全部だと読まれると、
          「入っていない文書に気づく」というこの画面の役目が壊れる。 */}
      {truncated && (
        <p className="hint ingest-status">
          件数が多いため上限で打ち切りました。区分で絞ると続きが見えます。
        </p>
      )}

      {sorted !== null && sorted.length === 0 && (
        <p className="hint">
          この区分には文書がありません。
          {project.trim() || topic.trim() ? (
            <>
              {" "}
              区分の指定を外すと全文書が見えます。この区分に入れたい場合は{" "}
              <button className="linklike" onClick={onAddDocuments}>
                上の「新たに文書を登録」
              </button>{" "}
              で同じ区分を付けて登録してください。
            </>
          ) : (
            <>
              {" "}
              <button className="linklike" onClick={onAddDocuments}>
                上の「新たに文書を登録」
              </button>{" "}
              からファイルを登録してください。
            </>
          )}
        </p>
      )}

      {sorted !== null && sorted.length > 0 && (
        <div className="table-wrap">
          <table className="doc-table">
            <thead>
              <tr>
                <th className="doc-select">
                  {/* 表に出ている行の全選択。絞り込みで見えていない行は含めない
                      （見えないものを消さない）。 */}
                  <label title="表示中の文書をすべて選ぶ">
                    <input
                      type="checkbox"
                      checked={allPicked}
                      onChange={togglePickAll}
                      aria-label="表示中の文書をすべて選ぶ"
                    />
                  </label>
                </th>
                <DocSortHeader
                  label="文書名"
                  state={sortState("source")}
                  tip="文書名で並べ替え（クリックで昇順/降順）"
                  onClick={() => toggleSort("source")}
                />
                <th>プロジェクト</th>
                <th>トピック</th>
                <DocSortHeader
                  label="チャンク"
                  state={sortState("chunk_count")}
                  tip="チャンク数で並べ替え。0 は索引に載っていない"
                  onClick={() => toggleSort("chunk_count")}
                />
                <th>
                  <Tip label="画像">
                    左のチャンクのうち<strong>画像チャンク</strong>
                    （<code>image_path</code> のあるもの）の数。 PDF/xlsx/pptx
                    に図表があるのに「—」なら、図表が索引に載っていない
                    ＝図の内容は検索で引けない。
                  </Tip>
                </th>
                <th>
                  <Tip label="差分検知">
                    取り込んだ内容のハッシュ（<code>content_hash</code>
                    ）を持っているか。 「—」の行は次に同じファイルを登録したとき、
                    中身が変わっていなくても
                    <strong>必ず入れ直される</strong>（＝埋め込みAPIを呼ぶ）。
                  </Tip>
                </th>
                <DocSortHeader
                  label="登録日時"
                  state={sortState("created_at")}
                  tip="登録日時で並べ替え（既定は新しい順）"
                  onClick={() => toggleSort("created_at")}
                />
              </tr>
            </thead>
            <tbody>
              {sorted.map((d) => (
                // key は id。source は一意ではない（同名の二重登録があり得る）
                <tr key={d.id} className={picked.includes(d.id) ? "doc-picked" : undefined}>
                  <td className="doc-select">
                    <label>
                      <input
                        type="checkbox"
                        checked={picked.includes(d.id)}
                        onChange={() => togglePick(d.id)}
                        aria-label={`${d.source} を選ぶ`}
                      />
                    </label>
                  </td>
                  <td className="doc-source">
                    <SourceLink source={d.source} />
                  </td>
                  {/* ★区分なしは空欄にしない★
                      空欄だと「表示漏れ」と読めてしまう。NULL は
                      「どこにも属さない共通文書」という意味のある状態なので、
                      そう書く。 */}
                  <td>
                    {d.project ?? <span className="doc-null">共通</span>}
                  </td>
                  <td>{d.topic ?? <span className="doc-null">共通</span>}</td>
                  {/* 0件は索引に載っていない＝検索で引けないので、他と違う色で出す */}
                  <td
                    className={
                      d.chunk_count === 0 ? "doc-num doc-warn" : "doc-num"
                    }
                  >
                    {d.chunk_count}
                  </td>
                  <td className="doc-num">
                    {d.image_chunk_count > 0 ? (
                      d.image_chunk_count
                    ) : (
                      <span className="doc-null">—</span>
                    )}
                  </td>
                  <td className="doc-num">
                    {d.has_content_hash ? (
                      "✓"
                    ) : (
                      <span className="doc-null">—</span>
                    )}
                  </td>
                  <td className="doc-when">{formatDateTime(d.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** 左サイドバーのタブ。順番がそのまま画面の並びになる。
 *
 * ①〜④ の番号は「文書を入れる → 検索を見る → 数字で測る → 会話する」という
 * 想定の順路。番号を振っておくと、説明文から他タブを指すときに短く書ける。
 *
 * ★会話は末尾★ 検索と評価で挙動を詰めてから使うものなので、順路の最後に置く
 * （このツールの主役は②の内訳と③の数字で、会話はその結果を使う側）。
 *
 * ★④は「会話する」★ 叩いているのが /chat（履歴を持つ会話API）なので、
 * 画面の名前もAPIに合わせる。「質問する」だと1問1答に見えるが、実際は
 * conversation_id で履歴が繋がる。
 *
 * ★desc はタブに当てたときのツールチップ★
 *   ②と③はどちらも「検索が当たっているかを見る」画面なので、名前だけだと
 *   毎回どっちがどっちか分からなくなる。パネルを開かないと違いが読めないのは
 *   遅いので、★選ぶ前の段階（サイドバー）★で読めるようにする。
 *   title 属性なので改行は \n で入れる。
 */
const TABS = [
  {
    id: "ingest",
    label: "① 文書を登録",
    hint: "/ingest-file",
    desc:
      "検索・回答の対象になる文書を入れる。\n" +
      "ここに入っていない文書は、②③④で何をしても出てこない。\n" +
      "同じ画面で区分を作り、今なにが入っているかも一覧できる。",
  },
  {
    id: "search",
    label: "② 検索の内訳",
    hint: "/search",
    desc:
      "質問を1件だけ投げて、ベクトル・字面・BM25 がそれぞれ何位を付けたかを並べて見る。\n" +
      "「1問をなぜその順位にしたのか」を開いて見る場所。点数は付かない。\n" +
      "RRFのkや閾値をその場で変えて、順位の動き方を体感するのに使う。",
  },
  {
    id: "eval",
    label: "③ 評価する",
    hint: "/eval",
    desc:
      "正解ラベル付きの質問集をまとめて検索して、Hit@k / MRR という数字にする。\n" +
      "「質問集全体で当たるようになったか」を測る場所。\n" +
      "②が1問の中身を見るのに対し、③は改良の前後で数字が上がったかを判定する。",
  },
  {
    id: "chat",
    label: "④ 会話する",
    hint: "/chat",
    desc:
      "検索で見つけたチャンクを根拠に Claude が回答する。\n" +
      "②と③で詰めた検索の設定を、実際の受け答えとして使う場所。",
  },
] as const;

type TabId = (typeof TABS)[number]["id"];

/** ③ の配下タブ。
 *
 * ③ は「正解ラベル付きの質問を貯める」と「貯めた質問集で数字を出す」という、
 * 使う頻度も見たいものも違う2つが1画面に縦積みになっていた（登録フォームを
 * 毎回読み飛ばして下のスコアまでスクロールすることになる）。同じ
 * eval_questions を扱う一続きの作業なので別タブには割らず、③ の配下で切り替える。
 */
const EVAL_SUBTABS = [
  { id: "add", label: "質問を追加", hint: "/eval-questions" },
  { id: "run", label: "質問集を評価", hint: "/eval" },
  // ★ここに置いたのは「評価するもの」が同じだから★
  //   ③は正解ラベルを人が用意して採点する。👍/👎 は利用者が付けた評価で、
  //   正解が無い代わりに実際に聞かれた質問に付く。どちらも「今の出来を測る」
  //   材料で、④（会話そのもの）ではなくこちらに属する。
  { id: "feedback", label: "👍/👎 を見る", hint: "/feedback" },
] as const;

type EvalSubTab = (typeof EVAL_SUBTABS)[number]["id"];

/** ② の配下タブ。
 *
 * ★保管質問の検証(/verify)をここに入れてある★
 *   検証が対象にする saved_questions は、②で検索するたびに貯まっていくもの
 *   （②の副産物）。独立したタブに置くと「どこから来た質問集なのか」が
 *   画面の構造から読めず、③の評価（正解ラベル付き・採点する）と混同される。
 *   ②の配下にすれば「②で投げた質問を、まとめて引き直す」と位置で分かる。
 */
const SEARCH_SUBTABS = [
  { id: "stages", label: "質問で資料を検索", hint: "/search" },
  { id: "verify", label: "保管質問をまとめて再検索", hint: "/verify" },
] as const;

type SearchSubTab = (typeof SEARCH_SUBTABS)[number]["id"];

/** 配下タブを持つタブと、その中身。ここに足せばサイドバーに出る。
 *
 * ★① は配下を持たない★
 *   「入れる」「区分を作る」「何が入っているか見る」は一続きの作業で、
 *   配下タブに割ると入れるたびに往復することになる。1画面に縦に並べ、
 *   登録フォームは畳めるようにして「一覧を見るたびに読み飛ばす」のを避ける。
 */
const SUBTABS: Partial<
  Record<TabId, readonly { id: string; label: string; hint: string }[]>
> = {
  search: SEARCH_SUBTABS,
  eval: EVAL_SUBTABS,
};

/** 準拠している公開ベンチマーク（README「参考にした公開ベンチマーク」と同じ内容）。
 *
 * ★データセットは使わず、評価設計だけを借りている★
 *   公開データセットは日本語の社内文書に合わないので、指標と組み立て方
 *   （何を測れば良い/悪いと言えるのか）だけを借りる、という立場。ここを
 *   混同されると「そのベンチマークのスコアを出した」と読まれてしまうため、
 *   表の上に必ず注記を出す。
 *
 * READMEと二重管理になるが、READMEを開かずに画面で確認できることを優先した。
 * 片方を直したらもう片方も直す（README の「参考にした公開ベンチマーク」）。
 */
const BENCHMARKS: {
  work: string;
  /** ベンチマーク名と出典。名前を出すだけだと確かめに行けないのでリンクを必ず持つ。 */
  benches: { name: string; href: string }[];
  design: string;
  /** どこで確認できるか。画面が無いものは「画面なし」と正直に書く。 */
  where: React.ReactNode;
}[] = [
  {
    work: "図表の検索対象化（caption 対 multimodal）",
    benches: [
      { name: "ViDoRe", href: "https://huggingface.co/vidore" },
      {
        name: "ViDoRe v2",
        href: "https://huggingface.co/collections/vidore/vidore-benchmark-v2",
      },
    ],
    design:
      "テキスト化検索 対 画像直接検索を nDCG 系で比較。視覚的ページと非視覚的ページを分けて集計",
    where: (
      <>
        <strong>② 検索の内訳</strong>（画像ベクトル検索のチェックを入れて比べる）
        <br />
        方式そのものの比較は <code>python -m app.eval --compare-image-index</code>
      </>
    ),
  },
  {
    work: "埋め込みモデルの選定（voyage-multimodal-3 を選ぶ根拠）",
    benches: [
      { name: "MIEB", href: "https://arxiv.org/abs/2504.10471" },
      {
        name: "M-BEIR",
        href: "https://huggingface.co/datasets/TIGER-Lab/M-BEIR",
      },
    ],
    design: "画像埋め込みモデルの検索性能の総合評価",
    where: (
      <>
        画面なし（モデルの選択は <code>.env</code> の{" "}
        <code>IMAGE_INDEX_METHOD</code> / <code>MULTIMODAL_EMBED_MODEL</code>）
      </>
    ),
  },
  {
    work: "原本画像を根拠にした回答生成",
    benches: [
      { name: "DocVQA", href: "https://www.docvqa.org/" },
      {
        name: "VisualMRC",
        href: "https://github.com/nttmdlab-nlp/VisualMRC",
      },
      { name: "JDocQA", href: "https://github.com/mizuumi/JDocQA" },
    ],
    design: "文書画像に対する QA の正答率（日本語は JDocQA）",
    where: (
      <>
        <strong>④ 会話する</strong>（回答の根拠に原本画像が出る）
      </>
    ),
  },
  {
    work: "チャート読解支援",
    benches: [
      { name: "ChartQA", href: "https://github.com/vis-nlp/ChartQA" },
      { name: "CharXiv", href: "https://charxiv.github.io/" },
    ],
    design:
      "チャート画像からの読み取り精度。「予測の前に、そもそも読めているか」を測る土台",
    where: (
      <>
        画面なし（<code>POST /chart-read</code> のみ）
      </>
    ),
  },
];

/** 👎率の切り口ごとの1行分（全体・区分別・時系列で同じ形）。 */
type Rate = { total: number; down: number; down_rate: number | null };

type FeedbackStats = Rate & {
  bucket: string;
  by_scope: (Rate & { project: string | null; topic: string | null })[];
  by_period: (Rate & { period: string })[];
  by_reason: { reason: string | null; count: number }[];
};

type FeedbackRow = {
  id: number;
  question: string;
  answer: string;
  sources: string[];
  rating: number;
  reason: string | null;
  comment: string | null;
  created_at: string | null;
  project: string | null;
  topic: string | null;
  conversation_id: number | null;
  message_id: number | null;
  retriever: string | null;
  top_k: number | null;
  reranked: boolean | null;
  chunk_ids: number[];
  latency_ms: number | null;
  /** この評価から作った評価用質問のID（null=まだ評価データセットに入れていない）。 */
  promoted_eval_question_id: number | null;
};

type ChunkDetail = {
  id: number;
  source: string;
  chunk_index: number;
  content: string;
  context: string | null;
  image_path: string | null;
};

type ConversationMessage = {
  id: number;
  role: string;
  content: string;
  sources: string[];
};

/** 👎率を「0件＝まだ誰も評価していない」と区別して出す。 */
function formatRate(rate: number | null): string {
  return rate === null ? "—" : `${(rate * 100).toFixed(0)}%`;
}

/** 期間の絞り込み。★既定を「すべて」にしない★
 *
 * 👎率は「いつからか上がった」を見るためのものなので、既定が全期間だと
 * 大昔の分に薄められて最近の変化が見えない。よく使う幅を並べて選ばせる。
 */
const FEEDBACK_RANGES = [
  { id: "7", label: "直近7日", days: 7, bucket: "day" },
  { id: "30", label: "直近30日", days: 30, bucket: "day" },
  { id: "90", label: "直近90日", days: 90, bucket: "week" },
  { id: "all", label: "すべて", days: 0, bucket: "month" },
] as const;

type FeedbackRange = (typeof FEEDBACK_RANGES)[number]["id"];

/** 👎1件を開いて、そのとき渡したチャンクと元の会話を読む（L2 トリアージ）。
 *
 * ★1件の👎からは原因が分解できない★
 *   検索が外したのか・生成が外したのか・そもそも文書に答えが無かったのかは、
 *   「そのとき何を根拠に渡していたか」を見ないと分けられない。この2つを
 *   開けるようにするのが、👎を調査の入口にするということ。
 */
function FeedbackDetail({ row }: { row: FeedbackRow }) {
  const [chunks, setChunks] = useState<ChunkDetail[] | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (row.chunk_ids.length === 0) {
      setChunks([]);
      return;
    }
    // 並びは記録したまま渡す（＝順位）。サーバもその並びで返す。
    fetch(`/api/backend/chunks?ids=${row.chunk_ids.join(",")}`)
      .then((r) => r.json())
      .then((d) => setChunks(d.chunks ?? []))
      .catch(() => setError("チャンクを読めませんでした"));
  }, [row.chunk_ids]);

  useEffect(() => {
    if (row.conversation_id === null) {
      setMessages([]);
      return;
    }
    fetch(`/api/backend/conversations/${row.conversation_id}`)
      .then((r) => r.json())
      .then((d) => setMessages(d.messages ?? []))
      .catch(() => setError("会話を読めませんでした"));
  }, [row.conversation_id]);

  return (
    <div className="fb-detail">
      {error && <p className="hint">{error}</p>}
      <div className="fb-detail-block">
        <h4>
          そのとき渡したチャンク（{row.chunk_ids.length}件・並びが順位）
        </h4>
        {row.chunk_ids.length === 0 ? (
          <p className="hint">
            記録されていない（この記録より前の👎、または根拠なしで答えた回答）。
          </p>
        ) : chunks === null ? (
          <p className="hint">読み込み中…</p>
        ) : (
          <ol className="fb-chunks">
            {chunks.map((c) => (
              <li key={c.id}>
                <div className="fb-chunk-head">
                  <SourceLink source={c.source} />
                  <code>chunk #{c.id}</code>
                  {c.image_path && <span className="fb-tag">画像</span>}
                </div>
                <p className="fb-chunk-body">{c.content}</p>
              </li>
            ))}
          </ol>
        )}
        {chunks !== null && chunks.length < row.chunk_ids.length && (
          <p className="hint">
            {row.chunk_ids.length - chunks.length}件は今のDBに無い（文書が
            消されている）。評価は残す作りなので、これ自体は壊れていない。
          </p>
        )}
      </div>
      <div className="fb-detail-block">
        <h4>元の会話</h4>
        {row.conversation_id === null ? (
          <p className="hint">記録されていない（この記録より前の👎）。</p>
        ) : messages === null ? (
          <p className="hint">読み込み中…</p>
        ) : (
          <ol className="fb-messages">
            {messages.map((m) => (
              <li
                key={m.id}
                className={m.id === row.message_id ? "fb-msg-target" : undefined}
              >
                <span className="fb-msg-role">
                  {m.role === "user" ? "質問" : "回答"}
                </span>
                <span>{m.content}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

/** 正解と認めるチャンクの種類（バックエンドの EXPECTED_KINDS と同じ並び）。
 *
 * 昇格のダイアログにだけ置いてある。👎は「その文書は引けていたのに、答えの
 * 載っている図表ではなく本文を渡していた」のように★どの種類を引くべきだったか★
 * まで分かっていることが多く、そこまで残せると同じ失敗を測れるようになる。
 */
const EXPECTED_KINDS = [
  { id: "any", label: "どれでもよい", hint: "文書が上位に来れば正解（既定）" },
  { id: "text", label: "本文", hint: "本文チャンクで引けたときだけ正解" },
  { id: "image", label: "図表", hint: "画像チャンクで引けたときだけ正解" },
] as const;

/** 👎を評価用質問（正解ラベル付き）に昇格させるダイアログ。
 *
 * ★👎を貯める意味はここで完結する★
 *   読んで終わりの👎は、次の変更で同じ質問がまた壊れても誰も気づけない。
 *   評価データセットに入って初めて Hit@k / MRR に乗り、以後の改良すべてが
 *   この1問を巻き込んで検証されるようになる。
 *
 * ★ただし機械的に流し込まない★
 *   評価用質問は正解(expected_source)が必須で、「そもそも文書に答えが無い」
 *   質問を混ぜると、引けなくて当然のものを不正解として数え続けることになり、
 *   指標そのものが読めなくなる。だから1件ずつ人が開いて、正解を選んでから通す。
 */
function PromoteModal({
  row,
  scopeVersion,
  onClose,
  onPromoted,
}: {
  /** 昇格しようとしている👎。★開いている間だけマウントされる★
   *
   * 閉じている間も置いておくと、その状態の区分（空＝すべて）で GET /documents を
   * 引きに行く。誰も見ていない候補のために撃つことになるので、呼び出し側で
   * 出し分ける。開くたびに作り直されるので、前に開いた行の入力も残らない
   * （残ると、別の👎に前の質問文を正解付きで登録してしまう）。 */
  row: FeedbackRow;
  scopeVersion: number;
  onClose: () => void;
  onPromoted: (feedbackId: number, evalQuestionId: number) => void;
}) {
  const [question, setQuestion] = useState(row.question);
  // ★正解は初期選択しない★
  //   出典に挙がった文書が正しければ👎は付いていない可能性が高い。既定で
  //   選んでおくと、そのまま押されて「間違った文書」が正解ラベルとして固定される。
  const [expected, setExpected] = useState("");
  const [kind, setKind] = useState<string>("any");
  const [expectedText, setExpectedText] = useState("");
  // 何を見て作った質問かを残す。後から eval_questions を読むとき、この1行が
  // 無いと「なぜこの質問が入っているのか」を辿れない。
  const [note, setNote] = useState(() =>
    [
      "👎から昇格",
      row.reason ? `理由: ${row.reason}` : "",
      row.comment ? `補足: ${row.comment}` : "",
    ]
      .filter(Boolean)
      .join(" / "),
  );
  const [status, setStatus] = useState("");
  const [saving, setSaving] = useState(false);

  // 正解の候補は★その👎を聞いた区分★で絞る。評価は同じ区分の文書だけを検索
  // するので、区分をまたいだ組み合わせを正解にすると永久に不正解になる
  //（③「質問を追加」の候補と同じ規則）。
  const docs = useDocuments(row.project ?? "", row.topic ?? "", scopeVersion);

  // そのとき出典に挙がった文書のうち、今も引けるものだけを候補にする。
  // 消された文書を正解に指定できると、その設問は何をやっても不正解になる。
  const cited = (docs ?? []).filter((d) => row.sources.includes(d));
  const gone = row.sources.filter((s) => docs !== null && !docs.includes(s));
  const others = (docs ?? []).filter((d) => !cited.includes(d));
  const options: { label: string; options: { value: string; label: string }[] }[] = [];
  if (cited.length > 0) {
    options.push({ label: "そのとき出典に挙がった文書", options: toOptions(cited) });
  }
  if (others.length > 0) {
    options.push({ label: "この区分のほかの文書", options: toOptions(others) });
  }

  async function submit() {
    if (saving) return;
    setSaving(true);
    setStatus("");
    try {
      const res = await fetch(`/api/backend/feedback/${row.id}/promote`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          question,
          expected_source: expected,
          expected_kind: kind,
          // 空欄は送らない（＝文書単位で判定する従来どおりの質問になる）
          expected_text: expectedText.trim() || null,
          // 区分は聞いたときのものをそのまま引き継ぐ（選び直させない）。
          // ここを変えると、正解に選んだ文書が評価時には検索対象外になる。
          project: row.project,
          topic: row.topic,
          note: note.trim() || null,
        }),
      });
      const err = await errorMessage(res);
      if (err) {
        setStatus(err);
        return;
      }
      const created = await res.json();
      onPromoted(row.id, created.id);
    } catch (e) {
      setStatus(`エラー: ${String(e)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open
      onCancel={onClose}
      onOk={submit}
      okText="評価用質問として登録"
      cancelText="やめる"
      // 正解を選ぶまで押せない。空のまま送れると、サーバに弾かれるだけの
      // 往復になる（正解必須なのはUIより先にDBの制約）。
      okButtonProps={{ disabled: !question.trim() || !expected || saving }}
      confirmLoading={saving}
      width={720}
      title="この👎を評価用質問にする"
    >
      {status && <p className="error-box">{status}</p>}
      <p className="hint">
        ここで作った質問は、以後 <code>python -m app.eval</code> の
        <strong>Hit@k / MRR に毎回乗る</strong>。逆に
        <strong>そもそも文書に答えが無い</strong>と分かった👎は入れないこと
        （引けなくて当然のものを不正解として数え続けることになり、
        指標そのものが読めなくなる）。
      </p>
      <div className="scope-field">
        <label className="scope-label" htmlFor="promote-question">
          質問（👎の質問がそのまま入っている。直してよい）
        </label>
        <input
          id="promote-question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
      </div>
      <div className="scope-field">
        <label className="scope-label" htmlFor="promote-expected">
          正解の文書（本当はどれを引くべきだったか）
        </label>
        <Select
          id="promote-expected"
          value={expected || undefined}
          placeholder={
            docs === null
              ? "文書を読み込み中…"
              : options.length === 0
                ? "この区分に文書がありません"
                : "文書を選ぶ"
          }
          options={options}
          onChange={(v?: string) => setExpected(v ?? "")}
          disabled={docs === null || options.length === 0}
          allowClear
          showSearch
          listHeight={LIST_HEIGHT}
        />
      </div>
      <p className="hint">
        ★出典に挙がった文書が正解とは限らない★
        違う文書を引くべきだったことこそ👎の中身なので、
        <strong>選び直す前提</strong>で候補として並べてある。 区分（
        {row.project ?? "なし"} / {row.topic ?? "なし"}）は聞いたときのまま。
        {gone.length > 0 && (
          <>
            <br />
            出典のうち <code>{gone.join(" / ")}</code> は今のDBに無い（文書が
            消されている）ので候補に出していない。
          </>
        )}
      </p>
      <div className="scope-field">
        <span className="scope-label">正解と認めるチャンクの種類</span>
        <div className="fb-range">
          {EXPECTED_KINDS.map((k) => (
            <button
              key={k.id}
              type="button"
              className={`fb-chip ${kind === k.id ? "fb-chip-on" : ""}`}
              onClick={() => setKind(k.id)}
              title={k.hint}
            >
              {k.label}
            </button>
          ))}
        </div>
      </div>
      <p className="hint">
        {EXPECTED_KINDS.find((k) => k.id === kind)?.hint}
        {/* 👎は「文書は合っていたが渡した根拠が違った」ことが多く、そこまで
            指定できると同じ失敗を測れる。分からなければ「どれでもよい」でよい。 */}
      </p>
      <details className="q-optional">
        <summary>
          チャンク単位で採点する（任意・ふつうは空欄のままでよい）
          {expectedText.trim() && (
            <span className="q-optional-value">{expectedText.trim()}</span>
          )}
        </summary>
        <div className="hint q-optional-body">
          <p>
            正解チャンクに<strong>必ず含まれる語句</strong>を入れると、
            「その文書のどこか」ではなく
            <strong>「この段落を引けたか」</strong>で採点する。
            渡したチャンクは下の一覧（行を開いた先）で読める。
          </p>
          <input
            placeholder="例: 1日2時間を超える場合"
            value={expectedText}
            onChange={(e) => setExpectedText(e.target.value)}
          />
        </div>
      </details>
      <div className="scope-field">
        <label className="scope-label" htmlFor="promote-note">
          メモ（何を確かめる質問か）
        </label>
        <input
          id="promote-note"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </div>
    </Modal>
  );
}

/** 👍/👎 の集計と一覧（③の配下）。
 *
 * ★上半分は集計、下半分は1件ずつ★
 *   生の👎は単体では意味が分解できないので、「改善のインプット」ではなく
 *   ★調査対象を絞り込むフィルタ★として扱う。上で「どの区分・いつが怪しいか」を
 *   決め、そのまま下の一覧を同じ条件で絞って1件ずつ開く、という順で使う。
 */
function FeedbackPanel({
  projects,
  scopeVersion,
}: {
  projects: string[];
  scopeVersion: number;
}) {
  const [project, setProject] = useState("");
  const [topic, setTopic] = useState("");
  const topics = useTopics(project, scopeVersion);
  const [range, setRange] = useState<FeedbackRange>("30");
  // 一覧はまず👎から見る（👍は数えるものであって、読むものではない）
  const [rating, setRating] = useState<"-1" | "1" | "">("-1");
  const [stats, setStats] = useState<FeedbackStats | null>(null);
  const [rows, setRows] = useState<FeedbackRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);
  // 昇格ダイアログを開いている行（null=閉じている）
  const [promoting, setPromoting] = useState<FeedbackRow | null>(null);
  const [error, setError] = useState("");

  const selected = FEEDBACK_RANGES.find((r) => r.id === range)!;

  /** 集計と一覧で同じ絞り込みを使う（別々に組むと画面の中で食い違う）。 */
  const filters = useMemo(() => {
    const params = new URLSearchParams();
    if (project.trim()) params.set("project", project.trim());
    if (topic.trim()) params.set("topic", topic.trim());
    if (selected.days > 0) {
      const since = new Date();
      since.setDate(since.getDate() - selected.days);
      params.set("since", since.toISOString());
    }
    return params;
  }, [project, topic, selected.days]);

  useEffect(() => {
    const params = new URLSearchParams(filters);
    params.set("bucket", selected.bucket);
    let current = true;
    fetch(`/api/backend/feedback/stats?${params}`)
      .then((r) => r.json())
      .then((d) => {
        if (!current) return;
        if (d.error) setError(d.message);
        else setStats(d);
      })
      .catch(() => setError("集計を読めませんでした"));
    return () => {
      current = false;
    };
  }, [filters, selected.bucket]);

  useEffect(() => {
    const params = new URLSearchParams(filters);
    if (rating) params.set("rating", rating);
    let current = true;
    fetch(`/api/backend/feedback?${params}`)
      .then((r) => r.json())
      .then((d) => {
        if (!current) return;
        setRows(d.feedback ?? []);
        setTotal(d.total ?? 0);
      })
      .catch(() => setError("一覧を読めませんでした"));
    return () => {
      current = false;
    };
  }, [filters, rating]);

  // 棒の長さは「その刻みで一番多かった件数」を基準にする（率だけだと1件しか
  // 評価が無い日が満杯に見える）。
  const peak = Math.max(1, ...(stats?.by_period ?? []).map((p) => p.total));

  /** 昇格できた行に印を付ける。★取り直さずその場で書き換える★
   *
   * 一覧を引き直すと、その間に増えた評価で並びがずれて「今開いていた行」を
   * 見失う。二度登録を防ぐのに要るのは印だけなので、返ってきたIDを入れる。
   */
  function markPromoted(feedbackId: number, evalQuestionId: number) {
    setRows(
      (rs) =>
        rs &&
        rs.map((r) =>
          r.id === feedbackId
            ? { ...r, promoted_eval_question_id: evalQuestionId }
            : r,
        ),
    );
    setPromoting(null);
  }

  return (
    <div className="fb-panel">
      {error && <p className="error-box">{error}</p>}

      <div className="fb-filters">
        <ScopeSelect
          idPrefix="fb"
          project={project}
          topic={topic}
          projects={projects}
          topics={topics}
          onProject={setProject}
          onTopic={setTopic}
        />
        <div className="fb-range">
          {FEEDBACK_RANGES.map((r) => (
            <button
              key={r.id}
              type="button"
              className={`fb-chip ${range === r.id ? "fb-chip-on" : ""}`}
              onClick={() => setRange(r.id)}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* --- L1 集計 ------------------------------------------------------ */}
      <h3 className="stage-title">👎率（/feedback/stats）</h3>
      <p className="hint">
        全体の👎率はまず動かない。見るのは
        <strong>「この区分だけ突出している」「この日から上がった」</strong>で、
        それが調べに行く先を絞ってくれる。
        <strong>件数の小さい区分は率が跳ねる</strong>ので、率と件数を一緒に読む。
      </p>
      {stats && (
        <>
          <p className="fb-overall">
            全体 <strong>{formatRate(stats.down_rate)}</strong>（👎 {stats.down}
            {" / "}
            {stats.total}件）
          </p>

          <h4 className="fb-sub">時系列</h4>
          {stats.by_period.length === 0 ? (
            <p className="hint">この期間に評価がありません。</p>
          ) : (
            <ul className="fb-bars">
              {stats.by_period.map((p) => (
                <li key={p.period}>
                  <span className="fb-bar-label">
                    {formatDateTime(p.period).slice(0, 10)}
                  </span>
                  <span className="fb-bar-track">
                    <span
                      className="fb-bar-fill"
                      style={{ width: `${(p.total / peak) * 100}%` }}
                    >
                      <span
                        className="fb-bar-down"
                        style={{ width: `${(p.down / p.total) * 100}%` }}
                      />
                    </span>
                  </span>
                  <span className="fb-bar-value">
                    {formatRate(p.down_rate)}（{p.down}/{p.total}）
                  </span>
                </li>
              ))}
            </ul>
          )}

          <h4 className="fb-sub">区分別（👎率の高い順）</h4>
          {stats.by_scope.length === 0 ? (
            <p className="hint">この期間に評価がありません。</p>
          ) : (
            <table className="doc-table">
              <thead>
                <tr>
                  <th>プロジェクト</th>
                  <th>トピック</th>
                  <th>👎率</th>
                  <th>👎</th>
                  <th>評価数</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_scope.map((s) => (
                  <tr key={`${s.project}/${s.topic}`}>
                    <td>{s.project ?? "（区分なし）"}</td>
                    <td>{s.topic ?? "（なし）"}</td>
                    <td>{formatRate(s.down_rate)}</td>
                    <td>{s.down}</td>
                    <td>{s.total}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4 className="fb-sub">👎の理由</h4>
          {stats.by_reason.length === 0 ? (
            <p className="hint">この期間に👎がありません。</p>
          ) : (
            <ul className="fb-reason-counts">
              {stats.by_reason.map((r) => (
                <li key={r.reason ?? "none"}>
                  <span>{r.reason ?? "（理由なし）"}</span>
                  <strong>{r.count}</strong>
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {/* --- L2 トリアージ ------------------------------------------------- */}
      <h3 className="stage-title">1件ずつ見る（/feedback）</h3>
      <p className="hint">
        👎 1件では、<strong>検索が外した</strong>のか
        <strong>生成が外した</strong>のか
        <strong>そもそも文書に答えが無い</strong>のかを区別できない。
        行を開いて、そのとき渡したチャンクと元の会話を確かめる。
      </p>
      <div className="fb-range">
        {[
          { id: "-1" as const, label: "👎だけ" },
          { id: "1" as const, label: "👍だけ" },
          { id: "" as const, label: "すべて" },
        ].map((r) => (
          <button
            key={r.id || "all"}
            type="button"
            className={`fb-chip ${rating === r.id ? "fb-chip-on" : ""}`}
            onClick={() => setRating(r.id)}
          >
            {r.label}
          </button>
        ))}
      </div>
      {rows === null ? (
        <p className="hint">読み込み中…</p>
      ) : rows.length === 0 ? (
        <p className="hint">この条件の評価はまだありません。</p>
      ) : (
        <>
          <p className="hint">
            {total}件中 {rows.length}件を表示（新しい順）
          </p>
          <ul className="fb-rows">
            {rows.map((row) => (
              <li key={row.id}>
                <button
                  type="button"
                  className="fb-row-head"
                  aria-expanded={openId === row.id}
                  onClick={() =>
                    setOpenId((id) => (id === row.id ? null : row.id))
                  }
                >
                  <span className="fb-row-rating">
                    {row.rating === -1 ? "👎" : "👍"}
                  </span>
                  <span className="fb-row-question">{row.question}</span>
                  {row.reason && <span className="fb-tag">{row.reason}</span>}
                  {/* 開かなくても「もう評価データセットに入れた」が分かること。
                      これが見えないと、同じ👎を二度登録しに行ってしまう。 */}
                  {row.promoted_eval_question_id !== null && (
                    <span className="fb-tag">評価用質問にした</span>
                  )}
                  <span className="fb-row-when">
                    {formatDateTime(row.created_at)}
                  </span>
                </button>
                {openId === row.id && (
                  <div className="fb-row-body">
                    <p className="fb-answer">{row.answer}</p>
                    {row.comment && (
                      <p className="fb-comment">補足: {row.comment}</p>
                    )}
                    {/* 条件を並べるのは「設定を変えたら👎が減ったか」を
                        突き合わせるため（同じ条件の👎が並ぶなら設定を疑う）。 */}
                    <p className="hint">
                      区分: {row.project ?? "（なし）"} /{" "}
                      {row.topic ?? "（なし）"} ・ 検索:{" "}
                      {row.retriever ?? "未記録"} ・ top_k:{" "}
                      {row.top_k ?? "未記録"} ・ リランク:{" "}
                      {row.reranked === null ? "未記録" : row.reranked ? "あり" : "なし"}{" "}
                      ・ 所要: {row.latency_ms === null ? "未記録" : `${row.latency_ms}ms`}
                    </p>
                    {/* ★調べた結果を残す唯一の出口★
                        ここを通さない限り、👎は読んで終わり＝次の変更で同じ
                        質問が壊れても誰も気づけない。入れた質問は以後の
                        Hit@k / MRR に毎回乗る。

                        ★昇格の入口を出すのは👎の行だけ★
                          この一覧は👍にも切り替えられる。ダイアログもここの
                          説明も「何が駄目だったか」を前提にした文言なので、
                          👍の行に出すと、押した先で言っていることと対象が
                          食い違う。既に昇格済みの行の印は評価によらず出す。 */}
                    {(row.rating === -1 ||
                      row.promoted_eval_question_id !== null) && (
                      <div className="fb-promote">
                        {row.promoted_eval_question_id === null ? (
                          <>
                            <button
                              type="button"
                              className="fb-promote-button"
                              onClick={() => setPromoting(row)}
                            >
                              評価用質問として登録
                            </button>
                            <span className="hint">
                              下の根拠を見て
                              <strong>「文書に答えはあるのに引けていない／答えが違う」</strong>
                              なら、評価データセットに入れて以後の回帰テストに乗せる。
                              <strong>そもそも文書に答えが無い</strong>ものは入れない。
                            </span>
                          </>
                        ) : (
                          <span className="hint">
                            評価用質問 #{row.promoted_eval_question_id}{" "}
                            として登録済み（③「質問集を評価」の採点対象に入っている）。
                          </span>
                        )}
                      </div>
                    )}
                    <FeedbackDetail row={row} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {/* ★開くときに初めてマウントする★
          置きっぱなしにすると、閉じている間も候補の文書を取りに行く。
          開くたびに作り直されるので、前に開いた行の入力も持ち越さない。 */}
      {promoting && (
        <PromoteModal
          row={promoting}
          scopeVersion={scopeVersion}
          onClose={() => setPromoting(null)}
          onPromoted={markPromoted}
        />
      )}
    </div>
  );
}

/** 準拠ベンチマークの一覧（サイドバーのタイトルから開く）。 */
function BenchmarkModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Modal
      open={open}
      onCancel={onClose}
      // 閉じるだけのダイアログなので OK/キャンセルは出さない
      footer={null}
      width={880}
      title="準拠している公開ベンチマーク"
    >
      <p className="hint">
        マルチモーダル各段の評価は、以下の公開ベンチマークの
        <strong>評価設計に準拠</strong>している。
        <strong>データセット自体は使っていない</strong>
        （日本語の社内文書に合わないため）。借りているのは指標と評価の組み立て方
        ＝「何を測れば良い/悪いと言えるのか」の部分。
      </p>
      {/* .table-wrap（横スクロール）は使わない。列を折り返して収めるほうが
          「さっと見る」目的に合う（bench-table 側で既定の nowrap を解いている）。 */}
      <table className="bench-table">
        <thead>
          <tr>
            <th>やること</th>
            <th>準拠ベンチマーク</th>
            <th>借りている評価設計</th>
            <th>確認できる画面</th>
          </tr>
        </thead>
        <tbody>
          {BENCHMARKS.map((b) => (
            <tr key={b.work}>
              <td>{b.work}</td>
              <td>
                {b.benches.map((bench, i) => (
                  <Fragment key={bench.name}>
                    {i > 0 && " / "}
                    <a href={bench.href} target="_blank" rel="noreferrer">
                      {bench.name}
                    </a>
                  </Fragment>
                ))}
              </td>
              <td>{b.design}</td>
              <td>{b.where}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="hint">
        詳細は README の「参考にした公開ベンチマーク」。
      </p>
    </Modal>
  );
}

const THEME_CHOICES: { id: ThemeChoice; label: string; title: string }[] = [
  { id: "auto", label: "自動", title: "OSの設定に合わせる" },
  { id: "light", label: "ライト", title: "常にライトテーマ" },
  { id: "dark", label: "ダーク", title: "常にダークテーマ" },
];

/** テーマ切り替え（サイドバー最下部）。
 *
 * 色そのものは globals.css の CSS変数が持っていて、ここは
 * <html data-theme> を差し替えるだけ。初回描画のちらつきは layout.tsx の
 * インラインスクリプトが先に属性を立てることで防いでいる。
 */
function ThemeToggle() {
  // サーバー描画の時点では localStorage を読めない＝選択が分からないので、
  // いったん既定の auto で描いてマウント後に実際の選択で上書きする。
  // 画面全体の色は init スクリプトが当て済みなので、ここがずれても
  // ちらつくのはこのボタンの選択表示だけ。
  const [choice, setChoice] = useState<ThemeChoice>("auto");

  useEffect(() => {
    setChoice(readThemeChoice());
  }, []);

  // 「自動」のときだけ OS 設定の変更に追従する（明示的に選んでいる間は無視）。
  useEffect(() => {
    if (choice !== "auto") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("auto");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [choice]);

  function pick(next: ThemeChoice) {
    setChoice(next);
    applyTheme(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // 保存できなくても今の表示は切り替わる（再訪時に既定へ戻るだけ）
    }
  }

  return (
    <div className="theme-switch">
      <span className="theme-switch-label" id="theme-switch-label">
        テーマ
      </span>
      <div
        className="theme-switch-options"
        role="group"
        aria-labelledby="theme-switch-label"
      >
        {THEME_CHOICES.map((c) => (
          <button
            key={c.id}
            type="button"
            className={c.id === choice ? "theme-option active" : "theme-option"}
            title={c.title}
            aria-pressed={c.id === choice}
            onClick={() => pick(c.id)}
          >
            {c.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** 機能を切り替える左サイドバー。
 *
 * 1ページに縦積みしていた頃は、目的の機能まで延々スクロールする必要があった。
 * 選択中のタブ以外は描画しないが、★stateはページ直下に置いたまま★なので
 * 切り替えても検索結果・チャット履歴・評価レポートは消えない。
 */
function Sidebar({
  tab,
  onTab,
  searchSubTab,
  onSearchSubTab,
  evalSubTab,
  onEvalSubTab,
}: {
  tab: TabId;
  onTab: (id: TabId) => void;
  searchSubTab: SearchSubTab;
  onSearchSubTab: (id: SearchSubTab) => void;
  evalSubTab: EvalSubTab;
  onEvalSubTab: (id: EvalSubTab) => void;
}) {
  // 配下タブを開いているか（タブidごと）。開閉は見せ方だけの話なので state は
  // ここに置く（Sidebar は常に描画されているので、タブを移動しても開閉は保たれる）。
  // 初期値 true: 配下タブがあること自体に気づけないと、そこにたどり着けない。
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({
    search: true,
    eval: true,
  });
  // 準拠ベンチマークの表（タイトルから開く）
  const [benchOpen, setBenchOpen] = useState(false);

  // 配下タブの「今どれか」と「選んだとき」を tab id で引けるようにする。
  // SUBTABS 側の id は string に均されているので、渡すときに元の型へ戻す
  // （キャストはこの2行だけに閉じ込める）。
  const groups: Record<
    string,
    { current: string; onPick: (id: string) => void }
  > = {
    search: {
      current: searchSubTab,
      onPick: (id) => onSearchSubTab(id as SearchSubTab),
    },
    eval: {
      current: evalSubTab,
      onPick: (id) => onEvalSubTab(id as EvalSubTab),
    },
  };

  return (
    <nav className="sidebar" aria-label="機能">
      {/* ページの見出しはここ1つ。本文側は各機能の h2 から始まる。
          押すと準拠ベンチマークの表が出る（README を開かずに確認できるように）。
          ★button は h1 の中に置く★ 逆に button で h1 を包むのはHTML的に
          許されない（button の中身は phrasing content だけ）うえ、
          ページの見出しが消えてしまう。 */}
      <div className="sidebar-brand">
        <h1>
          <button
            type="button"
            className="sidebar-brand-button"
            title="準拠している公開ベンチマークを表で見る"
            onClick={() => setBenchOpen(true)}
          >
            RAG Inspector
          </button>
        </h1>
        <span>RAG検証ラボ</span>
      </div>
      <BenchmarkModal open={benchOpen} onClose={() => setBenchOpen(false)} />
      <ul className="sidebar-tabs">
        {TABS.map((t) => {
          // 配下タブを持つのは ②③。開閉ボタンもそのタブにだけ付く。
          const subtabs = SUBTABS[t.id];
          const hasSubtabs = subtabs !== undefined;
          const open = openGroups[t.id] ?? true;
          const group = groups[t.id];
          return (
            <li key={t.id}>
              {/* 開閉ボタンはタブ本体と★兄弟★にする（button は入れ子にできない）。
                  行としては1つに見えるよう .sidebar-tab-row で横に並べる。 */}
              <div className={hasSubtabs ? "sidebar-tab-row" : undefined}>
                <button
                  type="button"
                  className={t.id === tab ? "sidebar-tab active" : "sidebar-tab"}
                  // 何をする画面かの説明（TABS.desc）。★開く前に読める★のが要点で、
                  // ②と③のどちらを開けばいいかをここで決められるようにする。
                  title={t.desc}
                  // "page" ではなく "true"。ページ遷移はしておらず同一ページ内の
                  // 表示切替なので、aria-current の汎用値（=その集合の現在の項目）が
                  // 実態に合う。
                  aria-current={t.id === tab ? "true" : undefined}
                  onClick={() => {
                    onTab(t.id);
                    // 配下を持つタブを選んだら配下も開く。畳んだまま選んで
                    // 「切り替え先が見えない」状態になるのを防ぐ。
                    if (hasSubtabs)
                      setOpenGroups((g) => ({ ...g, [t.id]: true }));
                  }}
                >
                  <span className="sidebar-tab-label">{t.label}</span>
                  <code className="sidebar-tab-hint">{t.hint}</code>
                </button>
                {hasSubtabs && (
                  <button
                    type="button"
                    className={[
                      "sidebar-tab-toggle",
                      open ? "open" : "",
                      // 親タブが選択中のときは同じ塗りにして1つのタブに見せる
                      t.id === tab ? "on-active" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    // 表示は「>」だけなので、何を開くボタンなのかを読み上げに補う
                    aria-label={
                      open ? `${t.label}の配下を閉じる` : `${t.label}の配下を開く`
                    }
                    aria-expanded={open}
                    aria-controls={`sidebar-${t.id}-subtabs`}
                    onClick={() =>
                      setOpenGroups((g) => ({ ...g, [t.id]: !open }))
                    }
                  >
                    {/* 三角の向きは CSS の回転で変える（開=下・閉=右） */}
                    <span aria-hidden="true">›</span>
                  </button>
                )}
              </div>
              {/* 配下タブは開いている間だけ出す。親タブ以外を見ているときでも
                  出しておき、そこから直接飛べるようにする（押したら親に移る）。 */}
              {subtabs && open && group && (
                <ul className="sidebar-subtabs" id={`sidebar-${t.id}-subtabs`}>
                  {subtabs.map((s) => {
                    const here = tab === t.id && s.id === group.current;
                    return (
                      <li key={s.id}>
                        <button
                          type="button"
                          className={
                            here ? "sidebar-subtab active" : "sidebar-subtab"
                          }
                          aria-current={here ? "true" : undefined}
                          onClick={() => {
                            onTab(t.id);
                            group.onPick(s.id);
                          }}
                        >
                          <span className="sidebar-tab-label">{s.label}</span>
                          <code className="sidebar-tab-hint">{s.hint}</code>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      <ThemeToggle />
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
  // ② の中で見ている面。既定は「質問で資料を検索」＝ ② の本題。
  // 保管質問の検証は、②で質問を投げて貯まってから使うもの。
  const [searchSubTab, setSearchSubTab] = useState<SearchSubTab>("stages");
  // ③ の中で見ている面。既定は「質問集を評価」＝ ③ の本題（スコアを見る）。
  // 質問を足すのは準備なので、必要なときに配下タブで開く。
  const [evalSubTab, setEvalSubTab] = useState<EvalSubTab>("run");

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
  // 登録フォームを開いているか。文書タブは登録・区分追加・一覧を縦に並べるので、
  // 一覧だけを見たいときにドロップゾーンを畳めるようにする。
  // ★state はここ（Home）★ 畳んだままタブを移動して戻っても畳んだままにする。
  const [ingestFormOpen, setIngestFormOpen] = useState(true);
  // 一覧の「上の登録フォームへ」から戻ってくる先。
  const ingestFormRef = useRef<HTMLDivElement>(null);
  // --- 文書一覧（「入っている文書」= /documents/summary）---
  // 見るだけの画面だが、絞り込みと並び順は他パネルと同じくここに置く
  // （タブを移動して戻ったときに「すべて・新しい順」へ戻らないように）。
  // トピックの候補はパネル側で引く（選択中のプロジェクト配下だけ・他パネルと同じ）
  const [listProject, setListProject] = useState("");
  const [listTopic, setListTopic] = useState("");
  const [listSortKey, setListSortKey] = useState<DocSortKey>("created_at");
  const [listSortDesc, setListSortDesc] = useState(true);

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
  // 👎の理由の選択肢。回答ごとに取り直す必要は無いので1回だけ引く。
  const feedbackReasons = useFeedbackReasons();

  // --- 評価パネル（/eval = 質問集で Hit@k / MRR を測る）---
  const [evalSelected, setEvalSelected] = useState<string[]>([]);
  const [evalRerank, setEvalRerank] = useState(false);
  const [evalProject, setEvalProject] = useState("");
  const [evalTopic, setEvalTopic] = useState("");
  const evalTopics = useTopics(evalProject, scopeVersion);

  // --- 保管質問の検証（②の配下 /verify = ②で検索した質問をまとめて引き直す）---
  // ★評価(③)とは別タブ・別state★
  //   扱うテーブルが違う（saved_questions / eval_questions）うえ、正解ラベルの
  //   要否も出力も別物なので機能として分けてある。区分セレクタを③と共用すると、
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
  // 「質問が0件」は GET /eval が 404 で返す（n=0 の空レポートではなくなった）。
  // ★エラーとは別の state に持つ★ 失敗ではなく「まだ登録していないだけ」なので、
  // 赤いエラー表示ではなく案内（.empty-note）として出す。
  const [evalEmpty, setEvalEmpty] = useState("");
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
  // 正解に指定できる文書の候補。選んでいる区分で絞る（scopeVersion は取り込みでも
  // 上がるので、①で文書を入れたらここの候補にもすぐ出る）。
  const newQDocs = useDocuments(newQProject, newQTopic, scopeVersion);
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
    setEvalEmpty("");
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
        setEvalReport(null);
        // 404 = 測る対象が無い。区分で絞って0件か、そもそも1件も無いかで
        // 文面はバックエンドが出し分けているので、そのまま案内として出す。
        if (res.status === 404) setEvalEmpty(err);
        else setEvalError(err);
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

  /** 一覧の「上の登録フォームへ」から呼ぶ。畳んでいたら開いてから送る。 */
  function focusIngestForm() {
    setIngestFormOpen(true);
    // 開くのは次の描画なので、要素が出てからスクロールする
    requestAnimationFrame(() =>
      ingestFormRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
    );
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
      // 検索が通ると質問が保管されるので、②「保管質問をまとめて再検索」の件数を取り直す
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
    const project = chatProject.trim();
    const topic = chatTopic.trim();
    setMessages((m) => [
      ...m,
      { role: "user", text: q },
      { role: "bot", text: "", question: q, project, topic },
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
          project: project || null,
          topic: topic || null,
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
        patchLastMessage({
          sources: data.sources,
          citations: data.citations,
          // 画面には出さない。👍/👎 に添えて送るため抱えておく
          conversationId: data.conversation_id,
          retrieval: data.retrieval,
        });
      } else if (name === "delta") {
        answer += data.text;
        patchLastMessage({ text: answer });
      } else if (name === "done") {
        // 回答IDと所要時間は生成が終わらないと決まらないので done で届く
        patchLastMessage({
          messageId: data.message_id,
          latencyMs: data.latency_ms,
        });
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
          // ★どういう条件で出た回答かを一緒に残す★
          //   本文だけだと「この設定変更で👎が減った」「👎のとき正解は何位に
          //   居たのか」が後から追えない。値はサーバが meta/done で返したものを
          //   そのまま戻すだけ（クライアントで組み立てない）。
          conversation_id: msg.conversationId ?? null,
          message_id: msg.messageId ?? null,
          retriever: msg.retrieval?.retriever ?? null,
          top_k: msg.retrieval?.top_k ?? null,
          reranked: msg.retrieval?.reranked ?? null,
          // 順位は citations の並びそのもの（[n] の n = 配列の位置+1）。
          // chunk_ids を別に受け取らないのは、二重に持つと片方だけズレるため。
          chunk_ids: (msg.citations ?? []).map((c) => c.chunk_id),
          latency_ms: msg.latencyMs ?? null,
          // 区分だけは meta の戻しではなく、質問したときの選択（未選択は null）。
          // これが無いと「この区分だけ👎率が高い」を後から出せない。
          project: msg.project || null,
          topic: msg.topic || null,
        }),
      });
      if (!res.ok) throw new Error(String(res.status));
      // 記録できたIDを覚えておく。👎の理由はこの行に後から足す。
      const { id } = await res.json();
      setMessages((m) =>
        m.map((x, i) => (i === index ? { ...x, feedbackId: id } : x)),
      );
    } catch {
      // 失敗したら印を戻して再送できるようにする
      setMessages((m) =>
        m.map((x, i) => (i === index ? { ...x, rating: undefined } : x)),
      );
    }
  }

  /** 記録済みの👎に理由・補足を後から足す（PATCH /feedback/{id}）。
   *
   * 失敗しても👎そのものは残っているので、印は戻さない（戻すと「押せていない」と
   * 誤解させる）。理由が付かなかっただけの状態になる。
   */
  async function sendReason(
    index: number,
    patch: { reason?: string; comment?: string },
  ) {
    const msg = messages[index];
    if (!msg?.feedbackId) return;
    if (patch.reason) {
      setMessages((m) =>
        m.map((x, i) => (i === index ? { ...x, reason: patch.reason } : x)),
      );
    }
    try {
      await fetch(`/api/backend/feedback/${msg.feedbackId}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(patch),
      });
    } catch {
      // 通信できなくても👎は記録済み。ここで出せる復旧手段が無いので黙って諦める。
    }
  }

  return (
    <div className="layout">
      <Sidebar
        tab={tab}
        onTab={setTab}
        searchSubTab={searchSubTab}
        onSearchSubTab={setSearchSubTab}
        evalSubTab={evalSubTab}
        onEvalSubTab={setEvalSubTab}
      />

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

      {/* 区分を作る入口。文書には触らない（マスタだけを増やす）ので、
          下の文書のパネルとは別の箱にして、文書の作業に入る前に置く。 */}
      {tab === "ingest" && (
        <ScopeCreatePanel onCreated={() => setScopeVersion((v) => v + 1)} />
      )}

      {/* 書き込みフロー: text → chunk → embed → pgvector

          ★「登録する」と「何が入ったか見る」は1枚のパネルに収める★
          この2つは同じ作業の続きなので、箱を分けると箱の間の余白ぶんだけ
          縦に伸び、登録してから一覧を見るまでに余計なスクロールが要る。
          仕切りは箱ではなく小見出しと罫線で表す。 */}
      {tab === "ingest" && (
      <section className="panel">
      <div className="panel-section" ref={ingestFormRef}>
        {/* ★畳めるようにしてある★
            一覧だけを見たい回にドロップゾーンを読み飛ばさずに済むよう、
            登録フォームは畳んで下を引き上げられるようにする。 */}
        <div className="panel-head">
          <h3 className="panel-section-title">
            2. 新たに文書を登録（/ingest-file・Voyageキー必要）
          </h3>
          <button
            type="button"
            className="panel-toggle"
            aria-expanded={ingestFormOpen}
            onClick={() => setIngestFormOpen((o) => !o)}
            title={
              ingestFormOpen
                ? "登録フォームを畳んで、下の一覧を引き上げる"
                : "登録フォームを開く"
            }
          >
            {ingestFormOpen ? "畳む" : "開く"}
          </button>
        </div>

        {!ingestFormOpen && (
          <p className="hint">
            登録フォームは畳んでいます。文書を入れるときは「開く」を押してください。
          </p>
        )}

        {ingestFormOpen && (
        <>
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
          文書を入れずに区分だけ作るときは、上の「新たに区分を追加」を使います。
        </p>

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
        </>
        )}
      </div>

      {/* 読み出し側の確認: 今そのプロジェクトに何がどう入っているか。
          scopeVersion は取り込みが終わると増えるので、上で登録すればここも
          自動で引き直される（＝入ったことがその場で見える）。 */}
      <DocumentListPanel
        projects={projects}
        scopeVersion={scopeVersion}
        project={listProject}
        topic={listTopic}
        onProject={setListProject}
        onTopic={setListTopic}
        sortKey={listSortKey}
        sortDesc={listSortDesc}
        onSort={(key, desc) => {
          setListSortKey(key);
          setListSortDesc(desc);
        }}
        onAddDocuments={focusIngestForm}
      />
      </section>
      )}

      {/* 検索の内訳: Claudeを呼ばないのでAnthropicキー不要 */}
      {tab === "search" && searchSubTab === "stages" && (
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
            この上位チャンクが、そのまま <strong>④ 会話する</strong> の回答生成で根拠として使われる。
            <br />
            <br />
            ここで検索した質問は、区分と一緒に自動で保管される。まとめて引き直すのは
            隣の<strong>保管質問をまとめて再検索</strong>（②の配下）。
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
          idPrefix="search"
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
        <h2>④ 会話する（/chat/stream・Voyage + Anthropicキー必要）</h2>
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
          idPrefix="chat"
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
              {/* 理由を聞くのは👎だけ。👍にも聞くと催促が増えて満足度が下がる。
                  記録できた（feedbackId がある）ときだけ出す＝足す先がある状態。 */}
              {m.rating === -1 && m.feedbackId && (
                <ReasonPicker
                  reasons={feedbackReasons}
                  picked={m.reason}
                  onPick={(reason) => sendReason(i, { reason })}
                  onComment={(comment) => sendReason(i, { comment })}
                />
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
      {/* 👍/👎 は③の配下だが、上の見出し（正解ラベル付きの質問集を採点する）とは
          扱うものが違うので、同じ section には入れず別のパネルにする。 */}
      {tab === "eval" && evalSubTab === "feedback" && (
        <section className="panel">
          <h2>
            <Tip label="③ 👍/👎 を見る">
              ④で回答に付いた<strong>👍/👎</strong>を、集計（どこが怪しいか）と
              一覧（1件ずつ何が起きたか）で見る。
              <br />
              <br />
              ★<strong>生の👎は改善のインプットにならない</strong>★
              1件の👎からは、検索が外したのか・生成が外したのか・そもそも文書に
              答えが無かったのかを区別できない。ここは
              <strong>調べに行く先を絞り込むためのフィルタ</strong>として使う。
              <br />
              <br />
              ③の評価（Hit@k / MRR）は<strong>人が用意した正解</strong>で測るのに対し、
              こちらは<strong>実際に聞かれた質問に利用者が付けた評価</strong>。
              正解ラベルが無い代わりに、現場で何が起きているかが出る。
            </Tip>
            （/feedback・キー不要）
          </h2>
          <FeedbackPanel projects={projects} scopeVersion={scopeVersion} />
        </section>
      )}

      {tab === "eval" && evalSubTab !== "feedback" && (
      <section className="panel">
        <h2>
          <Tip label="③ 評価する">
            登録済みの<strong>質問集（正解ラベル付き）</strong>を一気に検索して、
            <strong>どれだけ正解文書を上位で拾えたか</strong>を集計する。
            <br />
            <br />
            <strong>② 検索の内訳</strong> が「1問を深く見る」のに対し、③ は
            「質問集<strong>全体</strong>で当たるか」を見る。
            手法やリランクを変えて<strong>数字が上がるか下がるか</strong>で改良の効果を判定できる。
            <br />
            <br />
            質問はプロジェクト・トピックごとに分けて登録できる（<code>POST /eval-questions</code>）。
            まだ無ければ <code>python -m app.eval --seed</code> でサンプルを投入。
            <br />
            <br />
            正解ラベルを用意する前に並びだけ見たいときは ②の配下の
            <strong>保管質問をまとめて再検索</strong>。あちらは②で貯まった質問を採点せずに一覧する。
          </Tip>
          （/eval・Voyageキー必要 / リランク時のみAnthropic）
        </h2>

        {/* ★③と②「保管質問をまとめて再検索」の違いは常に見えるところに出す★
            説明は上の Tip にも書いてあるが、Tipは開かないと読めないので
            「どっちがどっちだったか」を毎回思い出せない。2つのタブを行き来する
            たびに読み返す種類の情報なので、開かずに読める位置に1行で置く。 */}
        <p className="hint panel-note">
          ここは<strong>正解ラベル付きの質問集</strong>（<code>eval_questions</code>）を
          Hit@k / MRR で<strong>採点</strong>する場所。
          ②の検索で自動的に貯まった質問（<code>saved_questions</code>）を、
          採点せずに並びだけ確かめたいときは ②の配下の<strong>保管質問をまとめて再検索</strong>。
        </p>

        {/* 評価用の質問を登録する（正解ラベル付き）。
            ★入力の順番は「区分 → 質問 → 正解文書」★ 正解文書の候補を区分で
            絞るので、絞る材料の区分を先に置く。区分が後ろにあると、候補を
            絞り込めないまま長い一覧から文書を探すことになる。 */}
        {evalSubTab === "add" && (
        <div className="eval-add">
          <h3 className="stage-title">評価用の質問を登録（/eval-questions）</h3>
          <ScopeSelect
            idPrefix="newq"
            project={newQProject}
            topic={newQTopic}
            projects={projects}
            topics={newQTopics}
            onProject={(v) => {
              setNewQProject(v);
              // 区分を変えると下の文書候補が入れ替わる。選び直させないと
              // 「その区分では引けない文書」を正解に指定した質問ができる。
              setNewExpected("");
            }}
            onTopic={(v) => {
              setNewQTopic(v);
              setNewExpected("");
            }}
          />
          <p className="hint">
            区分を選ぶと、下の<strong>正解の文書</strong>もその区分の文書だけになります
            （「すべて」なら全件）。評価は<strong>同じ区分の文書だけを検索</strong>するので、
            区分をまたいだ組み合わせは正解になりません。
          </p>
          <input
            placeholder="質問（例: 有給は入社何ヶ月で何日？）"
            value={newQ}
            onChange={(e) => setNewQ(e.target.value)}
          />
          <div className="scope-field">
            <label className="scope-label" htmlFor="newq-expected">
              正解の文書（この文書が上位に来れば正解）
            </label>
            {/* ★手入力させない★ ここは documents.source を指す値なので、実在
                しない名前を入れるとその設問は何をやっても不正解になる（引ける
                文書が無いので当然当たらない）。候補から選ぶ形にして防ぐ。 */}
            <Select
              id="newq-expected"
              value={newExpected || undefined}
              // 取得中（null）は「文書が無い」と言い切らない。区分を変えた直後は
              // ここで一旦候補が空になり、取り直しが終わるまで選べない。
              placeholder={
                newQDocs === null
                  ? "文書を読み込み中…"
                  : newQDocs.length === 0
                    ? "この区分に文書がありません（① 文書を登録 から）"
                    : "文書を選ぶ（例: 有給休暇.txt）"
              }
              options={toOptions(newQDocs ?? [])}
              onChange={(v?: string) => setNewExpected(v ?? "")}
              disabled={newQDocs === null || newQDocs.length === 0}
              allowClear
              showSearch
              listHeight={LIST_HEIGHT}
            />
          </div>
          {/* ★畳んでおく★ 使うのは「分割やcontextualの改良を測りたい」ときだけの
              レアケースで、ふつうの設問は空欄（＝文書単位）で足りる。開いたままだと
              説明文が下の「メモ」への説明に見えてしまうので、入力欄ごとこの中へ
              入れて「どの欄の説明か」を枠と位置で示す。 */}
          <details className="q-optional">
            <summary>
              チャンク単位で採点する（任意・ふつうは空欄のままでよい）
              {/* 畳んだ状態でも入力済みなら値を出す。隠れたまま登録されると
                  「文書単位のつもりが違った」に後から気づけない。 */}
              {newExpectedText.trim() && (
                <span className="q-optional-value">
                  {newExpectedText.trim()}
                </span>
              )}
            </summary>
            <div className="hint q-optional-body">
              <p>
                <strong>チャンクとは</strong>
                ：文書は登録時に一定の長さで分割されて保存されます。その1つ1つがチャンクで、
                <strong>検索が返すのも、回答の根拠になるのもこの単位</strong>です
                （「就業規則.txt」1件が5チャンクに割れている、といった状態）。
              </p>
              <p>
                <strong>空欄＝文書単位で採点（既定）</strong>
                ：正解の文書のチャンクが1つでも上位に来れば正解にします。手軽ですが、
                長い文書だと<strong>同じ文書の関係ない段落</strong>を引いても正解になるので、
                分割の仕方や contextual retrieval を改良しても
                <strong>数字が動きません</strong>。
              </p>
              <p>
                <strong>語句を入れる＝チャンク単位で採点</strong>
                ：その語句を含むチャンクを引けたときだけ正解にします。
                「この一節を引けるか」をピンポイントで測るので、上の改良の効果が数字に出ます。
              </p>
              <p>
                <strong>語句の選び方</strong>
                ：正解にしたい一節にしか出てこない言い回しを、本文からそのまま写します。
                判定は<strong>部分一致</strong>で、空白と改行は無視して比べるので、
                本文が途中で改行されていても当たります。文書名の一致も同時に見るため、
                同じ言い回しが別の文書にあっても誤って正解にはなりません。
              </p>
            </div>
            <input
              placeholder="正解チャンクに含まれる語句（例: 1日2時間を超える場合）"
              value={newExpectedText}
              onChange={(e) => setNewExpectedText(e.target.value)}
            />
          </details>
          <input
            placeholder="メモ（任意・何を確かめる質問か）"
            value={newQNote}
            onChange={(e) => setNewQNote(e.target.value)}
          />
          <button onClick={addEvalQuestion} disabled={addingQ || !newExpected}>
            {addingQ ? "登録中…" : "質問を追加"}
          </button>
          {addQStatus && <p className="hint">{addQStatus}</p>}
        </div>
        )}

        {/* 評価対象の絞り込みと手法選択 */}
        {evalSubTab === "run" && (
        <>
        <div className="eval-controls">
          <ScopeSelect
            idPrefix="eval"
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

        {/* 質問0件の案内（GET /eval の 404）。文面はバックエンド側にある
            ＝ CLI(app.compare)とも同じことを言う。 */}
        {evalEmpty && <p className="empty-note">{evalEmpty}</p>}

        {evalReport && (
            <>
              {/* 集計スコア（大きく表示） */}
              <div className="eval-score">
                <div className="eval-metric">
                  <span className="eval-metric-value">
                    {evalReport.hit_at_k.toFixed(3)}
                  </span>
                  <span className="eval-metric-label">
                    <Tip label={`Hit@${evalReport.top_k}`}>
                      上位{evalReport.top_k}件に正解が<strong>入ったか</strong>だけを見て、
                      入った質問の割合を出した値。1.0 = 全問で拾えている。
                      <br />
                      <br />
                      何位で拾えたかは見ないので、<strong>1位でも{evalReport.top_k}位でも
                      同じ1点</strong>。並び順を良くする改良（リランクなど）は
                      この数字には出にくい ＝ 隣の MRR で見る。
                    </Tip>
                    （上位{evalReport.top_k}件に正解が入った割合）
                  </span>
                </div>
                <div className="eval-metric">
                  <span className="eval-metric-value">
                    {evalReport.mrr.toFixed(3)}
                  </span>
                  <span className="eval-metric-label">
                    <Tip label="MRR">
                      正解を<strong>何位で</strong>拾えたかの平均点。
                      1位なら1、2位なら0.5、3位なら0.33…（順位の逆数）を全問で平均する。
                      1.0 = 全問で正解が1位。
                      <br />
                      <br />
                      たとえば2問で「1位・3位」なら (1 + 0.33) / 2 = 0.67。
                      圏外（上位{evalReport.top_k}件に無い）の質問は0点として数える。
                      <br />
                      <br />
                      隣の <strong>Hit@{evalReport.top_k}</strong> が「入ったか / 入らないか」
                      なのに対し、こちらは<strong>順位の良さ</strong>を見る。
                      Hit@{evalReport.top_k}が同じでMRRだけ上がったら、
                      「拾える文書は同じだが、より上位に来るようになった」という意味。
                    </Tip>
                    （正解順位の逆数平均・1.0が満点）
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
          )}
        </>
        )}
      </section>
      )}

      {/* 保管質問の検証フロー: saved_questions → 各問を検索 → 上位k件を一覧
          ★③の評価とは別物★ あちらは eval_questions（正解ラベル必須）を数値で
          採点する。こちらは②の検索で自動的に貯まった質問を、正解ラベル無しで
          「今の設定だと何が上位に来るか」目視で確かめるための道具。 */}
      {tab === "search" && searchSubTab === "verify" && (
      <section className="panel">
        <h2>
          {/* 見出しはサイドバーの配下タブ名と一字一句そろえる。ずれていると
              「今どの画面に居るのか」を名前で確かめられない。 */}
          <Tip label="② 保管質問をまとめて再検索">
            ② の「質問で資料を検索」で検索すると、そのときの<strong>プロジェクト・トピックと
            一緒に質問が自動で保管</strong>されます（同じ区分の同じ質問は重ねません）。
            <br />
            <br />
            ここでは保管済みの質問を<strong>まとめて引き直し</strong>、各質問の上位4件と
            RRFスコアを一覧で確認できます。
            <br />
            <br />
            ③ の評価とは<strong>別のデータ</strong>を見ています。③ は正解ラベル付きの
            質問集（<code>eval_questions</code>）を Hit@k / MRR で採点するもの。
            こちらは正解ラベルを持たない実際に聞かれた質問（<code>saved_questions</code>）
            なので○×は付きません。正解を用意する前でも並びを確かめられるのが利点です。
          </Tip>
          （/verify・Voyageキー必要）
        </h2>

        {/* ③ 側と対になる1行（あちらの panel-note と揃えてある）。
            どちらのタブから来ても、開かずに違いが読めるようにしておく。 */}
        <p className="hint panel-note">
          ここは②の検索で<strong>自動的に貯まった質問</strong>（
          <code>saved_questions</code>）を引き直して、
          <strong>並びだけ</strong>を確かめる場所（正解ラベルが無いので○×は付かない）。
          正解ラベル付きの質問集を数字で採点したいときは <strong>③ 評価する</strong>。
        </p>

        <div className="eval-controls">
          <ScopeSelect
            idPrefix="verify"
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
