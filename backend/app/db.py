"""PostgreSQL + pgvector 接続とスキーマ初期化。

最小版はコネクションを都度張る（プールは後で pool モジュールに切り出す想定）。
"""
import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

from app.config import DATABASE_URL, EMBED_DIM, MULTIMODAL_EMBED_DIM
from app.schema_labels import SCHEMA_LABELS


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
        # --- 区分(project / topic)のマスタ ------------------------------------
        # 文書・質問より先に作る（各テーブルが id を FK 参照するため）。
        # マスタが正: 「文書か質問に実在する値の DISTINCT」を選択肢にしていた頃は
        # 文書も質問も無いプロジェクトが存在できず、表記ゆれ（「営業部」と「営業」）
        # にも気づけなかった。
        #
        # 暫定スキーマ（名前が主キーで、各テーブルは名前TEXTを重複保持）からの
        # 冪等マイグレーション: id カラムが無ければ旧形式なので、一旦 *_v1 に
        # 退避して作り直し、後で名前を写す。索引を先に消すのは、索引名がDB全体で
        # 一意なため、旧テーブルに付いたままだと新テーブル側で同名の索引を
        # 作れないから。
        conn.execute(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.tables
                           WHERE table_name = 'projects')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name = 'projects'
                                     AND column_name = 'id')
                THEN
                    DROP INDEX IF EXISTS topics_unique_idx;
                    ALTER TABLE topics RENAME TO topics_v1;
                    ALTER TABLE projects RENAME TO projects_v1;
                END IF;
            END $$;
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id         BIGSERIAL PRIMARY KEY,
                -- 表示も検索の入口も名前で行う（APIは名前を受け取り、ここで id に引く）
                name       TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # トピックは「そのプロジェクト配下の名前」。UIが project → topic の順に
        # 絞り込むので、同じトピック名が別プロジェクトに在っても構わない。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topics (
                id         BIGSERIAL PRIMARY KEY,
                -- NULL = プロジェクトに属さないトピック。documents は project と topic
                -- を独立に NULL 可で持つため（topic だけ付いた文書が作れる）、マスタ側
                -- でもその状態を表現できないと既存データを取りこぼす。
                project_id BIGINT REFERENCES projects(id),
                name       TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # ★NULLS NOT DISTINCT★ が要るのは saved_questions と同じ理由。
        # 既定では NULL 同士が「別の値」になり、project なしの同名トピックが
        # 何行でも入ってしまう（PostgreSQL 15+。compose の pgvector:pg16 は満たす）。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS topics_unique_idx "
            "ON topics (project_id, name) NULLS NOT DISTINCT;"
        )
        # 暫定スキーマから退避した名前を新テーブルへ写して、退避を消す。
        # 「文書0件で作った空のプロジェクト」はマスタにしか存在しないので、
        # ここで写さないと消えてしまう（使用中の値は後段の正規化でも入るが、
        # 空の区分はそこでは拾えない）。
        conn.execute(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.tables
                           WHERE table_name = 'projects_v1')
                THEN
                    INSERT INTO projects (name, created_at)
                        SELECT name, created_at FROM projects_v1
                        ON CONFLICT (name) DO NOTHING;
                    INSERT INTO topics (project_id, name, created_at)
                        SELECT p.id, t.name, t.created_at FROM topics_v1 t
                        LEFT JOIN projects p ON p.name = t.project
                        ON CONFLICT DO NOTHING;
                    DROP TABLE topics_v1;
                    DROP TABLE projects_v1;
                END IF;
            END $$;
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id         BIGSERIAL PRIMARY KEY,
                source     TEXT NOT NULL,
                -- 文書の所属。project(プロジェクト) > topic(トピック) の2階層で、
                -- マスタへの id 参照。NULL = どこにも属さない共通文書。
                -- 評価(eval_questions)も同じ軸で区切り、「そのプロジェクトの文書 ×
                -- その質問」で測れるようにする。
                project_id BIGINT REFERENCES projects(id),
                topic_id   BIGINT REFERENCES topics(id),
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # 既存DB向けの冪等マイグレーション:
        #   旧 category カラムは topic に改名して中身を引き継ぐ（旧名は「分類」の
        #   つもりだったが、実体はトピックだったため名前を実体に合わせた）。
        #   RENAME には IF EXISTS が無いので information_schema で在否を見る。
        #   この時代のDBは project カラム自体が無いので、ここで TEXT のまま足して
        #   「TEXT時代のDBは project/topic を両方持つ」に揃える（後段の正規化
        #   マイグレーションがその前提で組で処理するため）。
        conn.execute(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'documents' AND column_name = 'category')
                THEN
                    ALTER TABLE documents RENAME COLUMN category TO topic;
                    ALTER TABLE documents ADD COLUMN IF NOT EXISTS project TEXT;
                END IF;
            END $$;
            """
        )
        # 既存DB（TEXT時代・区分導入前）向け。値の写しは後段の正規化マイグレーションで行う。
        conn.execute(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS "
            "project_id BIGINT REFERENCES projects(id);"
        )
        conn.execute(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS "
            "topic_id BIGINT REFERENCES topics(id);"
        )
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
        # 検索の区分絞り込み用の索引(documents_scope_idx)は、正規化マイグレーション
        # （ファイル末尾）の後で id カラムに対して作る。ここで作ると、TEXT時代の
        # 同名索引が残っているDBで IF NOT EXISTS が効いてしまい、id 版が作られない。
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
        # 文書内画像の原本のS3キー（app.storage.image_key）。
        # NULL = テキストチャンク。値あり = 画像チャンク（その1枚が根拠になる）。
        # 画像チャンクは登録した時点では embedding も content_nouns も持たないため
        # 検索にはヒットしない（検索対象化は索引作成で別途行う）。回答生成で原本画像を渡す
        # ときに、ヒットしたチャンクからこのキーで原本を引く。
        conn.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS image_path TEXT;")
        # 画像チャンクは「その文書の分を丸ごと入れ替える」形で書くので、
        # 文書ID＋画像有無で引ける形にしておく（部分インデックスなので
        # テキストチャンクが大半でも小さいまま）。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_image_idx ON chunks (document_id) "
            "WHERE image_path IS NOT NULL;"
        )
        # 案B（IMAGE_INDEX_METHOD=multimodal）で画像を直接ベクトル化したもの。
        # ★embedding とは別の空間★（voyage-multimodal-3 と voyage-3.5）なので
        # 同じ列に混ぜられない。混ぜるとエラーにならずただ無意味な順位が返る。
        # NULL = その画像は案Bの索引を持たない（案A・未索引・テキストチャンク）。
        conn.execute(
            "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS image_embedding "
            f"VECTOR({MULTIMODAL_EMBED_DIM});"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS chunks_image_embedding_idx "
            "ON chunks USING hnsw (image_embedding vector_cosine_ops);"
        )
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
        # 👍/👎 が「どういう条件で出た回答への評価か」を残す列。
        # 本文コピー（question/answer/sources）はそのまま残し、ここは
        # ★あれば辿れる補助★として足す。これが無いと「この設定変更で👎が
        # 減った」「👎のとき正解は何位に居たのか」が後から一切追えない。
        #
        # ★すべて任意（NULL可）★
        #   既存行は NULL のまま＝「記録していなかった頃のもの」。埋められない値を
        #   NOT NULL にすると過去分を捨てるか嘘の既定値を入れるかになる。
        #
        # ★参照は ON DELETE SET NULL（CASCADE にしない）★
        #   上の「会話が消えてもフィードバックは残す」を維持するため。会話を消したら
        #   評価の素材まで道連れになる、が一番避けたい壊れ方。
        conn.execute(
            "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS conversation_id BIGINT "
            "REFERENCES conversations(id) ON DELETE SET NULL;"
        )
        # どの発言への評価か。conversation_id だけだと「会話のどの回答か」が
        # 定まらない（1つの会話に回答は何度も入る）。
        conn.execute(
            "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS message_id BIGINT "
            "REFERENCES messages(id) ON DELETE SET NULL;"
        )
        # 使った検索手法。RRFで複数を融合するので "vector,trgm" のように連結して
        # 持つ（設定 RETRIEVERS と同じ書式にしておくと、そのまま再現に使える）。
        conn.execute("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS retriever TEXT;")
        conn.execute("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS top_k SMALLINT;")
        conn.execute("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS reranked BOOLEAN;")
        # 回答生成に渡したチャンクを★順位どおり★に並べた配列（先頭が1位）。
        # 空配列を既定にするのは sources と同じ理由（「根拠なし」と「未記録」を
        # 区別する必要が無く、NULLと空配列の二重の空を作りたくない）。
        conn.execute(
            "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS "
            "chunk_ids BIGINT[] NOT NULL DEFAULT '{}';"
        )
        # 質問を受けてから回答が出来上がるまで（検索＋生成）。体感の遅さと
        # 👎の相関を見るため。
        conn.execute("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS latency_ms INTEGER;")
        # ②で検索した質問の保管庫。正解ラベルを持たない「実際に聞かれた質問」を
        # 区分ごとに貯め、④でまとめてRRFを検証するのに使う。
        # eval_questions と分けるのは、あちらが expected_source NOT NULL（正解必須）で
        # 正解の分からない質問を入れられないため。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_questions (
                id         BIGSERIAL PRIMARY KEY,
                project_id BIGINT REFERENCES projects(id),  -- NULL = 区分を選ばずに検索した質問
                topic_id   BIGINT REFERENCES topics(id),
                question   TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        # 既存DB（TEXT時代）向け。値の写しは後段の正規化マイグレーションで行う。
        # 重複防止のユニーク索引(saved_questions_unique_idx)も同じ理由で後段
        # （documents_scope_idx と同じ。TEXT時代の同名索引と衝突するため）。
        conn.execute(
            "ALTER TABLE saved_questions ADD COLUMN IF NOT EXISTS "
            "project_id BIGINT REFERENCES projects(id);"
        )
        conn.execute(
            "ALTER TABLE saved_questions ADD COLUMN IF NOT EXISTS "
            "topic_id BIGINT REFERENCES topics(id);"
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
                -- NULL を許すと「区分なし＝全部見える」キーが作れてしまい、
                -- 分離が壊れるので NOT NULL。
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
                -- NULL = プロジェクト／トピックをまたぐ共通の質問
                project_id      BIGINT REFERENCES projects(id),
                topic_id        BIGINT REFERENCES topics(id),
                question        TEXT NOT NULL,
                expected_source TEXT NOT NULL,  -- 正解の文書名（documents.source）
                note            TEXT,
                created_at      TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        # 既存DB（TEXT時代）向け。値の写しは後段の正規化マイグレーションで行う。
        conn.execute(
            "ALTER TABLE eval_questions ADD COLUMN IF NOT EXISTS "
            "project_id BIGINT REFERENCES projects(id);"
        )
        conn.execute(
            "ALTER TABLE eval_questions ADD COLUMN IF NOT EXISTS "
            "topic_id BIGINT REFERENCES topics(id);"
        )
        # 正解を「どの種類のチャンクで引けたら正解か」まで下ろす軸（画像の索引方式の比較評価用）。
        #   'any'（既定） … 文書が上位に来れば正解（従来どおり）
        #   'text'        … 本文チャンクで引けたときだけ正解
        #   'image'       … ★画像チャンクで引けたときだけ正解★
        # これが無いと、図表の索引方式を変えても「同じ文書が1位」で同点になり、
        # 案A/案Bの差が数値に出ない（文書名だけが正解ラベルだったときの限界）。
        # NOT NULL + DEFAULT にするのは、NULL に「未指定」以上の意味が無く、
        # 既存行の意味（文書単位の判定）がそのまま 'any' に対応するため。
        conn.execute(
            "ALTER TABLE eval_questions ADD COLUMN IF NOT EXISTS "
            "expected_kind TEXT NOT NULL DEFAULT 'any';"
        )
        # 正解ラベルを「その文書のどこか」から「このチャンク」へ下ろす軸。
        #   NULL   … 従来どおり文書名だけで判定する（既存の質問の意味を変えない）
        #   値あり … その語句を含むチャンクを引けたときだけ正解
        # これが無いと、分割・文脈付与・リランクといった★チャンク単位の改良★が
        # 数値に出ない（就業規則.txt の5チャンクのどれが1位でも同点になるため）。
        # ★チャンクIDではなく語句で持つ★ 比較評価(app.compare)は文書を取り込み
        # 直すのでIDが変わり、分割ロジックを変えればさらにずれる。語句なら
        # 再チャンク後も生き残る。
        # nullable にするのは、NULL に「文書単位で判定する」という意味があり、
        # 既存の質問すべてがその状態に当たるため（後方互換）。
        conn.execute(
            "ALTER TABLE eval_questions ADD COLUMN IF NOT EXISTS expected_text TEXT;"
        )
        # 既存DB向けの冪等マイグレーション: 会社・部署の2軸は当初の実装で、
        # 本来の設計軸は project/topic。データを保ったまま改名する
        # （改名後のTEXTカラムは、後段の正規化マイグレーションで id 参照になる）。
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
        # --- 区分の正規化マイグレーション（TEXT → id 参照）--------------------
        # かつて documents / eval_questions / saved_questions は区分を生の TEXT
        # （project / topic カラム）で重複保持していた。マスタ(projects/topics)を
        # 正としたので、値をマスタへ写し、各行の参照を id に切り替え、TEXT カラムは
        # 落とす。リネームは projects.name の UPDATE 1発になり、表記ゆれも
        # マスタに無い名前として弾ける素地ができる。
        #
        # ★テーブルごとに独立した IF で囲む★
        #   3表のTEXTカラムは別々の時期に生まれたので、「documents にはあるが
        #   saved_questions は既にid化済み」のようなDBがあり得る。一括で書くと
        #   その状態で存在しないカラムを参照して落ちる。
        # 移行済みのDBではカラムが無い＝IFが偽で丸ごとスキップ（冪等）。
        conn.execute(
            """
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'documents'
                             AND column_name = 'project')
                THEN
                    INSERT INTO projects (name)
                        SELECT DISTINCT project FROM documents
                        WHERE project IS NOT NULL
                        ON CONFLICT (name) DO NOTHING;
                    INSERT INTO topics (project_id, name)
                        SELECT DISTINCT p.id, d.topic FROM documents d
                        LEFT JOIN projects p ON p.name = d.project
                        WHERE d.topic IS NOT NULL
                        ON CONFLICT DO NOTHING;
                    UPDATE documents d SET project_id = p.id
                        FROM projects p
                        WHERE d.project = p.name AND d.project_id IS NULL;
                    UPDATE documents d SET topic_id = t.id
                        FROM topics t
                        WHERE t.name = d.topic
                          AND t.project_id IS NOT DISTINCT FROM d.project_id
                          AND d.topic_id IS NULL;
                    -- 索引(documents_scope_idx)はカラムと一緒に消える。
                    -- id 版は後段で作り直す。
                    ALTER TABLE documents DROP COLUMN project;
                    ALTER TABLE documents DROP COLUMN topic;
                END IF;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'eval_questions'
                             AND column_name = 'project')
                THEN
                    INSERT INTO projects (name)
                        SELECT DISTINCT project FROM eval_questions
                        WHERE project IS NOT NULL
                        ON CONFLICT (name) DO NOTHING;
                    INSERT INTO topics (project_id, name)
                        SELECT DISTINCT p.id, q.topic FROM eval_questions q
                        LEFT JOIN projects p ON p.name = q.project
                        WHERE q.topic IS NOT NULL
                        ON CONFLICT DO NOTHING;
                    UPDATE eval_questions q SET project_id = p.id
                        FROM projects p
                        WHERE q.project = p.name AND q.project_id IS NULL;
                    UPDATE eval_questions q SET topic_id = t.id
                        FROM topics t
                        WHERE t.name = q.topic
                          AND t.project_id IS NOT DISTINCT FROM q.project_id
                          AND q.topic_id IS NULL;
                    ALTER TABLE eval_questions DROP COLUMN project;
                    ALTER TABLE eval_questions DROP COLUMN topic;
                END IF;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'saved_questions'
                             AND column_name = 'project')
                THEN
                    INSERT INTO projects (name)
                        SELECT DISTINCT project FROM saved_questions
                        WHERE project IS NOT NULL
                        ON CONFLICT (name) DO NOTHING;
                    INSERT INTO topics (project_id, name)
                        SELECT DISTINCT p.id, s.topic FROM saved_questions s
                        LEFT JOIN projects p ON p.name = s.project
                        WHERE s.topic IS NOT NULL
                        ON CONFLICT DO NOTHING;
                    UPDATE saved_questions s SET project_id = p.id
                        FROM projects p
                        WHERE s.project = p.name AND s.project_id IS NULL;
                    UPDATE saved_questions s SET topic_id = t.id
                        FROM topics t
                        WHERE t.name = s.topic
                          AND t.project_id IS NOT DISTINCT FROM s.project_id
                          AND s.topic_id IS NULL;
                    -- 重複防止のユニーク索引もカラムと一緒に消える（id 版は後段）。
                    ALTER TABLE saved_questions DROP COLUMN project;
                    ALTER TABLE saved_questions DROP COLUMN topic;
                END IF;
            END $$;
            """
        )
        # --- 正規化後の索引（TEXT時代と同名。旧索引はカラムDROPで消えている）---
        # 検索の区分絞り込み（retrieval._scope_sql の WHERE d.project_id/d.topic_id）用。
        # project だけの絞り込みでも先頭列として効くので複合1本で足りる。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS documents_scope_idx "
            "ON documents (project_id, topic_id);"
        )
        # 同じ区分の同じ質問は積み上げない。★NULLS NOT DISTINCT★ が肝なのは
        # TEXT時代と同じ（既定では NULL 同士が「別の値」になり、区分なしの同じ
        # 質問が何行でも入る。PostgreSQL 15+。compose の pgvector:pg16 は満たす）。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS saved_questions_unique_idx "
            "ON saved_questions (project_id, topic_id, question) NULLS NOT DISTINCT;"
        )
        # --- 論理名（日本語名）をDBにも写す --------------------------------
        # 正は app.schema_labels。ここは「DBクライアントやER図ツールからも
        # 日本語名が見える」ようにするための写しなので、毎回上書きでよい
        # （COMMENT ON は同じ値を入れ直しても副作用が無く、冪等）。
        # ★カラムのDROPより後で実行する★ 上の正規化マイグレーションで消える
        # TEXT時代のカラム（documents.project 等）に対して COMMENT を打つと
        # 存在しない列でエラーになるため。
        _apply_labels(conn)


def _apply_labels(conn: psycopg.Connection) -> None:
    """論理名を COMMENT ON TABLE / COLUMN としてDBへ書き込む。

    ★COMMENT ON はプレースホルダを受け付けない★ 対象名も本文もSQL文そのものに
    埋める必要があるので、psycopg.sql で識別子・リテラルとしてクォートする
    （論理名は自分たちで書く定数だが、'' の混入で構文が壊れるのを防ぐ）。

    実在しないテーブル・カラムには打たない。辞書には載っているが、そのDBには
    まだ無い（マイグレーション途中の古いDB）という状態があり得るため、
    ここで落とすと起動できなくなる。
    """
    for table, entry in SCHEMA_LABELS.items():
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s",
                (table,),
            ).fetchall()
        }
        if not existing:
            continue  # そのテーブル自体がまだ無い
        conn.execute(
            sql.SQL("COMMENT ON TABLE {} IS {}").format(
                sql.Identifier(table), sql.Literal(entry["label"])
            )
        )
        for column, label in entry["columns"].items():
            if column not in existing:
                continue
            conn.execute(
                sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
                    sql.Identifier(table), sql.Identifier(column), sql.Literal(label)
                )
            )
