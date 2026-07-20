"""環境変数の読み込み。最小構成なので dotenv + os.getenv で十分。"""
import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rag:rag@localhost:5432/rag")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY")

CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-8")
EMBED_MODEL = os.getenv("EMBED_MODEL", "voyage-3.5")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))  # voyage-3.5 は 1024次元

# チャンク分割（最小版は文字数ベース。設計書のcontextual化は次段）
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

# 検索・生成
TOP_K = 4  # 回答生成に使うチャンク数

# 字面検索のノイズ除去閾値。
# trgm類似度がこの値未満の候補は「一致していない」とみなし、RRFに渡さない。
# （類似度0の候補が"偽の順位"を持ってRRFに票を投じるのを防ぐ）
# 実測では正解チャンクでも0.0102程度しか出ないため既定値は低め。
# 名詞抽出を入れて類似度の値域が上がったら、この値も上げて再調整すること。
LEXICAL_MIN_SIMILARITY = float(os.getenv("LEXICAL_MIN_SIMILARITY", "0.005"))

# ハイブリッド検索で使う手法の既定（カンマ区切り）。
#   "vector,trgm"       … 現状
#   "vector,bm25"       … BM25実装後の本命（案A：字面系を1本に統一）
#   "vector,trgm,bm25"  … 検証用（案C：字面系2本。票が字面に偏る点に注意）
# 個別のリクエストでは /search?retrievers=... で上書きできる。
RETRIEVERS_DEFAULT = [
    n.strip() for n in os.getenv("RETRIEVERS", "vector,trgm").split(",") if n.strip()
]

# LLMリランク（有り/無しを比較できるようフラグ化）
USE_RERANK = os.getenv("USE_RERANK", "false").lower() == "true"
RERANK_CANDIDATES = 10  # 融合結果の上位いくつをリランク対象にするか
