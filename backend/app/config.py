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

# 文書内画像の抽出（フェーズ5 マルチモーダルの土台。app.parsers / app.ingest）。
# 抽出した画像は原本を S3 に置き、chunks.image_path から辿れるようにする。
# 画像を検索対象にする（キャプション or マルチモーダル埋め込み）のは次段(5-2)。
EXTRACT_IMAGES = os.getenv("EXTRACT_IMAGES", "true").lower() == "true"

# PDF は「埋め込みラスタ画像」ではなく★ページ全体をレンダリング★して1枚の画像にする。
# Excel/PowerPoint から出力したPDFの図表はベクタ描画で、埋め込み画像として
# 取り出せないものが多いため（チャート読解(5-4)がごっそり取りこぼす）。
# スケールは 1.0 = 72dpi。2.0 ≒ 144dpi で、細かい軸ラベルも読める程度。
# 上げるほど読みやすくなるがS3容量と入力トークンが増える。
PDF_RENDER_SCALE = float(os.getenv("PDF_RENDER_SCALE", "2.0"))

# 抽出画像の足切り（幅・高さのどちらかがこれ未満なら捨てる）。
# ヘッダのロゴ・罫線・箇条書きアイコンが画像チャンクとして大量に積み上がるのを防ぐ。
# PDFのページ画像は必ずこれを超えるので、実質 xlsx/pptx の埋め込み画像に効く。
IMAGE_MIN_PIXELS = int(os.getenv("IMAGE_MIN_PIXELS", "100"))

# 1文書あたりの抽出上限。数百ページのPDFで S3 と DB が膨らむのを止める安全弁。
IMAGE_MAX_PER_DOC = int(os.getenv("IMAGE_MAX_PER_DOC", "50"))

# 抽出画像を「テキストの質問で引ける」状態にする方式（5-2）。
# ★どちらが良いかは eval で決める★ ものなので、両方を実装して切り替え可能にしてある。
#
#   "caption"    … 案A: Claudeに画像を説明文へ変換させ、その文を既存のテキスト経路
#                  （埋め込み＋名詞の字面検索）に流す。3年前に人手でやっていた
#                  「図の逐一言語化」の自動化版。既存の検索3手法がそのまま効く。
#   "multimodal" … 案B: voyage-multimodal-3 で画像を直接ベクトル化し、テキストと
#                  同じ空間に置く。言語化を挟まないので「説明文に書かれなかった
#                  情報」を落とさないが、専用の検索手法(image)が要る。
#   "none"       … 索引を作らない（5-1のまま＝保管だけ）。画像APIのコストを止める用。
#
# 比較のしかたは app.eval のドキュメント参照。
IMAGE_INDEX_METHOD = os.getenv("IMAGE_INDEX_METHOD", "caption").lower()

# 案A用。画像の説明文を書かせるモデル（未指定なら CHAT_MODEL と同じ）と並列数。
CAPTION_MODEL = os.getenv("CAPTION_MODEL", CHAT_MODEL)
CAPTION_CONCURRENCY = int(os.getenv("CAPTION_CONCURRENCY", "4"))

# 案B用。voyage-multimodal-3 は画像とテキストを同じ1024次元の空間に埋め込む。
# ★テキスト用の EMBED_MODEL とは別の空間★なので、比較できるのは
# 「multimodalで埋めた画像」と「multimodalで埋めた質問」の間だけ。
# そのため chunks.embedding とは別カラム(image_embedding)に持つ。
MULTIMODAL_EMBED_MODEL = os.getenv("MULTIMODAL_EMBED_MODEL", "voyage-multimodal-3")
MULTIMODAL_EMBED_DIM = int(os.getenv("MULTIMODAL_EMBED_DIM", "1024"))

# 検索・生成
TOP_K = 4  # 回答生成に使うチャンク数

# 公開API(/v1)のレート制限。APIキー1本あたり「直近1分間に受け付ける本数」。
# キーごとに api_keys.rate_limit_per_min で上書きでき、ここはその既定値。
# 目的は課金保護（1リクエストごとに埋め込み・生成APIを呼ぶため）と暴走の抑制。
API_RATE_LIMIT_PER_MIN = int(os.getenv("API_RATE_LIMIT_PER_MIN", "60"))

# 回答生成に載せる直近の会話履歴の件数（user/assistant を1件ずつ数える）。
# 既定6件＝直近3往復。増やすほど文脈は繋がるが入力トークンとコストが増える。
# 0にすると履歴を使わない＝単発の一問一答に戻る（挙動比較用）。
HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "6"))

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

# リランク（有り/無しを比較できるようフラグ化）
USE_RERANK = os.getenv("USE_RERANK", "false").lower() == "true"
RERANK_CANDIDATES = 10  # 融合結果の上位いくつをリランク対象にするか

# リランクの方式。どちらも「質問 + 候補本文 → 関連順の番号」を返す同じ形で、
# app.retrieval.RERANKERS で切り替える。eval で3条件（なし / llm / voyage）を比較する。
#   "voyage" … Voyage の専用リランクAPI（rerank-2）。本命。
#               生成モデルより安く・速く、順位が安定する（毎回同じ入力なら同じ順位）。
#   "llm"    … Claudeに番号を並べ替えさせるプロンプト式。比較用に残してある。
RERANK_METHOD = os.getenv("RERANK_METHOD", "voyage").lower()

# Voyage のリランクモデル。rerank-2 は多言語対応で日本語も扱える。
# 速度優先なら rerank-2-lite（精度は少し落ちる）。
RERANK_MODEL = os.getenv("RERANK_MODEL", "rerank-2")
