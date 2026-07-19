"""名詞抽出。字面検索を「内容語だけ」で行うための前処理。

なぜ必要か:
  pg_trgm は文字3つ組の重なりを見るだけなので、「〜は」「〜もらえる」のような
  助詞・活用語尾までノイズとして計算に混ざる。さらに質問(短い)と本文(長い)の
  長さ差で類似度が押し下げられ、実測では正解チャンクでも 0.0102 しか出なかった。

対策:
  質問側・文書側の両方から名詞だけを取り出して突き合わせる。
  これで「名詞が一致したときだけ字面類似度が上がる」状態になる。
"""
from __future__ import annotations

from janome.tokenizer import Tokenizer

# 辞書のロードが重いのでモジュールロード時に1度だけ生成する
_tokenizer = Tokenizer()

# 名詞でも中身の薄いものは除く（「こと」「もの」「それ」「〜的」など）
_EXCLUDED_SUB_POS = {"非自立", "代名詞", "接尾", "接続詞的", "特殊"}


def extract_nouns(text: str) -> list[str]:
    """テキストから名詞の表層形を抽出する。"""
    nouns: list[str] = []
    for token in _tokenizer.tokenize(text):
        pos = token.part_of_speech.split(",")
        if pos[0] != "名詞":
            continue
        if len(pos) > 1 and pos[1] in _EXCLUDED_SUB_POS:
            continue
        nouns.append(token.surface)
    return nouns


def noun_text(text: str) -> str:
    """名詞だけを空白区切りで連結した文字列を返す。

    字面検索はこの文字列同士を比較する。空白で区切ることで
    pg_trgm が語の切れ目を認識しやすくなる。
    """
    return " ".join(extract_nouns(text))
