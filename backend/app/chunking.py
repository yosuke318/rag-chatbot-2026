"""チャンク分割: 文書の「構造」で切る。★検索精度の土台★

なぜ必要か:
  文字数1000で機械的に切ると「第3条（有給休暇）」の途中で切れる。
  切れたチャンクは、前半に条件・後半に例外……と意味が割れるため、
  どちらを引いても質問に答えられない。検索精度の上限がここで決まる。

やること:
  1) 見出し・条番号（第N条、N. など）の行を境界として「節」に分ける
  2) 短すぎる節は後ろとくっつける（1文だけのチャンクを量産しない）
  3) 長すぎる節だけ、文の切れ目で二次分割する（ここだけオーバーラップを使う）

出力の Chunk は本文(text)と見出しの階層(heading)を持つ。
heading は contextual retrieval（app.llm.generate_chunk_contexts）が使えない
ときの代替文脈として ingest 側で使う。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import CHUNK_MAX_CHARS, CHUNK_MIN_CHARS, CHUNK_OVERLAP


@dataclass
class Chunk:
    """1チャンク。

    text=本文、heading=その位置の見出し階層（「第2章 休暇 > 第5条 年次有給休暇」）。
    """

    text: str
    heading: str = ""


# --- 見出しの判定 -------------------------------------------------------------
# (正規表現, 階層レベル) の並び。レベルが小さいほど上位の見出し。
# 数字は半角/全角/漢数字のいずれも拾う（規程類は表記が揺れるため）。
_NUM = r"[0-9０-９一二三四五六七八九十百千]+"

_HEADING_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(rf"^第{_NUM}編(\s|　|$)"), 1),
    (re.compile(rf"^第{_NUM}章(\s|　|$)"), 2),
    (re.compile(rf"^第{_NUM}節(\s|　|$)"), 3),
    (re.compile(rf"^第{_NUM}条(の{_NUM})?(\s|　|$|（|\()"), 4),
    # 【総則】のような角括弧見出し、■/◆ で始まる見出し
    (re.compile(r"^[【〔\[].{1,40}[】〕\]]\s*$"), 3),
    (re.compile(r"^[■◆●▼]\s*\S"), 4),
    # 1.1 見出し / 1. 見出し（箇条書きの番号。条文が無い社内文書向け）
    (re.compile(r"^[0-9]+\.[0-9]+[\.\s　]"), 5),
    (re.compile(r"^[0-9]+[\.．][\s　]"), 4),
]

# Markdown 見出しは「#の数」がそのまま階層になるので別扱い
_MD_HEADING = re.compile(r"^(#{1,6})\s+\S")

# 二次分割で使う文の区切り（句点・感嘆符・疑問符のあと）。
# 閉じ括弧は入れない。「〜と定める。」のような引用の途中で切れるため。
# 「(?<=...)」は区切り文字を前の文に残したまま切るための後読み。
_SENTENCE_END = re.compile(r"(?<=[。．！？!?])")


def _heading_level(line: str) -> int | None:
    """その行が見出しならレベル(1が最上位)、見出しでなければ None。"""
    stripped = line.strip()
    if not stripped:
        return None

    md = _MD_HEADING.match(stripped)
    if md:
        return len(md.group(1))

    for pattern, level in _HEADING_PATTERNS:
        if pattern.match(stripped):
            return level
    return None


# --- 節への分割 ---------------------------------------------------------------


@dataclass
class _Section:
    """見出し1つとその配下の本文。"""

    level: int  # 見出しの階層。見出しを持たない冒頭部は _NO_HEADING
    title: str  # 見出し行そのもの（見出しが無ければ空）
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


_NO_HEADING = 99  # 「見出しが付いていない本文」を表す番兵レベル

# ここより浅い見出し（編・章・節、Markdown の H1〜H3）は「話題の変わり目」とみなし、
# 短すぎる節の合流をここで打ち切る。これが無いと「第3章の末尾の条 + 第4章 + その
# 配下の条」が1チャンクに混ざり、章をまたいだ無関係な内容が同居してしまう。
_MAJOR_HEADING_LEVEL = 3


def _split_sections(text: str) -> list[_Section]:
    """見出し行を境界にして本文を節へ分ける。"""
    sections: list[_Section] = []
    current = _Section(level=_NO_HEADING, title="", lines=[])

    for line in text.splitlines():
        level = _heading_level(line)
        if level is None:
            current.lines.append(line)
            continue
        # 見出しに当たった → ここまでを1節として確定し、新しい節を始める
        if current.text:
            sections.append(current)
        current = _Section(level=level, title=line.strip(), lines=[line])

    if current.text:
        sections.append(current)
    return sections


def _heading_path(stack: list[_Section]) -> str:
    """見出しスタックを「第2章 休暇 > 第5条 年次有給休暇」形式に整形する。"""
    return " > ".join(s.title for s in stack if s.title)


# --- 長い節の二次分割 ---------------------------------------------------------


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """max_chars を超える節を、文の切れ目で分ける（末尾を overlap 分だけ重ねる）。

    重ねるのは「分割点をまたぐ文脈」を両側に残すため。構造で切れている
    通常のチャンクにはオーバーラップを付けない（重複が検索結果を汚すため）、
    ここだけの措置。
    """
    sentences = [s for s in _SENTENCE_END.split(text) if s]
    chunks: list[str] = []
    buffer = ""

    for sentence in sentences:
        # 1文だけで上限を超える場合は諦めて文字数で割る（表・長いURL等）
        if len(sentence) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for i in range(0, len(sentence), max_chars):
                chunks.append(sentence[i : i + max_chars])
            continue

        if len(buffer) + len(sentence) > max_chars and buffer:
            chunks.append(buffer)
            buffer = buffer[-overlap:] if overlap else ""
        buffer += sentence

    if buffer.strip():
        chunks.append(buffer)
    return [c.strip() for c in chunks if c.strip()]


# --- 本体 ---------------------------------------------------------------------


def split_chunks(text: str) -> list[Chunk]:
    """構造で分割したチャンク列を返す。

    分割の判断は2つだけ:
      - 節が CHUNK_MIN_CHARS 未満なら、次の節とくっつける（断片化を防ぐ）
      - 節が CHUNK_MAX_CHARS を超えたら、文の切れ目で二次分割する
    それ以外は「1つの見出し = 1チャンク」。条文の途中では切れない。
    """
    text = text.strip()
    if not text:
        return []

    sections = _split_sections(text)
    chunks: list[Chunk] = []

    stack: list[_Section] = []  # 現在位置の見出し階層
    pending: list[str] = []     # まだ最小サイズに満たない、繰り越し中の本文
    pending_heading = ""

    def flush() -> None:
        """繰り越し中の本文を確定してチャンクにする。"""
        nonlocal pending, pending_heading
        body = "\n\n".join(pending).strip()
        pending = []
        if not body:
            pending_heading = ""
            return
        if len(body) > CHUNK_MAX_CHARS:
            chunks.extend(
                Chunk(text=part, heading=pending_heading)
                for part in _split_long(body, CHUNK_MAX_CHARS, CHUNK_OVERLAP)
            )
        else:
            chunks.append(Chunk(text=body, heading=pending_heading))
        pending_heading = ""

    for section in sections:
        # 章・節に入ったら、繰り越し中の（最小サイズ未満の）本文はそこで打ち切る。
        # サイズを揃えることより、章をまたいで内容が混ざらないことを優先する。
        # ただし見出しがまだ1つも出ていない部分（文書タイトル等）は打ち切らず、
        # 最初の章に合流させる（タイトルだけの数文字チャンクを作らないため）。
        if section.level <= _MAJOR_HEADING_LEVEL and pending and pending_heading:
            flush()

        # 見出しスタックを更新（同じか上位の見出しが来たら、その分だけ戻す）
        if section.level != _NO_HEADING:
            while stack and stack[-1].level >= section.level:
                stack.pop()
            stack.append(section)

        # 見出しは「そのチャンクが属する最も深い階層」を持たせる。
        # 章見出しだけの短い節に条文が合流したときは、章ではなく条まで伸ばす
        # （合流相手が兄弟の見出しなら、最初の見出しのまま据え置く）。
        path = _heading_path(stack)
        if not pending or (path and path.startswith(pending_heading)):
            pending_heading = path
        pending.append(section.text)

        # 繰り越し分が最小サイズに達したら確定。達していなければ次の節と合流する
        if sum(len(p) for p in pending) >= CHUNK_MIN_CHARS:
            flush()

    flush()  # 末尾の端数
    return chunks


def chunk_text(text: str) -> list[str]:
    """split_chunks の本文だけを返す薄いラッパ（本文だけ欲しい呼び出し向け）。"""
    return [c.text for c in split_chunks(text)]
