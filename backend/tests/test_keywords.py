"""app.keywords（名詞抽出）のテスト。janome だけに依存し、DB/APIは不要。"""
from app.keywords import extract_nouns, noun_text


def test_extracts_content_nouns_and_drops_particles():
    nouns = extract_nouns("有給休暇は何日もらえますか？")
    # 内容語（名詞）は残る
    assert "有給" in nouns
    assert "休暇" in nouns
    # 助詞「は」や活用語尾「もらえ」はノイズなので入らない
    assert "は" not in nouns
    assert "もらえ" not in nouns


def test_excludes_pronoun_sub_pos():
    # 「それ」は名詞-代名詞なので _EXCLUDED_SUB_POS で除外され、「テスト」だけ残る
    assert extract_nouns("それはテストです") == ["テスト"]


def test_plain_nouns_only():
    assert extract_nouns("会社の規定について") == ["会社", "規定"]


def test_empty_text_returns_empty_list():
    assert extract_nouns("") == []


def test_noun_text_joins_with_single_spaces_in_order():
    # 表層形が出現順に半角スペースで連結される（pg_trgm が語境界を認識しやすくするため）
    assert noun_text("有給休暇の申請") == "有給 休暇 申請"


def test_noun_text_empty_when_no_nouns():
    # 名詞が無ければ空文字（lexical_search 側でこの空を見て字面検索を打ち切る）
    assert noun_text("！？、。") == ""
