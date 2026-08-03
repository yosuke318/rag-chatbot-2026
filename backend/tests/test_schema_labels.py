"""論理名（app.schema_labels）と GET /schema のテスト。

★このファイルの主役は「漏れ検出」★
  論理名はコードから参照されないただの定数なので、カラムを足したときに
  一緒に足し忘れても何も壊れない ─ 気づくのは、UIに物理名（`content_nouns`）が
  そのまま出てからになる。そこで db.py のDDLを読んで突き合わせ、
  ★論理名の無いカラムがあればここで落とす★。

DBには繋がない。db.py はソースとして読むだけ。
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from app import db as db_module  # noqa: E402
from app import main as main_module  # noqa: E402
from app.schema_labels import (  # noqa: E402
    SCHEMA_LABELS,
    column_label,
    table_label,
)

# CREATE TABLE IF NOT EXISTS <名前> ( ... ); を丸ごと拾う
_CREATE = re.compile(
    r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\s*\);", re.DOTALL
)
# 本体の中の1カラム＝「小文字の識別子 + 空白 + 大文字で始まる型」。
# `REFERENCES ...` のような継続行（大文字始まり）と `-- コメント` は弾かれる。
_COLUMN = re.compile(r"^\s{2,}([a-z_][a-z0-9_]*)\s+[A-Z]", re.MULTILINE)
_ADD = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS\s+(\w+)")
_DROP = re.compile(r"ALTER TABLE (\w+) DROP COLUMN (\w+)")
# 隣り合う文字列リテラルの継ぎ目（`"` 改行 `"` / `"` 改行 `f"`）。
# db.py は長いSQLを複数のリテラルに割って書くので、素のソースだと
# `ADD COLUMN IF NOT EXISTS "` `"expected_kind ...` のように文の途中で切れ、
# 1本の正規表現では拾えない。Pythonが実行時に行う連結をここでも先にやる。
_JOIN = re.compile(r'"\s*f?"')


def _ddl_columns() -> dict[str, set[str]]:
    """db.py のDDLから {テーブル: カラム集合} を組み立てる。

    CREATE の本体と ADD COLUMN を足し、DROP COLUMN を引く。DROP を引くのは、
    TEXT時代の project / topic のように★マイグレーションの途中でだけ存在する★
    カラムがあるため（最終的なスキーマには無いので論理名も要らない）。
    """
    source = _JOIN.sub("", Path(db_module.__file__).read_text(encoding="utf-8"))
    tables: dict[str, set[str]] = {}
    for name, body in _CREATE.findall(source):
        tables[name] = set(_COLUMN.findall(body))
    for table, column in _ADD.findall(source):
        tables.setdefault(table, set()).add(column)
    for table, column in _DROP.findall(source):
        tables.get(table, set()).discard(column)
    return tables


def test_ddl_parser_finds_the_known_tables():
    """パーサ自体の健全性。★これが空振りすると漏れ検出が素通りする★

    下の2つのテストは「DDLから読んだ集合」と突き合わせる形なので、
    パーサが何も拾えないと全部パスしてしまう。まず拾えていることを固定する。
    """
    tables = _ddl_columns()

    assert "documents" in tables
    # CREATE の列 + ALTER で足した content_hash。TEXT時代の project/topic は
    # DROP されるので入らない。
    assert tables["documents"] == {
        "id",
        "source",
        "project_id",
        "topic_id",
        "created_at",
        "content_hash",
    }
    # 継続行（REFERENCES conversations(id) ...）をカラムと誤認していないこと
    assert tables["messages"] == {
        "id",
        "conversation_id",
        "role",
        "content",
        "sources",
        "created_at",
    }
    # ★リテラルが割れている ALTER も拾えること★
    #   expected_kind は "… IF NOT EXISTS " と "expected_kind TEXT …" の
    #   2リテラルに割れて書かれている。ここを取りこぼすと、CREATE にしか
    #   現れないカラムだけが検査対象になり、漏れ検出がざるになる。
    assert "expected_kind" in tables["eval_questions"]


def test_every_ddl_column_has_a_label():
    """★カラムを足したら論理名も足す★ を強制する。

    ここが落ちたら app/schema_labels.py に1行足すこと。物理名のままUIに出ると、
    利用者には `expected_kind` が何のことか分からない。
    """
    missing: list[str] = []
    for table, columns in _ddl_columns().items():
        entry = SCHEMA_LABELS.get(table)
        if entry is None:
            missing.append(f"{table}（テーブルごと未登録）")
            continue
        for column in sorted(columns):
            if column not in entry["columns"]:
                missing.append(f"{table}.{column}")

    assert not missing, "論理名が未登録: " + ", ".join(missing)


def test_no_label_for_a_column_that_does_not_exist():
    """逆向き。消したカラムの論理名が残っていると、定義書に幽霊が載る。"""
    ddl = _ddl_columns()
    stale: list[str] = []
    for table, entry in SCHEMA_LABELS.items():
        if table not in ddl:
            stale.append(f"{table}（DDLに無いテーブル）")
            continue
        for column in entry["columns"]:
            if column not in ddl[table]:
                stale.append(f"{table}.{column}")

    assert not stale, "DDLに無いのに論理名がある: " + ", ".join(stale)


def test_labels_are_not_empty_and_differ_from_physical_name():
    """空文字や物理名のコピペを論理名として通さない（付けた意味が無くなる）。"""
    for table, entry in SCHEMA_LABELS.items():
        assert entry["label"].strip(), f"{table} の論理名が空"
        assert entry["label"] != table, f"{table} の論理名が物理名のまま"
        for column, label in entry["columns"].items():
            assert label.strip(), f"{table}.{column} の論理名が空"
            assert label != column, f"{table}.{column} の論理名が物理名のまま"


def test_lookup_helpers_fall_back_to_physical_name():
    """未登録でも例外にせず物理名を返す（表示の飾りで実行時を止めない）。"""
    assert table_label("documents") == "文書"
    assert column_label("documents", "source") == "文書名"
    assert table_label("no_such_table") == "no_such_table"
    assert column_label("documents", "no_such_column") == "no_such_column"
    assert column_label("no_such_table", "whatever") == "whatever"


@pytest.fixture(scope="module")
def client():
    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def test_schema_endpoint_returns_all_tables(client):
    """GET /schema は辞書をそのまま返す。★DBに繋がない★ ので常に同じ答。"""
    res = client.get("/schema")
    assert res.status_code == 200, res.text
    body = res.json()

    names = [t["name"] for t in body["tables"]]
    assert names == list(SCHEMA_LABELS)  # DDLと同じ作成順のまま返る

    documents = next(t for t in body["tables"] if t["name"] == "documents")
    assert documents["label"] == "文書"
    assert {"name": "source", "label": "文書名"} in documents["columns"]
    # カラムの並びも辞書のまま（＝DDLの並び）。id が先頭に来る。
    assert documents["columns"][0]["name"] == "id"
