"""原本画像を根拠にした回答生成のテスト。

★この段の主張★
  検索でヒットしたチャンクが図表なら、言語化テキストではなく画像そのものを
  Claude に渡す。言語化は「検索で見つけるための索引」に格下げし、判断は毎回
  原本に対して行わせる ＝ 言語化時に書かれなかったことも後から問える。

Anthropic SDK と S3 は呼ばずに差し替え、「何をClaudeに渡したか」を検査する。
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

llm = pytest.importorskip("app.llm")

ImageContext = llm.ImageContext


def _image(label: str = "3ページ目", data: bytes = b"png-bytes") -> ImageContext:
    return ImageContext(data=data, media_type="image/png", label=label)


# ---------------------------------------------------------------------------
# メッセージの組み立て
# ---------------------------------------------------------------------------


def test_text_only_prompt_is_unchanged_from_before_images_existed():
    """★画像が無いときのプロンプトは1文字も変えない★

    ここを変えると、画像機能と無関係に eval の数字が動いて過去の測定と
    比較できなくなる。テキストだけの経路は従来どおり1本の文字列で渡す。
    """
    messages = llm._answer_messages("有給は何日?", ["第5条 …", "第6条 …"])

    assert len(messages) == 1
    assert messages[0]["content"] == (
        "# コンテキスト\n[1] 第5条 …\n\n---\n\n[2] 第6条 …\n\n# 質問\n有給は何日?"
    )


def test_image_context_becomes_an_image_block_after_its_number():
    """番号を書いたテキスト → 画像、の順に置く（逆だと番号と画像が対応しない）。"""
    messages = llm._answer_messages(
        "満足度が一番高い地域は?", ["第1条 …", _image("3ページ目", b"raw")]
    )

    blocks = messages[0]["content"]
    assert isinstance(blocks, list)
    kinds = [b["type"] for b in blocks]
    assert kinds == ["text", "text", "text", "image", "text"]
    assert blocks[1]["text"] == "[1] 第1条 …"
    assert blocks[2]["text"] == "[2] 次の画像（3ページ目）"   # 画像の直前に番号と由来
    assert blocks[3]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[3]["source"]["data"]) == b"raw"
    assert blocks[4]["text"].endswith("満足度が一番高い地域は?")


def test_image_label_is_only_the_origin_not_a_description():
    """★ラベルは「どこの図か」だけ★

    中身の説明をここに書くと、Claude が画像を見ずにその説明文から答えてしまい、
    「原本で判断する」という主張が崩れる。
    """
    blocks = llm._context_blocks([_image("シート「売上」の画像1")])

    assert blocks[0]["text"] == "[1] 次の画像（シート「売上」の画像1）"


def test_numbering_is_shared_between_text_and_image_contexts():
    """引用マーカー [n] は種類によらず contexts の並びで決まる。"""
    blocks = llm._context_blocks(["A", _image(), "C"])

    texts = [b["text"] for b in blocks if b["type"] == "text"]
    assert texts[0].startswith("[1]")
    assert texts[1].startswith("[2]")
    assert texts[2].startswith("[3]")


def test_history_never_carries_images():
    """画像は今回の質問にだけ付ける（会話が続くほど入力が膨らむのを防ぐ）。"""
    history = [
        {"role": "user", "content": "前の質問"},
        {"role": "assistant", "content": "前の回答 [1]"},
    ]
    messages = llm._answer_messages("今回の質問", [_image()], history)

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "前の質問"
    assert messages[1]["content"] == "前の回答"  # 古い [1] は剥がす（従来どおり）
    assert isinstance(messages[2]["content"], list)  # 画像は今回の分だけ


# ---------------------------------------------------------------------------
# システムプロンプト
# ---------------------------------------------------------------------------


def test_system_prompt_gains_image_instructions_only_when_images_are_present():
    assert llm._system_prompt(["テキスト"]) == llm.SYSTEM_PROMPT
    with_image = llm._system_prompt([_image()])
    assert with_image.startswith(llm.SYSTEM_PROMPT)
    assert "画像を見て読み取って" in with_image


def test_image_instructions_forbid_guessing_from_the_label():
    """見出しから中身を推測させない＝原本に対して判断させる。"""
    assert "見出しから中身を推測しないでください" in llm.IMAGE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# generate_answer / stream_answer が実際に渡すもの
# ---------------------------------------------------------------------------


def test_generate_answer_sends_image_block_and_image_system_prompt(monkeypatch):
    fake = MagicMock()
    fake.messages.create.return_value = MagicMock(
        content=[MagicMock(type="text", text="東京が82で最も高いです [1]")]
    )
    monkeypatch.setattr(llm, "_anthropic", fake)
    monkeypatch.setattr(llm, "ANTHROPIC_API_KEY", "dummy")

    answer = llm.generate_answer("満足度が一番高い地域は?", [_image()])

    assert answer == "東京が82で最も高いです [1]"
    kwargs = fake.messages.create.call_args.kwargs
    assert "画像を見て読み取って" in kwargs["system"]
    assert any(
        b["type"] == "image" for b in kwargs["messages"][0]["content"]
    )


def test_stream_answer_sends_the_same_thing_as_generate_answer(monkeypatch):
    """ストリーミングでも渡すものは同じ（受け取り方だけが違う）。"""
    fake = MagicMock()
    stream_ctx = fake.messages.stream.return_value.__enter__.return_value
    stream_ctx.text_stream = iter(["東京", "が82"])
    monkeypatch.setattr(llm, "_anthropic", fake)
    monkeypatch.setattr(llm, "ANTHROPIC_API_KEY", "dummy")

    assert "".join(llm.stream_answer("q", [_image()])) == "東京が82"

    kwargs = fake.messages.stream.call_args.kwargs
    assert "画像を見て読み取って" in kwargs["system"]
    assert any(b["type"] == "image" for b in kwargs["messages"][0]["content"])


# ---------------------------------------------------------------------------
# S3から画像を取ってコンテキストに載せるところ（app.main）
# ---------------------------------------------------------------------------

pytest.importorskip("psycopg")
main_module = pytest.importorskip("app.main")


def _hit(image_path=None, content="本文", context=None, hit_id=1):
    return {
        "id": hit_id,
        "content": content,
        "source": "決算.pdf",
        "image_path": image_path,
        "context": context,
    }


def test_image_chunk_is_replaced_by_the_original_image(monkeypatch):
    """★言語化テキストではなく原本画像を渡す★（この機能の核心）。"""
    monkeypatch.setattr(
        main_module.storage, "get_object", lambda key: (b"raw-png", "image/png")
    )

    contexts = main_module._answer_contexts(
        [
            _hit(content="第1条 …"),
            _hit(
                image_path="images/決算.pdf/0003.png",
                content="売上のグラフ。",
                context="3ページ目",
            ),
        ]
    )

    assert contexts[0] == "第1条 …"
    assert isinstance(contexts[1], ImageContext)
    assert contexts[1].data == b"raw-png"
    assert contexts[1].label == "3ページ目"
    # キャプション（"売上のグラフ。"）はコンテキストに現れない＝索引に格下げ
    assert "売上のグラフ" not in str(contexts)


def test_falls_back_to_the_caption_when_the_image_is_missing(monkeypatch):
    """S3から取れなくても回答は成立させる（言語化テキストで答える品質に落ちるだけ）。"""
    monkeypatch.setattr(main_module.storage, "get_object", lambda key: None)

    contexts = main_module._answer_contexts(
        [_hit(image_path="images/決算.pdf/0003.png", content="売上のグラフ。")]
    )

    assert contexts == ["売上のグラフ。"]


def test_oversized_image_falls_back_instead_of_failing_the_request(monkeypatch):
    """Claudeの画像上限を超えるとリクエストごと失敗するので、手前で弾く。"""
    monkeypatch.setattr(main_module, "ANSWER_IMAGE_MAX_BYTES", 10)
    monkeypatch.setattr(
        main_module.storage, "get_object", lambda key: (b"x" * 11, "image/png")
    )

    contexts = main_module._answer_contexts(
        [_hit(image_path="images/決算.pdf/0003.png", content="売上のグラフ。")]
    )

    assert contexts == ["売上のグラフ。"]


def test_unsupported_media_type_falls_back(monkeypatch):
    monkeypatch.setattr(
        main_module.storage, "get_object", lambda key: (b"raw", "image/tiff")
    )

    contexts = main_module._answer_contexts(
        [_hit(image_path="images/決算.pdf/0003.png", content="売上のグラフ。")]
    )

    assert contexts == ["売上のグラフ。"]


def test_attaches_at_most_the_configured_number_of_images(monkeypatch):
    """上限を超えた分は言語化テキストで渡す（入力トークンとコストの保護）。"""
    monkeypatch.setattr(main_module, "ANSWER_MAX_IMAGES", 2)
    monkeypatch.setattr(
        main_module.storage, "get_object", lambda key: (b"raw", "image/png")
    )

    hits = [
        _hit(image_path=f"images/決算.pdf/000{i}.png", content=f"図{i}の説明", hit_id=i)
        for i in range(1, 5)
    ]
    contexts = main_module._answer_contexts(hits)

    # 順位が上の2件だけ画像になり、残りはテキストのまま
    assert [isinstance(c, ImageContext) for c in contexts] == [True, True, False, False]
    assert contexts[2] == "図3の説明"


def test_contexts_stay_aligned_with_hits_so_citation_numbers_hold(monkeypatch):
    """★並びは hits と1対1★ ずれると [n] が別のチャンクを指す。"""
    monkeypatch.setattr(
        main_module.storage, "get_object", lambda key: (b"raw", "image/png")
    )
    hits = [_hit(hit_id=1), _hit(image_path="images/a/0001.png", hit_id=2), _hit(hit_id=3)]

    assert len(main_module._answer_contexts(hits)) == len(hits)


# ---------------------------------------------------------------------------
# 引用（利用者が原本画像を確かめられるか）
# ---------------------------------------------------------------------------


def test_citation_of_an_image_chunk_links_to_that_image(monkeypatch):
    """回答生成に渡したのと同じ1枚を利用者にも見せる。"""
    monkeypatch.setattr(
        main_module.storage, "file_url", lambda key: f"/files/{key}"
    )

    citations = main_module._citations(
        [_hit(image_path="images/決算.pdf/0003.png", context="3ページ目")]
    )

    assert citations[0]["image_url"] == "/files/images/決算.pdf/0003.png"
    assert citations[0]["image_label"] == "3ページ目"
    # 文書の原本URLとは別物（どのページの図かは image_url でしか分からない）
    assert citations[0]["file_url"] == "/files/決算.pdf"


def test_citation_of_a_text_chunk_has_no_image_fields(monkeypatch):
    monkeypatch.setattr(main_module.storage, "file_url", lambda key: f"/files/{key}")

    citations = main_module._citations([_hit()])

    assert citations[0]["image_url"] is None
    assert citations[0]["image_label"] is None


def test_citations_and_contexts_use_the_same_hit_order(monkeypatch):
    """引用番号 [n] と contexts[n-1] の対応は画像が混ざっても保たれる。"""
    monkeypatch.setattr(main_module.storage, "file_url", lambda key: f"/files/{key}")
    monkeypatch.setattr(
        main_module.storage, "get_object", lambda key: (b"raw", "image/png")
    )
    hits = [
        _hit(hit_id=11),
        _hit(image_path="images/決算.pdf/0003.png", context="3ページ目", hit_id=12),
    ]

    citations = main_module._citations(hits)
    contexts = main_module._answer_contexts(hits)

    assert [c["n"] for c in citations] == [1, 2]
    assert [c["chunk_id"] for c in citations] == [11, 12]
    assert isinstance(contexts[1], ImageContext)  # [2] の根拠が画像


def test_prepare_answer_puts_images_into_the_contexts(monkeypatch):
    """/chat と /chat/stream の共通の手前処理まで通して画像が載ること。"""
    hits = [_hit(image_path="images/決算.pdf/0003.png", context="3ページ目")]
    with (
        patch.object(main_module, "hybrid_search", return_value=hits),
        patch.object(main_module.conversations, "resolve", return_value=1),
        patch.object(main_module.conversations, "load_history", return_value=[]),
        patch.object(main_module.conversations, "add_message"),
        patch.object(main_module.storage, "file_url", return_value=None),
        patch.object(
            main_module.storage, "get_object", return_value=(b"raw", "image/png")
        ),
    ):
        prepared = main_module._prepare_answer(
            main_module.ChatRequest(question="満足度が一番高い地域は?")
        )

    assert isinstance(prepared["contexts"][0], ImageContext)
