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
                -- 文書の所属。project(プロジェクト) > topic(トピック) の2階層で、
                -- NULL = どこにも属さない共通文書。評価(eval_questions)も同じ軸で
                -- 区切り、「そのプロジェクトの文書 × その質問」で測れるようにする。
                project    TEXT,
                topic      TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # 既存DB向けの冪等マイグレーション:
        #   旧 category カラムは topic に改名して中身を引き継ぐ（旧名は「分類」の
        #   つもりだったが、実体はトピックだったため名前を実体に合わせた）。
        #   RENAME には IF EXISTS が無いので information_schema で在否を見る。
        conn.execute(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'documents' AND column_name = 'category')
                THEN
                    ALTER TABLE documents RENAME COLUMN category TO topic;
                END IF;
            END $$;
            """
        )
        conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS project TEXT;")
        conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS topic TEXT;")
        # 差分検知用（app.ingest.content_hash）。再取り込み時にこれが一致すれば
        # 埋め込みAPIを呼ばずにスキップする。既存行は NULL＝「不明」なので
        # 一度だけ必ず取り込み直され、そこで値が入る。
        conn.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT;")
        # 取り込みは毎回 source で1件引く（差分検知）ので、無いと件数が増えたときに
        # 逐次スキャンになる。UNIQUE にしないのは、この機能より前のDBに同名の行が
        # 残っていた場合に init_db 自体が落ちるのを避けるため（取り込み側は
        # 「同じ source は消してから入れ直す」ので実質1件に保たれる）。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source);"
        )
        # 検索の区分絞り込み（retrieval._scope_sql の WHERE d.project/d.topic）用。
        # project だけの絞り込みでも先頭列として効くので複合1本で足りる。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS documents_scope_idx "
            "ON documents (project, topic);"
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
        # 外部キーの参照側にはインデックスが自動では付かない。無いと
        # スキップ時のチャンク数集計も、documents 削除時の CASCADE も
        # chunks 全体の逐次スキャンになる。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_document_id_idx "
            "ON chunks (document_id);"
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
        # 会話履歴。1件の会話(conversations) に発言(messages) がぶら下がる。
        # 「その上限は？」のような続きの質問に答えるには、直前のやり取りを
        # コンテキストに載せる必要があるため（単発の一問一答では答えられない）。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id         BIGSERIAL PRIMARY KEY,
                -- 一覧表示用の見出し。NULL = 未設定（最初の質問から後で付ける余地）
                title      TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id              BIGSERIAL PRIMARY KEY,
                -- 発言は必ず会話に属する。NULLだと履歴の復元から黙って外れるので NOT NULL。
                conversation_id BIGINT NOT NULL
                                REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,   -- 'user' | 'assistant'
                content         TEXT NOT NULL,
                -- 回答の根拠に使った出典。NULL可にせず空配列を既定にする
                -- （「根拠なし」と「未記録」を区別する必要が無いため）
                sources         TEXT[] NOT NULL DEFAULT '{}',
                created_at      TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # 履歴の読み出しは「この会話の直近N件」なので、会話ID＋順序で引く
        conn.execute(
            "CREATE INDEX IF NOT EXISTS messages_conversation_idx "
            "ON messages (conversation_id, id);"
        )
        # 回答フィードバック：👍/👎 を貯めて評価(eval)のQA候補に回す。
        # 会話とは独立に「回答そのもの」を丸ごと残すテーブル（評価用の素材なので、
        # 会話が消えてもフィードバックは残したい）。
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
        # 公開API(/v1)のAPIキー。発行・検証は app.apikeys 参照。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id                 BIGSERIAL PRIMARY KEY,
                -- 発行先が分かる名前（「営業部の社内ツール」等）。運用上必ず要るので NOT NULL
                name               TEXT NOT NULL,
                -- ★平文のキーは保存しない★ sha256(トークン) だけを持つ。
                -- 漏洩時に他システムへ流用されないようにするため（照合はハッシュ同士）。
                key_hash           TEXT NOT NULL UNIQUE,
                -- ★テナント分離キー★ このキーで見えるのはこのプロジェクトの文書だけ。
                -- NULL を許すと「区分なし＝全部見える」キーが作れてしまい分離が壊れるので NOT NULL。
                project            TEXT NOT NULL,
                -- 直近1分間に受け付ける本数（キーごとに変えられる）
                rate_limit_per_min INT NOT NULL DEFAULT 60,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                -- NULL = 有効。失効は行を消さず日時を入れる（利用ログを残すため）
                revoked_at         TIMESTAMPTZ
            );
            """
        )
        # キー単位の利用ログ。レート制限の判定もこの表を数えて行う。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_usage (
                id         BIGSERIAL PRIMARY KEY,
                api_key_id BIGINT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
                path       TEXT NOT NULL,
                -- 応答のHTTPステータス。NULL = 応答を返す前に落ちた（記録は受付時に入れ、
                -- 応答時に埋める）。受付の事実はレート制限に効くので先に1行作る。
                status     INT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # レート制限は毎リクエスト「このキーの直近1分」を数えるので、その形で引く
        conn.execute(
            "CREATE INDEX IF NOT EXISTS api_usage_key_time_idx "
            "ON api_usage (api_key_id, created_at DESC);"
        )
        # 会話の持ち主。NULL = 画面(UI)から始めた会話、値あり = そのAPIキーの会話。
        # これが無いと、公開APIの利用者が他人の conversation_id を渡すだけで
        # 別テナントの履歴を読み出せてしまう（app.conversations.resolve で照合する）。
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS api_key_id BIGINT "
            "REFERENCES api_keys(id) ON DELETE SET NULL;"
        )
        # 評価用の質問集（Hit@k / MRR を測る正解ラベル）。
        # コードの定数に置くと、文書をプロジェクト・トピックごとに分けたときに評価
        # だけ全体共通という歪みが出るうえ、質問追加のたびにコード改修が要る。
        # DBに置くことで文書(documents)と同じ軸(project/topic)で区切り、非エンジニア
        # でも追加できるようにする。expected_source が正解ラベル（正しく引けるべき文書名）。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_questions (
                id              BIGSERIAL PRIMARY KEY,
                project         TEXT,   -- NULL = プロジェクトをまたぐ共通の質問
                topic           TEXT,   -- NULL = トピックをまたぐ共通の質問
                question        TEXT NOT NULL,
                expected_source TEXT NOT NULL,  -- 正解の文書名（documents.source）
                note            TEXT,
                created_at      TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # 既存DB向けの冪等マイグレーション: 会社・部署の2軸は当初の実装で、
        # 本来の設計軸は project/topic。データを保ったまま改名する。
        conn.execute(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'eval_questions'
                             AND column_name = 'company')
                THEN
                    ALTER TABLE eval_questions RENAME COLUMN company TO project;
                END IF;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'eval_questions'
                             AND column_name = 'department')
                THEN
                    ALTER TABLE eval_questions RENAME COLUMN department TO topic;
                END IF;
            END $$;
            """
        )
