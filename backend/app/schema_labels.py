"""テーブル・カラムの論理名（日本語の表示名）。★ここが正★

物理名（`eval_questions` / `expected_kind` / `content_nouns` …）は英語なので、
そのままUIやエラーメッセージに出しても利用者には意味が伝わらない。物理名と対に
なる日本語名をここ1か所に持ち、次の2方向へ配る:

  - DB   : app.db.init_db が COMMENT ON TABLE/COLUMN で流し込む
           （DBクライアントやER図ツールからも見える。DB側は★写し★）
  - アプリ: GET /schema がそのまま返す（UI・ドキュメント生成の参照口）

★なぜ Python 側を正にしたか★
  COMMENT ON をDDLに直接書いてDBを正にすると、論理名を引くだけでDB接続が要る
  （テストもDB必須になる）。逆にここを正にすれば、DBは起動時に上書きされる
  写しでよく、二重管理にならない。COMMENT ON は冪等なので、既存DBにも
  init_db を通すだけで反映される。

★カラムを足したらここにも足す★
  tests/test_schema_labels.py が db.py のDDLを読んで突き合わせ、論理名の無い
  カラムがあると落ちる。落ちたらこのファイルに1行足すこと。
"""
from __future__ import annotations

# {物理テーブル名: {"label": 論理名, "columns": {物理カラム名: 論理名}}}
#
# 並びは db.py の DDL と同じ順（作成順）にしてある。突き合わせるときに
# 目で追いやすいのと、新しいテーブルを足す位置が迷わないため。
SCHEMA_LABELS: dict[str, dict] = {
    "projects": {
        "label": "プロジェクト（区分マスタ）",
        "columns": {
            "id": "プロジェクトID",
            "name": "プロジェクト名",
            "created_at": "登録日時",
        },
    },
    "topics": {
        "label": "トピック（区分マスタ）",
        "columns": {
            "id": "トピックID",
            "project_id": "親プロジェクトID",
            "name": "トピック名",
            "created_at": "登録日時",
        },
    },
    "documents": {
        "label": "文書",
        "columns": {
            "id": "文書ID",
            "source": "文書名",
            "project_id": "所属プロジェクトID",
            "topic_id": "所属トピックID",
            "created_at": "登録日時",
            "content_hash": "本文ハッシュ（差分検知用）",
        },
    },
    "chunks": {
        "label": "チャンク（文書の分割単位）",
        "columns": {
            "id": "チャンクID",
            "document_id": "文書ID",
            "chunk_index": "文書内の連番",
            "content": "本文",
            "embedding": "本文ベクトル",
            "content_nouns": "本文から抜いた名詞列（字面検索用）",
            "context": "文書内での位置づけ（contextual retrieval）",
            "image_path": "画像の保管キー（S3）",
            "image_embedding": "画像ベクトル（マルチモーダル）",
        },
    },
    "conversations": {
        "label": "会話",
        "columns": {
            "id": "会話ID",
            "title": "会話の見出し",
            "created_at": "開始日時",
            "api_key_id": "発行元APIキーID（NULL=画面から）",
        },
    },
    "messages": {
        "label": "発言（会話の1件）",
        "columns": {
            "id": "発言ID",
            "conversation_id": "会話ID",
            "role": "話者（user / assistant）",
            "content": "本文",
            "sources": "根拠にした文書名",
            "created_at": "発言日時",
        },
    },
    "feedback": {
        "label": "回答フィードバック",
        "columns": {
            "id": "フィードバックID",
            "question": "質問",
            "answer": "回答",
            "sources": "根拠にした文書名",
            "rating": "評価（+1=👍 / -1=👎）",
            "comment": "コメント",
            "created_at": "登録日時",
            # ここから下は「どういう条件で出た回答への評価か」（8-1）。
            # 記録より前の行は空欄になる。
            "conversation_id": "会話ID",
            "message_id": "回答ID（この評価の対象）",
            "retriever": "使った検索手法",
            "top_k": "回答に渡したチャンク数",
            "reranked": "リランカーを通したか",
            "chunk_ids": "渡したチャンクID（並び順＝順位）",
            "latency_ms": "回答までの所要時間（ミリ秒）",
        },
    },
    "saved_questions": {
        "label": "保管質問（検索で自動保管）",
        "columns": {
            "id": "保管質問ID",
            "project_id": "プロジェクトID",
            "topic_id": "トピックID",
            "question": "質問",
            "created_at": "保管日時",
        },
    },
    "api_keys": {
        "label": "APIキー（公開API用）",
        "columns": {
            "id": "APIキーID",
            "name": "発行先の名前",
            "key_hash": "キーのハッシュ（平文は保存しない）",
            "project": "参照できるプロジェクト",
            "rate_limit_per_min": "毎分の上限リクエスト数",
            "created_at": "発行日時",
            "revoked_at": "失効日時（NULL=有効）",
        },
    },
    "api_usage": {
        "label": "API利用ログ",
        "columns": {
            "id": "利用ログID",
            "api_key_id": "APIキーID",
            "path": "呼ばれたパス",
            "status": "応答ステータス（NULL=応答前に中断）",
            "created_at": "受付日時",
        },
    },
    "eval_questions": {
        "label": "評価用の質問（正解ラベル付き）",
        "columns": {
            "id": "評価質問ID",
            "project_id": "プロジェクトID",
            "topic_id": "トピックID",
            "question": "質問",
            "expected_source": "正解の文書名",
            "note": "メモ",
            "created_at": "登録日時",
            "expected_kind": "正解とするチャンクの種類（any / text / image）",
            "expected_text": "正解チャンクに含まれる語句（NULL=文書単位で判定）",
        },
    },
}


def table_label(table: str) -> str:
    """テーブルの論理名。未登録なら物理名をそのまま返す（画面が空欄にならない）。"""
    entry = SCHEMA_LABELS.get(table)
    return entry["label"] if entry else table


def column_label(table: str, column: str) -> str:
    """カラムの論理名。未登録なら物理名をそのまま返す。

    ★未登録でも例外にしない★ 論理名は表示のための飾りなので、抜けを理由に
    実行時の機能を止める意味がない。抜けはテスト（test_schema_labels.py）で
    落とす担当にして、実行時は物理名にフォールバックする。
    """
    entry = SCHEMA_LABELS.get(table)
    if not entry:
        return column
    return entry["columns"].get(column, column)
