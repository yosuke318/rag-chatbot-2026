"""ファイル → プレーンテキスト の抽出（PDF / XLSX / PPTX）。

ingest.extract_text から拡張子ごとに呼ばれる。検索・埋め込みに使うのは
「テキスト」なので、ここでは体裁を捨てて本文だけを取り出すことに徹する。

方針:
  - 重いライブラリ（pypdf / openpyxl / python-pptx）は関数内で遅延importする。
    テキスト貼り付けしか使わない構成では読み込まれないようにするため（storage の
    boto3 と同じ方針）。
  - 解析に失敗したら ValueError を投げる。呼び出し側(/ingest-file)は400にまとめる。
  - スキャンPDFのように文字が取れないファイルは空文字を返す。呼び出し側が
    「本文が空」として弾く。
"""
from __future__ import annotations

import io


def parse_pdf(data: bytes) -> str:
    """PDFの各ページからテキストを抽出して連結する。

    テキストレイヤを持たない（スキャン画像の）PDFは空になる。OCRは範囲外。
    パスワード無しで開ける暗号化PDFは開く（空パスワードで復号を試みる）。
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # 空パスワードで開けるものだけ対応（本当に保護されたものは失敗させる）
            reader.decrypt("")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except PdfReadError as exc:
        raise ValueError(f"PDFを解析できませんでした: {exc}") from exc
    # ページ境界は空行で区切る（チャンク分割時に文がまたがりにくくする）
    return "\n\n".join(p for p in pages if p)


def parse_xlsx(data: bytes) -> str:
    """XLSXの各シートのセル値を、シートごとにタブ区切りで並べる。

    data_only=True で数式は「計算済みの値」を取る（式そのものは検索に無意味なため）。
    read_only=True で大きなブックでもメモリを抑える。
    """
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl は種々の例外を投げるためまとめて包む
        raise ValueError(f"XLSXを解析できませんでした: {exc}") from exc

    lines: list[str] = []
    try:
        for ws in wb.worksheets:
            lines.append(f"# {ws.title}")  # シート名は文脈になるので残す
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    lines.append("\t".join(cells))
    finally:
        wb.close()
    return "\n".join(lines)


def parse_pptx(data: bytes) -> str:
    """PPTXの各スライドから、図形内テキストと表のセルを抽出する。"""
    from pptx import Presentation

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise ValueError(f"PPTXを解析できませんでした: {exc}") from exc

    lines: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        lines.append(f"# Slide {i}")
        for shape in slide.shapes:
            # テキストを持つ図形（タイトル・本文プレースホルダ・テキストボックス）
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
            # 表はセルを行ごとにタブ区切りで
            if shape.has_table:
                for r in shape.table.rows:
                    cells = [cell.text.strip() for cell in r.cells]
                    lines.append("\t".join(cells))
    return "\n".join(lines)


# 拡張子 → パーサ。ingest.extract_text がこの表を引く。
PARSERS = {
    ".pdf": parse_pdf,
    ".xlsx": parse_xlsx,
    ".pptx": parse_pptx,
}
