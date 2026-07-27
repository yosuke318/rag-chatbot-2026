"""環境変数の読み込み。最小構成なので dotenv + os.getenv で十分。"""
import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rag:rag@localhost:5432/rag")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY")

# 原本保存（S3互換。ローカルは docker compose の MinIO）。
# 未設定なら原本保存はスキップする（S3なしでもアプリは動く）。
# 認証情報(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)は boto3 が環境変数から自動で読む。
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")  # 例: http://minio:9000。実S3なら未設定
S3_BUCKET = os.getenv("S3_BUCKET")  # 例: rag-docs

CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-8")
EMBED_MODEL = os.getenv("EMBED_MODEL", "voyage-3.5")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))  # voyage-3.5 は 1024次元

# チャンクへの文脈付与（contextual retrieval）に使うモデル。
# 「文書全体を読んで、このチャンクが何の話かを1〜2文で書く」だけの軽い仕事なので
# 回答生成より安いモデルに落としてもよい（未指定なら CHAT_MODEL と同じ）。
CONTEXT_MODEL = os.getenv("CONTEXT_MODEL", CHAT_MODEL)

# チャンク分割（app.chunking）。
# 文字数で機械的に切るのをやめ、見出し・条文の構造で切るようにしたため、
# 「サイズ」は"目標値"ではなく"上限と下限"の意味に変わった。
#   CHUNK_MAX_CHARS: これを超えた節だけ、文の切れ目で二次分割する
#   CHUNK_MIN_CHARS: これに満たない節は次の節とくっつける（断片化防止）
#   CHUNK_OVERLAP  : 二次分割したときだけ末尾を重ねる幅。構造で切れた
#                    チャンク同士は重ねない（重複が検索結果を汚すため）
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "1200"))
CHUNK_MIN_CHARS = int(os.getenv("CHUNK_MIN_CHARS", "200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))

# 差分検知(app.ingest.content_hash)に混ぜる分割ロジックの版。
# 取り込みは content_hash が一致したら埋め込みを丸ごと省くので、app.chunking の
# 分割規則を変えても本文が同じ限り既存文書は作り直されない。分割を変えたら
# ここを上げること（＝次の取り込みで全文書が入れ直される）。
# 上のサイズ設定は値が変われば自動でハッシュに効くため、ここに含めるのは
# 「コードを変えた」ことだけを伝える手動の目印。
CHUNKING_VERSION = "1"

# contextual retrieval: 各チャンクに「文書内での位置づけ」を前置してから埋め込む。
# 断片だけでは意味が取れないチャンク（「これを超える場合は所属長の承認を要する」等）の
# ヒット率を上げる狙い。false にすると見出しの階層だけを前置する（Claude を呼ばない）。
# 効果は eval（python -m app.eval）で Hit@k / MRR を比較して確認すること。
USE_CONTEXTUAL_CHUNKING = os.getenv("USE_CONTEXTUAL_CHUNKING", "true").lower() == "true"

# 文脈生成の並列数。1件目は直列で投げてプロンプトキャッシュを作り、
# 残りをこの数だけ並列で投げる（詳細は app.llm.generate_chunk_contexts）。
CONTEXT_CONCURRENCY = int(os.getenv("CONTEXT_CONCURRENCY", "4"))

# ファイルアップロード（/ingest-file）の1ファイル上限。
# 受け取ったバイト列は一度メモリに載せるため、無制限だと巨大ファイルで
# メモリを食い潰す。既定20MB。社内文書想定なら十分で、必要なら環境変数で調整。
UPLOAD_MAX_BYTES = int(os.getenv("UPLOAD_MAX_MB", "20")) * 1024 * 1024

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

# BM25のパラメータ（式は retrieval.bm25_search を参照）
#   k1: TFの飽和度。大きいほど「出現回数が多い」ことを強く評価する（1.2〜2.0が定番）
#   b : 長さ正規化の強さ。1.0=長い文書を強く不利に、0=長さを無視（0.75が定番）
BM25_K1 = float(os.getenv("BM25_K1", "1.2"))
BM25_B = float(os.getenv("BM25_B", "0.75"))

# LLMリランク（有り/無しを比較できるようフラグ化）
USE_RERANK = os.getenv("USE_RERANK", "false").lower() == "true"
RERANK_CANDIDATES = 10  # 融合結果の上位いくつをリランク対象にするか
