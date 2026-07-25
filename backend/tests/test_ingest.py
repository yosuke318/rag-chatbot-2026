"""app.ingest.extract_text のユニットテスト。

`.txt`(UTF-8) / `.csv`(cp932) / 未対応拡張子(UnsupportedFileType) /
`.pdf`（parsers に委譲）の各分岐を最小限でカバーする。
"""
from unittest.mock import patch

import pytest

from app.ingest import UnsupportedFileType, extract_text


def test_extract_text_txt_utf8():
    data = "こんにちは".encode("utf-8")
    assert extract_text("readme.txt", data) == "こんにちは"


def test_extract_text_csv_cp932():
    data = "名前,年齢\n太郎,30".encode("cp932")
    result = extract_text("data.csv", data)
    assert "名前" in result
    assert "太郎" in result


def test_extract_text_unsupported_extension():
    with pytest.raises(UnsupportedFileType) as exc_info:
        extract_text("archive.zip", b"PK\x03\x04dummy")
    assert exc_info.value.ext == ".zip"


def test_extract_text_pdf_delegates_to_parser():
    """PDF は parsers.PARSERS[".pdf"] に委譲されることを確認する。"""
    mock_parse = patch("app.parsers.PARSERS", {".pdf": lambda data: "PDF本文"})
    with mock_parse:
        result = extract_text("doc.pdf", b"%PDF-dummy")
    assert result == "PDF本文"
