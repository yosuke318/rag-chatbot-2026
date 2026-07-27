"""POST /ingest-file エンドポイントのテスト。

TestClient(app) を使って主要なステータスコードとレスポンス形を検証する。
外部サービス（DB / 埋め込みAPI / S3）はすべてモックする。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

# psycopg が入っていない環境では skip する（本番コンテナでは実行される）。
pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402
from app.ingest import UnsupportedFileType  # noqa: E402


@pytest.fixture(scope="module")
def client():
    """DB・LLM・S3 をモックした TestClient。lifespan の init_db も差し替える。

    main.py は `from app.db import init_db` で名前を束縛しているため、
    `patch("app.db.init_db")` では差し替わらない（「patchの中で初めて
    app.main を import する」形なら効くが、他のテストが先に app.main を
    import すると崩れる）。実際に呼ばれる名前を patch.object で差し替える。
    """
    from app import main as main_module

    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

_MOCK_INGEST_RESULT = {"chunks_created": 3, "replaced": 0}


def _post_txt(client: TestClient, content: bytes = b"Hello world", filename: str = "doc.txt"):
    """テキストファイルを /ingest-file へ送る共通ヘルパー。"""
    return client.post(
        "/ingest-file",
        files={"file": (filename, content, "text/plain")},
    )


# ---------------------------------------------------------------------------
# 200 – 正常系
# ---------------------------------------------------------------------------


def test_200_returns_ingest_response_shape(client: TestClient):
    """正常アップロードで IngestResponse（source / chunks_created / replaced）が返る。"""
    with (
        patch("app.main.extract_text", return_value="取り込み本文テキスト"),
        patch("app.main.ingest_text", return_value=_MOCK_INGEST_RESULT),
        patch("app.main.storage"),
    ):
        resp = _post_txt(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "doc.txt"
    assert body["chunks_created"] == 3
    assert body["replaced"] == 0


# ---------------------------------------------------------------------------
# 400 – 入力不正
# ---------------------------------------------------------------------------


def test_400_empty_filename(client: TestClient):
    """ファイル名が空白のみのときは 400 + ErrorResponse。"""
    resp = client.post(
        "/ingest-file",
        files={"file": ("   ", b"some content", "text/plain")},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_ingest"
    assert "message" in body


def test_400_empty_file_content(client: TestClient):
    """空ファイル（0 バイト）は 400 + ErrorResponse。"""
    resp = _post_txt(client, content=b"")

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_ingest"


def test_400_extract_raises_value_error(client: TestClient):
    """extract_text が ValueError を上げたら 400 + ErrorResponse。"""
    with (
        patch("app.main.extract_text", side_effect=ValueError("文字コード不明")),
        patch("app.main.ingest_text", return_value=_MOCK_INGEST_RESULT),
        patch("app.main.storage"),
    ):
        resp = _post_txt(client)

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_ingest"


def test_400_empty_extracted_text(client: TestClient):
    """抽出結果が空白のみのときは 400 + ErrorResponse。"""
    with (
        patch("app.main.extract_text", return_value="   \n  "),
        patch("app.main.ingest_text", return_value=_MOCK_INGEST_RESULT),
        patch("app.main.storage"),
    ):
        resp = _post_txt(client)

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_ingest"


# ---------------------------------------------------------------------------
# 413 – ファイルが大きすぎる
# ---------------------------------------------------------------------------


def test_413_file_too_large(client: TestClient):
    """上限を超えるファイルサイズは 413 + ErrorResponse。"""
    with patch("app.main.UPLOAD_MAX_BYTES", 10):
        resp = _post_txt(client, content=b"x" * 11)

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"] == "file_too_large"


# ---------------------------------------------------------------------------
# 415 – 未対応形式
# ---------------------------------------------------------------------------


def test_415_unsupported_file_type(client: TestClient):
    """未対応拡張子は 415 + ErrorResponse。"""
    with (
        patch("app.main.extract_text", side_effect=UnsupportedFileType(".xyz")),
        patch("app.main.ingest_text", return_value=_MOCK_INGEST_RESULT),
        patch("app.main.storage"),
    ):
        resp = client.post(
            "/ingest-file",
            files={"file": ("document.xyz", b"binary content", "application/octet-stream")},
        )

    assert resp.status_code == 415
    body = resp.json()
    assert body["error"] == "unsupported_file_type"
