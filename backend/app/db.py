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
                document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index INT NOT NULL,
                content     TEXT NOT NULL,
                embedding   VECTOR({EMBED_DIM})
            );
            """
        )
        # 字面検索用：本文から名詞だけを抜き出した文字列（keywords.noun_text）
        # 既存DBにも後から足せるよう ALTER で追加する
        conn.execute(
            "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_nouns TEXT;"
        )
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
