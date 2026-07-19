"""API の入出力スキーマ（Pydantic）。

ここで宣言した型が FastAPI により OpenAPI スキーマ(/openapi.json)へ出力され、
フロントはそれを openapi-typescript で TypeScript 型に変換して使う。
＝ FE と BE で型定義の実体を1つに保つ（手書きの二重管理をなくす）。
"""
from __future__ import annotations

from typing import List, Optional

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


class VectorHit(BaseModel):
    """ベクトル検索（意味の近さ）のヒット。"""

    rank: int
    id: int
    source: str
    cosine_similarity: float = Field(description="1に近いほど意味が近い（1 - コサイン距離）")
    cosine_distance: float
    preview: str


class LexicalHit(BaseModel):
    """字面検索（名詞のトライグラム一致）のヒット。"""

    rank: int
    id: int
    source: str
    trgm_similarity: float = Field(description="0〜1。1に近いほど字面が一致")
    preview: str


class FusedHit(BaseModel):
    """RRFで融合した後の最終順位。"""

    rank: int
    id: int
    source: str
    score: float = Field(description="RRFスコア。Σ 1/(60 + 各検索での順位)")
    vector_rank: Optional[int] = Field(description="null = ベクトル検索に出てこなかった")
    lexical_rank: Optional[int] = Field(description="null = 字面検索に出てこなかった")
    cosine_similarity: Optional[float]
    trgm_similarity: Optional[float]
    preview: str


class SearchResponse(BaseModel):
    """検索の各段階（Claudeを呼ばない）。"""

    question: str
    lexical_min_similarity: float = Field(
        description="これ未満の字面ヒットはRRFに渡さない閾値"
    )
    vector_search: List[VectorHit]
    lexical_search: List[LexicalHit]
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
