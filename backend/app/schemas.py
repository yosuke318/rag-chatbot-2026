"""API の入出力スキーマ（Pydantic）。

ここで宣言した型が FastAPI により OpenAPI スキーマ(/openapi.json)へ出力され、
フロントはそれを openapi-typescript で TypeScript 型に変換して使う。
＝ FE と BE で型定義の実体を1つに保つ（手書きの二重管理をなくす）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# --- リクエスト ---------------------------------------------------------------


class IngestRequest(BaseModel):
    source: str = Field(description="文書名（例: 有給休暇.txt）")
    text: str = Field(description="本文")
    category: Optional[str] = Field(default=None, description="分類（任意）")


class ChatRequest(BaseModel):
    question: str = Field(description="質問文")


# --- レスポンス ---------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str


class IngestResponse(BaseModel):
    source: str
    chunks_created: int = Field(description="作成したチャンク数")
    replaced: int = Field(description="置き換えた既存文書の件数（0なら新規）")


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


class ChatResponse(BaseModel):
    answer: str
    sources: List[str] = Field(description="根拠に使ったチャンクの出典（重複排除済み）")


class ErrorResponse(BaseModel):
    """エラー時の共通形。UIはこれをそのまま表示する。"""

    error: str = Field(description="機械判定用のコード（voyage_rate_limit 等）")
    message: str = Field(description="利用者向けメッセージ")
    hint: str = Field(default="", description="対処のヒント")
    detail: str = Field(default="", description="元の例外メッセージ")
