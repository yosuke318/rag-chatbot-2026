"""app.chunking の構造分割のユニットテスト。

外部APIもDBも触らない純ロジック。確認するのは3点:
  - 条文・見出しの境界でチャンクが切れる（途中で切れない）
  - 短すぎる節は後ろとくっつく（1文チャンクを量産しない）
  - 長すぎる節だけ文の切れ目で二次分割される
"""
import pytest

from app import chunking
from app.chunking import Chunk, chunk_text, split_chunks


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    """閾値を小さくして、短いサンプルでも分割の挙動を確かめられるようにする。"""
    monkeypatch.setattr(chunking, "CHUNK_MAX_CHARS", 200)
    monkeypatch.setattr(chunking, "CHUNK_MIN_CHARS", 30)
    monkeypatch.setattr(chunking, "CHUNK_OVERLAP", 20)


REGULATION = """第2章 休暇

第5条 年次有給休暇
年次有給休暇は、入社から6か月継続勤務し、全労働日の8割以上出勤した従業員に10日付与される。
その後は勤続年数に応じて付与日数が増える。

第6条 特別休暇
慶弔に際しては特別休暇を付与する。日数は別表による。
"""


def test_splits_at_article_boundaries():
    """第N条の境界でチャンクが分かれ、条文が2つのチャンクにまたがらない。"""
    chunks = split_chunks(REGULATION)

    fifth = [c for c in chunks if "第5条" in c.text]
    assert len(fifth) == 1, "第5条が複数チャンクに割れている"
    # 第5条のチャンクに第6条の本文が混ざっていない（＝境界で切れている）
    assert "慶弔" not in fifth[0].text
    assert any("第6条" in c.text for c in chunks)


def test_heading_path_includes_chapter_and_article():
    """見出しの階層が「章 > 条」として記録される。"""
    chunks = split_chunks(REGULATION)
    fifth = next(c for c in chunks if "第5条" in c.text)
    assert fifth.heading == "第2章 休暇 > 第5条 年次有給休暇"


def test_short_sections_are_merged():
    """最小サイズ未満の節が単独チャンクにならず、後続とまとめられる。"""
    text = "第1条 目的\nこの規程は。\n\n第2条 定義\n用語は次のとおり。\n"
    chunks = split_chunks(text)
    assert len(chunks) == 1
    assert "第1条" in chunks[0].text and "第2条" in chunks[0].text


def test_short_sections_do_not_merge_across_chapters():
    """章をまたいだ合流はしない（無関係な章の内容が1チャンクに同居しない）。"""
    text = (
        "第3章 休日\n"
        "第9条 振替休日\n振替休日は翌月末までに取得すること。\n\n"
        "第4章 服務規律\n"
        "第11条 遵守事項\n業務上知り得た秘密を漏らしてはならない。\n"
    )
    chunks = split_chunks(text)

    holiday = next(c for c in chunks if "振替休日" in c.text)
    assert "秘密" not in holiday.text
    assert holiday.heading.startswith("第3章 休日")


def test_document_title_merges_into_first_chapter():
    """見出し前の文書タイトルは単独チャンクにせず、最初の章にくっつける。"""
    text = "就業規則（抜粋）\n\n第1章 総則\n第1条 目的\nこの規則は労働条件を定める。\n"
    chunks = split_chunks(text)
    assert len(chunks) == 1
    assert chunks[0].text.startswith("就業規則（抜粋）")


def test_long_section_is_split_at_sentence_boundary():
    """上限を超えた節だけ二次分割され、切れ目が文末にくる。"""
    body = "".join(f"これは第{i}文です。" for i in range(40))
    chunks = split_chunks(f"第1条 長い条文\n{body}")

    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text) <= chunking.CHUNK_MAX_CHARS
    # 最後のチャンク以外は文末（句点）で終わる
    for c in chunks[:-1]:
        assert c.text.endswith("。")


def test_markdown_headings_are_boundaries():
    """Markdown の見出しレベルも階層として扱う。"""
    text = (
        "# 経費精算規程\n"
        "本規程は経費の精算について定める。全社員に適用する。\n\n"
        "## 申請期限\n"
        "経費が発生した月の翌月10日までに申請すること。遅延は認めない。\n"
    )
    chunks = split_chunks(text)
    deadline = next(c for c in chunks if "翌月10日" in c.text)
    assert deadline.heading == "# 経費精算規程 > ## 申請期限"


def test_empty_text_returns_no_chunks():
    assert split_chunks("") == []
    assert split_chunks("   \n\n  ") == []


def test_plain_text_without_headings_still_chunks():
    """見出しが1つも無いプレーンテキストでも本文が失われない。"""
    text = "".join(f"文{i}です。" for i in range(60))
    chunks = split_chunks(text)
    assert chunks
    joined = "".join(c.text for c in chunks)
    for i in (0, 30, 59):
        assert f"文{i}です。" in joined


def test_chunk_text_returns_plain_strings():
    """薄いラッパは本文だけの文字列リストを返す。"""
    assert chunk_text(REGULATION) == [c.text for c in split_chunks(REGULATION)]
    assert all(isinstance(t, str) for t in chunk_text(REGULATION))


def test_chunk_dataclass_defaults_to_empty_heading():
    assert Chunk(text="本文").heading == ""
