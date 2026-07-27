"""contextual retrieval（チャンクへの文脈付与）のユニットテスト。

Claude は呼ばずにモックする。確認するのは:
  - 文脈の有無・フォールバック（見出しで代替）の分岐
  - 埋め込みに渡すテキストが「文脈 + 本文」になっていること
  - プロンプトキャッシュが効く並び（文書が先・チャンクが後）で組まれていること
"""
from unittest.mock import MagicMock, patch

from app import ingest, llm
from app.chunking import Chunk
from app.ingest import _embed_source, build_contexts

CHUNKS = [
    Chunk(text="10日付与される。", heading="第2章 休暇 > 第5条 年次有給休暇"),
    Chunk(text="翌年度まで繰り越せる。", heading="第2章 休暇 > 第6条 繰越"),
]


def test_build_contexts_uses_generated_text(monkeypatch):
    """contextual=True なら Claude が書いた文脈を使う。"""
    monkeypatch.setattr(
        ingest, "generate_chunk_contexts", lambda text, chunks: ["文脈A", "文脈B"]
    )
    assert build_contexts("全文", CHUNKS, contextual=True) == ["文脈A", "文脈B"]


def test_build_contexts_falls_back_to_heading_when_disabled(monkeypatch):
    """contextual=False なら Claude を呼ばず、見出しの階層で代用する。"""
    called = False

    def spy(text, chunks):  # 呼ばれたら失敗させる
        nonlocal called
        called = True
        return [""] * len(chunks)

    monkeypatch.setattr(ingest, "generate_chunk_contexts", spy)
    contexts = build_contexts("全文", CHUNKS, contextual=False)

    assert called is False
    assert contexts == [c.heading for c in CHUNKS]


def test_build_contexts_falls_back_per_chunk_on_failure(monkeypatch):
    """生成に失敗した（空が返った）チャンクだけ見出しにフォールバックする。"""
    monkeypatch.setattr(
        ingest, "generate_chunk_contexts", lambda text, chunks: ["文脈A", "  "]
    )
    contexts = build_contexts("全文", CHUNKS, contextual=True)
    assert contexts == ["文脈A", "第2章 休暇 > 第6条 繰越"]


def test_build_contexts_follows_setting_when_unspecified(monkeypatch):
    """contextual 未指定なら設定 USE_CONTEXTUAL_CHUNKING に従う。"""
    monkeypatch.setattr(ingest, "USE_CONTEXTUAL_CHUNKING", False)
    monkeypatch.setattr(
        ingest, "generate_chunk_contexts", lambda text, chunks: ["文脈A", "文脈B"]
    )
    assert build_contexts("全文", CHUNKS) == [c.heading for c in CHUNKS]


def test_embed_source_prepends_context():
    assert _embed_source("文脈", "本文") == "文脈\n\n本文"
    assert _embed_source("", "本文") == "本文"  # 文脈が無ければ本文のみ


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def test_generate_chunk_context_orders_document_before_chunk():
    """★キャッシュが効く並び★ 文書(毎回同じ)が先、チャンク(毎回変わる)が後。

    逆順だとプレフィックス一致が崩れてキャッシュが一切効かなくなるため、
    並びと cache_control の位置をテストで固定しておく。
    """
    with patch.object(llm, "_anthropic") as client:
        client.messages.create.return_value = _mock_response(" 有給休暇の付与日数の話。 ")
        result = llm.generate_chunk_context("文書全体", "チャンク本文")

    assert result == "有給休暇の付与日数の話。"  # 前後の空白は落とす

    blocks = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "文書全体" in blocks[0]["text"]
    assert "チャンク本文" in blocks[1]["text"]
    # キャッシュの区切りは文書ブロックの末尾（ここまでが毎回同じ内容）
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_generate_chunk_contexts_returns_empty_string_on_error(caplog):
    """1チャンクの失敗で取り込み全体を止めない。ただし黙って落とさず警告を出す。"""
    with patch.object(llm, "_anthropic") as client:
        client.messages.create.side_effect = [
            _mock_response("文脈A"),
            RuntimeError("rate limited"),
        ]
        with caplog.at_level("WARNING", logger="app.llm"):
            assert llm.generate_chunk_contexts("文書", ["c1", "c2"]) == ["文脈A", ""]

    assert "1/2" in caplog.text and "rate limited" in caplog.text


def test_generate_chunk_contexts_is_quiet_when_all_succeed(caplog):
    with patch.object(llm, "_anthropic") as client:
        client.messages.create.return_value = _mock_response("文脈")
        with caplog.at_level("WARNING", logger="app.llm"):
            llm.generate_chunk_contexts("文書", ["c1", "c2"])
    assert caplog.text == ""


def test_generate_chunk_contexts_empty_input_skips_api():
    with patch.object(llm, "_anthropic") as client:
        assert llm.generate_chunk_contexts("文書", []) == []
    client.messages.create.assert_not_called()
