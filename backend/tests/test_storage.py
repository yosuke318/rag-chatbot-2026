"""原本URL（storage.file_url）のテスト。

根拠から原本へ飛ぶURLは環境で形が変わる（ローカルMinIO=backend中継 /
実S3=署名URL）。ここを間違えるとUIから原本が開けなくなるので、
どちらの形になるかを固定しておく。S3には接続せずモックする。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

storage = pytest.importorskip("app.storage")


def test_file_url_is_none_when_original_is_missing(monkeypatch):
    monkeypatch.setattr(storage, "exists", lambda source: False)
    assert storage.file_url("無い.txt") is None


def test_file_url_uses_relay_path_for_local_minio(monkeypatch):
    """MinIO(S3_ENDPOINT_URL あり)では署名URLを使わない。

    署名URLのホストが docker 内の名前(minio:9000)になり、ブラウザから
    名前解決できないため。代わりに backend 中継の /files/... を返す。
    """
    monkeypatch.setattr(storage, "exists", lambda source: True)
    monkeypatch.setattr(storage, "S3_ENDPOINT_URL", "http://minio:9000")

    # 日本語のファイル名もURLとして壊れないこと
    assert storage.file_url("有給休暇.txt") == "/files/%E6%9C%89%E7%B5%A6%E4%BC%91%E6%9A%87.txt"


def test_file_url_signs_when_using_real_s3(monkeypatch):
    monkeypatch.setattr(storage, "exists", lambda source: True)
    monkeypatch.setattr(storage, "S3_ENDPOINT_URL", None)
    monkeypatch.setattr(storage, "S3_BUCKET", "rag-docs")
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://s3.example.com/signed"
    with patch.object(storage, "_client", lambda: client):
        assert storage.file_url("a.pdf") == "https://s3.example.com/signed"

    kwargs = client.generate_presigned_url.call_args.kwargs
    assert kwargs["Params"] == {"Bucket": "rag-docs", "Key": "a.pdf"}
    assert kwargs["ExpiresIn"] == storage.SIGNED_URL_TTL


def test_file_url_returns_none_when_signing_fails(monkeypatch, caplog):
    """署名に失敗しても回答自体は返したいので、例外は握って None にする。"""
    monkeypatch.setattr(storage, "exists", lambda source: True)
    monkeypatch.setattr(storage, "S3_ENDPOINT_URL", None)
    client = MagicMock()
    client.generate_presigned_url.side_effect = RuntimeError("no credentials")
    with patch.object(storage, "_client", lambda: client):
        with caplog.at_level("WARNING", logger="app.storage"):
            assert storage.file_url("a.pdf") is None

    assert "署名URL" in caplog.text  # 黙って落とさない
