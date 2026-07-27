"""PostgreSQL + pgvector 接続とスキーマ初期化。

最小版はコネクションを都度張る（プールは後で pool モジュールに切り出す想定）。
"""
import psycopg
from pgvector.psycopg import register_vector

from app.config import DATABASE_URL, EMBED_DIM


def get_conn() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    register_vector(conn)  # Python の list <-> pgvector を相互変換
    return conn


def init_db() -> None:
    """拡張の有効化とテーブル作成。冪等。アプリ起動時に一度呼ぶ。"""
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        # pg_trgm: 字面（トライグラム）一致検索用。日本語もそのまま効く。
        # ※これは厳密なBM25ではなく「文字n-gramの一致度」。retrieval.py 参照。
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id         BIGSERIAL PRIMARY KEY,
                source     TEXT NOT NULL,
                category   TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id          BIGSERIAL PRIMARY KEY,
                -- チャンクは必ず文書に属する。NULLだと検索のJOINから黙って外れて
                -- 孤児化するため NOT NULL。embedding は「必須にしない」判断（複数モデル
                -- 併存・遅延埋め込み・画像チャンクの自由度を残すため。将来は別テーブル化）。
                document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index INT NOT NULL,
                content     TEXT NOT NULL,
                embedding   VECTOR({EMBED_DIM})
            );
            """
        )
        # 既存DB（NOT NULL 追加より前に作られたもの）向けの冪等マイグレーション。
        # 現状 document_id が NULL の行は無いので安全に効く（既に NOT NULL なら no-op）。
        conn.execute(
            "ALTER TABLE chunks ALTER COLUMN document_id SET NOT NULL;"
        )
        # 字面検索用：本文から名詞だけを抜き出した文字列（keywords.noun_text）
        # 既存DBにも後から足せるよう ALTER で追加する
        conn.execute(
            "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_nouns TEXT;"
        )
        # contextual retrieval で生成した「文書内での位置づけ」（app.llm 参照）。
        # 埋め込み・字面検索には content と繋げたものを使うが、回答生成に渡すのは
        # あくまで content なので、別カラムに分けて保持する。
        conn.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS context TEXT;")
        # コサイン距離での近傍探索用インデックス（件数が少ないうちは無くても動く）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_embedding_idx "
            "ON chunks USING hnsw (embedding vector_cosine_ops);"
        )
        # トライグラム字面検索用インデックス（件数が少ないうちは無くても動く）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_content_nouns_trgm_idx "
            "ON chunks USING gin (content_nouns gin_trgm_ops);"
        )
        # 回答フィードバック：👍/👎 を貯めて評価(eval)のQA候補に回す。
        # 会話履歴(conversations)は未実装なので、まずは回答そのものを丸ごと残す
        # 独立テーブルにする。conversations 実装時に conversation_id を足せばよい。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id         BIGSERIAL PRIMARY KEY,
                question   TEXT NOT NULL,
                answer     TEXT NOT NULL,
                sources    TEXT[] NOT NULL DEFAULT '{}',
                rating     SMALLINT NOT NULL,   -- +1 = 👍 / -1 = 👎
                comment    TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # 評価用の質問集（Hit@k / MRR を測る正解ラベル）。
        # コードの定数に置くと、文書を会社・部署ごとに分けたときに評価だけ全社共通
        # という歪みが出るうえ、質問追加のたびにコード改修が要る。DBに置くことで
        # 文書(documents)と同じ粒度(company/department)で区切り、非エンジニアでも
        # 追加できるようにする。expected_source が正解ラベル（正しく引けるべき文書名）。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_questions (
                id              BIGSERIAL PRIMARY KEY,
                company         TEXT,   -- NULL = 会社をまたぐ共通の質問
                department      TEXT,   -- NULL = 部署をまたぐ共通の質問
                question        TEXT NOT NULL,
                expected_source TEXT NOT NULL,  -- 正解の文書名（documents.source）
                note            TEXT,
                created_at      TIMESTAMPTZ DEFAULT now()
            );
            """
        )
