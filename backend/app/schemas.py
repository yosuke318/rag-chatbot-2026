"""API の入出力スキーマ（Pydantic）。

ここで宣言した型が FastAPI により OpenAPI スキーマ(/openapi.json)へ出力され、
フロントはそれを openapi-typescript で TypeScript 型に変換して使う。
＝ FE と BE で型定義の実体を1つに保つ（手書きの二重管理をなくす）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# --- リクエスト ---------------------------------------------------------------


class IngestRequest(BaseModel):
    source: str = Field(description="文書名（例: 有給休暇.txt）")
    text: str = Field(description="本文")
    project: Optional[str] = Field(default=None, description="プロジェクト（任意）")
    topic: Optional[str] = Field(default=None, description="トピック（任意）")


class ChatRequest(BaseModel):
    question: str = Field(description="質問文")
    conversation_id: Optional[int] = Field(
        default=None,
        description="続きを話す会話のID。null=新しい会話を始める（IDは応答に入る）",
    )
    # 回答の根拠を「この区分の文書だけ」に限る。未指定＝全文書から探す。
    project: Optional[str] = Field(
        default=None, description="プロジェクト（任意。指定するとその区分の文書だけを根拠にする）"
    )
    topic: Optional[str] = Field(
        default=None, description="トピック（任意。指定するとその区分の文書だけを根拠にする）"
    )


class PublicChatRequest(BaseModel):
    """公開API(/v1/chat)の入力。

    ★project を持たない★ 検索範囲はAPIキーに紐づくプロジェクトで決まり、
    リクエストからは指定させない（指定できると分離が破れる）。
    余計なキーは黙って無視せず弾く（extra="forbid"）: project を送って
    「効いているつもり」になるのが一番危ないため、その場で気づけるようにする。
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(description="質問文")
    conversation_id: Optional[int] = Field(
        default=None,
        description="続きを話す会話のID。null=新しい会話（★自分のキーで始めた会話のみ継続可）",
    )
    topic: Optional[str] = Field(
        default=None,
        description="トピック（任意）。キーのプロジェクト内をさらに絞る",
    )


class FeedbackRequest(BaseModel):
    """回答への 👍/👎。評価(eval)のQA候補として貯める。"""

    question: str = Field(description="評価対象の質問")
    answer: str = Field(description="評価対象の回答")
    rating: int = Field(description="+1 = 👍 / -1 = 👎")
    sources: List[str] = Field(default_factory=list, description="回答の根拠に使った出典")
    comment: Optional[str] = Field(default=None, description="自由記述（任意）")


class SavedQuestionRequest(BaseModel):
    """保管する質問1件（正解ラベルなし）。②の検索時は自動で保管されるので、
    これは「検索せずに質問だけ足したい」場合の入口。"""

    question: str = Field(description="保管する質問")
    project: Optional[str] = Field(default=None, description="プロジェクト（任意）")
    topic: Optional[str] = Field(default=None, description="トピック（任意）")


class EvalQuestionRequest(BaseModel):
    """評価用の質問1件（正解ラベル付き）。プロジェクト・トピックごとに分けて登録できる。"""

    question: str = Field(description="評価する質問")
    expected_source: str = Field(description="正解の文書名（この文書が上位に来れば正解）")
    project: Optional[str] = Field(default=None, description="プロジェクト（未指定は共通）")
    topic: Optional[str] = Field(default=None, description="トピック（未指定は共通）")
    note: Optional[str] = Field(default=None, description="何を確かめる質問かのメモ（任意）")


# --- レスポンス ---------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str


class IngestResponse(BaseModel):
    source: str
    chunks_created: int = Field(
        description="作成したチャンク数（skipped=true のときは既存のチャンク数）"
    )
    replaced: int = Field(description="置き換えた既存文書の件数（0なら新規）")
    skipped: bool = Field(
        description="内容が既存と同じで、埋め込みをやり直さずに済ませたか"
    )
    # 常にサーバー側が値を入れる（画像を持たない登録は0）ので Optional にはしない。
    # default=0 を置くのは、テキスト貼り付け登録のように画像の概念が無い経路で
    # 呼び出し側がこのキーを組み立てなくて済むようにするため。
    images_stored: int = Field(
        default=0,
        description="文書から抽出してS3に保存した画像の枚数（skipped=true でも保存する）",
    )


class StageHit(BaseModel):
    """ある検索手法1つの中でのヒット。手法によらず同じ形にしてある。"""

    rank: int
    id: int
    source: str
    metric_value: float = Field(
        description="その手法が計算した生スコア（cos類似度 / 字面類似度 / BM25スコア）"
    )
    preview: str


class RetrieverStage(BaseModel):
    """検索手法1つ分のランキング（融合前）。"""

    name: str = Field(description="手法の識別子: vector / trgm / bm25")
    label: str = Field(description="表示名")
    metric_label: str = Field(description="metric_value の表示名（cos類似度 等）")
    hits: List[StageHit]


class Contribution(BaseModel):
    """融合結果1件に対する、各検索手法からの寄与。

    RRFは「どの手法が何位に置いたか」の足し合わせなので、
    手法ごとの内訳を持たせて寄与を追えるようにする。
    """

    retriever: str
    rank: Optional[int] = Field(description="null = この手法のリストに出てこなかった")
    metric_value: Optional[float] = Field(description="その手法の生スコア")
    rrf_term: Optional[float] = Field(
        description="この手法が寄与したRRFスコア 1/(k + 順位 + 1)"
    )


class FusedHit(BaseModel):
    """RRFで融合した後の最終順位。"""

    rank: int
    id: int
    source: str
    score: float = Field(description="RRFスコア。各手法の rrf_term の合計")
    contributions: List[Contribution] = Field(
        description="検索手法ごとの内訳。手法を増やすと要素が増える"
    )
    preview: str


class ParamSpec(BaseModel):
    """調整可能なパラメータの仕様。UIの入力欄はこれを元に生成する。"""

    name: str
    label: str
    default: float
    min: float
    max: float
    step: float
    description: str


class RetrieverInfo(BaseModel):
    """選択可能な検索手法（UIの切り替え用）。"""

    name: str
    label: str
    metric_label: str
    # 常に返すので必須。任意にすると生成TS型が undefined を含み扱いづらくなる
    params: List[ParamSpec] = Field(description="この手法で調整できる定数（空配列もあり）")


class AppliedParams(BaseModel):
    """実際に計算へ使われた値（未指定なら既定が入る）。"""

    rrf_k: int
    retrievers: Dict[str, Dict[str, float]]


class RetrieversResponse(BaseModel):
    """選択可能な検索手法と、設定されている既定。"""

    available: List[RetrieverInfo]
    default: List[str] = Field(description="環境変数 RETRIEVERS の値")
    fusion_params: List[ParamSpec] = Field(description="融合そのもののパラメータ（RRF k）")


class ProjectsResponse(BaseModel):
    """登録済みのプロジェクト一覧（UIのセレクタ用）。"""

    projects: List[str] = Field(
        description="文書または評価用質問に付いている project（NULL=共通は含まない）"
    )


class TopicsResponse(BaseModel):
    """登録済みのトピック一覧（UIのセレクタ用）。"""

    topics: List[str] = Field(
        description="?project= を付けるとそのプロジェクト配下のトピックだけになる"
    )


class SearchResponse(BaseModel):
    """検索の各段階（Claudeを呼ばない）。

    検索手法の本数に依らない形にしてあるので、BM25等を足しても構造は変わらない。
    """

    question: str
    retrievers: List[str] = Field(description="この検索で使った手法の並び")
    available_retrievers: List[RetrieverInfo] = Field(
        description="指定可能な手法の一覧。/search?retrievers=a,b で選べる"
    )
    applied_params: AppliedParams = Field(description="この検索で実際に使われた定数")
    lexical_min_similarity: float = Field(
        description="これ未満の字面ヒットはRRFに渡さない閾値（trgm手法用・後方互換）"
    )
    stages: List[RetrieverStage] = Field(description="融合前の各手法のランキング")
    fused: List[FusedHit]


class Citation(BaseModel):
    """回答の根拠に使ったチャンク1件。

    出典名（文書）だけでは「その文書のどこか」までしか分からず、利用者が回答を
    自分で検証できない。チャンクのid・該当箇所・原本URLまで返して、
    回答中の [n] から根拠そのものへ辿れるようにする。
    """

    n: int = Field(description="回答本文の引用マーカー [n] に対応する番号（1始まり）")
    chunk_id: int = Field(description="根拠チャンクのid（chunks.id）")
    source: str = Field(description="出典の文書名")
    preview: str = Field(description="該当箇所のプレビュー（本文の冒頭を抜粋）")
    file_url: Optional[str] = Field(
        default=None,
        description="原本を開くURL（実S3なら署名URL / ローカルは中継URL）。null=原本なし",
    )


class ChatResponse(BaseModel):
    answer: str = Field(
        description="回答本文。各文の末尾に根拠を指す引用マーカー [n] が付く"
    )
    conversation_id: int = Field(
        description="この発言が属する会話のID。次の質問にこれを渡すと履歴が効く"
    )
    sources: List[str] = Field(description="根拠に使ったチャンクの出典（重複排除済み）")
    citations: List[Citation] = Field(
        default_factory=list,
        description="回答の根拠に使ったチャンク（[n] の n はこの並びの1始まりの位置）",
    )


class FeedbackResponse(BaseModel):
    id: int = Field(description="保存したフィードバックのID")
    rating: int = Field(description="記録した評価（+1 / -1）")


class EvalQuestion(BaseModel):
    """登録済みの評価用質問1件。"""

    id: int
    question: str
    expected_source: str
    project: Optional[str] = None
    topic: Optional[str] = None
    note: Optional[str] = None


class EvalQuestionsResponse(BaseModel):
    """評価用質問の一覧（会社・部署で絞り込める）。"""

    questions: List[EvalQuestion]


class SavedQuestion(BaseModel):
    """②で検索したときに保管された質問1件（正解ラベルは持たない）。"""

    id: int
    question: str
    project: Optional[str] = None
    topic: Optional[str] = None


class SavedQuestionsResponse(BaseModel):
    questions: List[SavedQuestion]


class SavedQuestionResponse(BaseModel):
    """保管の結果。既に同じ質問があれば saved=false（エラーではない）。"""

    saved: bool = Field(description="新しく保管したか。false=同じ区分に同じ質問が既にある")
    question: str
    project: Optional[str] = None
    topic: Optional[str] = None


class VerifyResult(BaseModel):
    """保管質問1件の検証結果。正解ラベルが無いので○×は付かない。"""

    question: str
    project: Optional[str] = None
    topic: Optional[str] = None
    fused: List[FusedHit] = Field(description="RRF融合後の上位k件（②の表と同じ形）")


class VerifyReport(BaseModel):
    """保管質問すべての検証結果。UIの一覧はこれを描画する。"""

    n: int = Field(description="検証した質問数")
    top_k: int
    project: Optional[str] = Field(default=None, description="絞り込んだプロジェクト（null=全件）")
    topic: Optional[str] = Field(default=None, description="絞り込んだトピック（null=全件）")
    results: List[VerifyResult]


class EvalResult(BaseModel):
    """評価1問分の結果。"""

    question: str
    expected_source: str = Field(description="正解の文書名")
    hit: bool = Field(description="上位k件に正解が入ったか")
    rank: Optional[int] = Field(description="正解の順位（0始まり）。null=圏外")
    retrieved: List[str] = Field(description="実際に上位で引いた文書名の並び")


class EvalReport(BaseModel):
    """質問集全体の評価結果。UIの評価パネルはこれを描画する。"""

    n: int = Field(description="評価した質問数")
    top_k: int
    retrievers: Optional[List[str]] = Field(description="使った手法（null=設定の既定）")
    rerank: Optional[bool] = Field(description="リランクの有無（null=設定の既定）")
    rerank_method: Optional[str] = Field(
        default=None,
        description="リランクの方式 voyage/llm（null=設定の既定）。rerankが無効なら無意味",
    )
    rrf_k: Optional[int] = Field(default=None, description="使ったRRF k（null=既定）")
    params: Optional[Dict[str, Dict[str, float]]] = Field(
        default=None, description="使った数値パラメータ（手法ごと。null/空=既定）"
    )
    # 常にサーバー側が設定値を入れる（Optional にしない）。既定を置くのは、
    # このフィールドを持たない呼び出し側がレポートを組み立てられるようにするため。
    image_index_method: str = Field(
        default="none",
        description="画像の索引方式 caption/multimodal/none（取り込み時の設定。A/B比較用の記録）",
    )
    hit_at_k: float = Field(description="上位k件に正解が入った質問の割合")
    mrr: float = Field(description="正解順位の逆数平均（1位=1.0 / 圏外=0）")
    results: List[EvalResult]


class ErrorResponse(BaseModel):
    """エラー時の共通形。UIはこれをそのまま表示する。"""

    error: str = Field(description="機械判定用のコード（voyage_rate_limit 等）")
    message: str = Field(description="利用者向けメッセージ")
    hint: str = Field(default="", description="対処のヒント")
    detail: str = Field(default="", description="元の例外メッセージ")
