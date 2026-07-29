"""画像の検索対象化（5-2）のテスト。

案A（自動キャプション）と案B（マルチモーダル埋め込み）は eval で比べて選ぶもの
なので、両方が「同じ形の索引」を作り、切り替えだけで入れ替わることを確かめる。
Claude / Voyage は呼ばずに差し替える。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import parsers

ingest = pytest.importorskip("app.ingest")


def _image(label: str, data: bytes = b"png-bytes") -> parsers.ExtractedImage:
    return parsers.ExtractedImage(data, ".png", "image/png", label, 400, 300)


@pytest.fixture
def no_api(monkeypatch):
    """埋め込み・キャプションAPIを、呼ばれたら記録するだけの偽物に差し替える。"""
    calls: dict[str, list] = {"caption": [], "embed_texts": [], "embed_images": []}

    def fake_captions(images, source):
        calls["caption"].append((source, [label for _d, _t, label in images]))
        return [f"{label}の説明文。売上のグラフ。" for _d, _t, label in images]

    def fake_embed_texts(texts, input_type="document", retry_waits=None):
        calls["embed_texts"].append(list(texts))
        return [[0.1] * 8 for _ in texts]

    def fake_embed_images(datas, retry_waits=None):
        calls["embed_images"].append(list(datas))
        return [[0.2] * 8 for _ in datas]

    monkeypatch.setattr(ingest, "generate_image_captions", fake_captions)
    monkeypatch.setattr(ingest, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(ingest, "embed_images", fake_embed_images)
    return calls


# ---------------------------------------------------------------------------
# 案A: 自動キャプション
# ---------------------------------------------------------------------------


def test_caption_method_puts_description_on_the_text_path(no_api):
    """説明文が content に入り、名詞と埋め込みも付く＝既存の3手法で引ける。"""
    rows = ingest.build_image_index(
        "決算.pdf", [_image("1ページ目")], method="caption"
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["content"] == "1ページ目の説明文。売上のグラフ。"
    assert row["context"] == "1ページ目"
    assert row["content_nouns"]  # 字面検索・BM25が使う名詞列
    assert "売上" in row["content_nouns"]
    assert row["embedding"] is not None
    assert row["image_embedding"] is None  # 案Aでは画像ベクトルは作らない


def test_caption_method_embeds_label_with_the_caption(no_api):
    """埋め込む文字列にラベルを前置する（テキストチャンクの文脈付与と同じ狙い）。"""
    ingest.build_image_index("決算.pdf", [_image("3ページ目")], method="caption")

    embedded = no_api["embed_texts"][0][0]
    assert embedded.startswith("3ページ目")
    assert "説明文" in embedded


def test_caption_failure_leaves_image_unindexed(monkeypatch, no_api):
    """説明文が書けなかった画像は索引を持たない（＝検索に出ない）。

    ★空の説明文を埋め込まない★のが要点。ラベルだけのベクトルを作ると、
    中身と無関係な質問にその画像が当たりはじめる。
    """
    monkeypatch.setattr(
        ingest, "generate_image_captions", lambda images, source: ["", "  "]
    )

    rows = ingest.build_image_index(
        "決算.pdf", [_image("1ページ目"), _image("2ページ目")], method="caption"
    )

    assert no_api["embed_texts"] == []  # 埋め込みAPIすら呼ばない
    assert [r["embedding"] for r in rows] == [None, None]
    assert [r["content"] for r in rows] == ["[画像] 1ページ目", "[画像] 2ページ目"]
    assert [r["context"] for r in rows] == ["1ページ目", "2ページ目"]


def test_caption_partial_failure_indexes_the_rest(monkeypatch, no_api):
    """1枚失敗しても、書けた分だけは索引に載る。"""
    monkeypatch.setattr(
        ingest,
        "generate_image_captions",
        lambda images, source: ["", "2ページ目の説明文。"],
    )

    rows = ingest.build_image_index(
        "決算.pdf", [_image("1ページ目"), _image("2ページ目")], method="caption"
    )

    assert rows[0]["embedding"] is None
    assert rows[1]["embedding"] is not None
    assert rows[1]["content"] == "2ページ目の説明文。"
    # 埋め込みに渡すのは書けた1件だけ（空文字を混ぜない）
    assert len(no_api["embed_texts"][0]) == 1


# ---------------------------------------------------------------------------
# 案B: マルチモーダル埋め込み
# ---------------------------------------------------------------------------


def test_multimodal_method_embeds_the_image_itself(no_api):
    rows = ingest.build_image_index(
        "決算.pdf", [_image("1ページ目", b"raw")], method="multimodal"
    )

    assert no_api["embed_images"] == [[b"raw"]]
    assert no_api["caption"] == []  # Claudeは呼ばない
    row = rows[0]
    assert row["image_embedding"] is not None
    # ★テキスト側の列は空のまま★（別の空間なので embedding には入れられない）
    assert row["embedding"] is None
    assert row["content_nouns"] is None
    assert row["content"] == "[画像] 1ページ目"


# ---------------------------------------------------------------------------
# 方式の切り替えと失敗時の振る舞い
# ---------------------------------------------------------------------------


def test_none_method_keeps_storage_only(no_api):
    rows = ingest.build_image_index("決算.pdf", [_image("1ページ目")], method="none")

    assert no_api == {"caption": [], "embed_texts": [], "embed_images": []}
    assert rows[0]["embedding"] is None
    assert rows[0]["image_embedding"] is None


def test_unknown_method_indexes_nothing_instead_of_falling_back(no_api):
    """★設定のtypoで黙って案Aに落ちない★

    落ちてしまうと、案Bを測っているつもりの評価が案Aの数字を返す。
    """
    rows = ingest.build_image_index("決算.pdf", [_image("1ページ目")], method="multimodel")

    assert no_api == {"caption": [], "embed_texts": [], "embed_images": []}
    assert rows[0]["embedding"] is None
    assert rows[0]["image_embedding"] is None


def test_method_defaults_to_config(monkeypatch, no_api):
    monkeypatch.setattr(ingest, "IMAGE_INDEX_METHOD", "multimodal")

    ingest.build_image_index("決算.pdf", [_image("1ページ目")])

    assert no_api["embed_images"]  # 設定どおり案Bで索引された


def test_index_failure_does_not_break_ingest(monkeypatch, no_api):
    """APIエラーは「索引なしの画像」に落とす（文書登録ごと失敗させない）。"""
    monkeypatch.setattr(
        ingest,
        "generate_image_captions",
        lambda images, source: (_ for _ in ()).throw(RuntimeError("API down")),
    )

    rows = ingest.build_image_index(
        "決算.pdf", [_image("1ページ目")], method="caption"
    )

    assert rows[0]["embedding"] is None
    assert rows[0]["content"] == "[画像] 1ページ目"


def test_index_method_registry_matches_documented_values():
    assert ingest.IMAGE_INDEX_METHODS == ("caption", "multimodal", "none")


# ---------------------------------------------------------------------------
# 索引の作り直し（比較評価を回すための道具）
# ---------------------------------------------------------------------------


class _FakeConn:
    """reindex_images が投げるSQLを解釈する偽コネクション。"""

    def __init__(self, rows):
        self.rows = rows
        self.updates: list = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return type("R", (), {"fetchall": lambda _s: self.rows})()

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def executemany(self_inner, sql, params_seq):
                conn.updates.append((sql, list(params_seq)))

        return _Cur()


def test_reindex_reads_originals_from_s3_and_rewrites_the_index(monkeypatch, no_api):
    """ファイルを上げ直さずに索引だけ差し替えられる（画像原本はS3にある）。"""
    conn = _FakeConn(
        [
            (11, "images/決算.pdf/0001.png", "1ページ目", "決算.pdf"),
            (12, "images/決算.pdf/0002.png", "2ページ目", "決算.pdf"),
        ]
    )
    monkeypatch.setattr(ingest, "get_conn", conn)
    monkeypatch.setattr(
        ingest.storage, "get_object", lambda key: (b"bytes-" + key.encode(), "image/png")
    )

    result = ingest.reindex_images(method="multimodal")

    assert result == {"documents": 1, "images": 2, "indexed": 2}
    assert no_api["embed_images"]  # 指定した方式で作り直している
    sql, rows = conn.updates[0]
    assert sql.strip().upper().startswith("UPDATE")
    assert [r[-1] for r in rows] == [11, 12]  # WHERE id = %s は元の行を指す


def test_reindex_skips_images_missing_from_s3(monkeypatch, no_api):
    """S3から取れない画像は飛ばす（その行は前の索引のまま残る）。"""
    conn = _FakeConn(
        [
            (11, "images/決算.pdf/0001.png", "1ページ目", "決算.pdf"),
            (12, "images/決算.pdf/0002.png", "2ページ目", "決算.pdf"),
        ]
    )
    monkeypatch.setattr(ingest, "get_conn", conn)
    monkeypatch.setattr(
        ingest.storage,
        "get_object",
        lambda key: None if key.endswith("0001.png") else (b"ok", "image/png"),
    )

    result = ingest.reindex_images(method="caption")

    assert result == {"documents": 1, "images": 1, "indexed": 1}
    assert [r[-1] for r in conn.updates[0][1]] == [12]


def test_reindex_counts_only_images_that_actually_got_an_index(monkeypatch, no_api):
    """★索引を作れなかった枚数が分かること★

    レート制限で索引作成が失敗すると、その方式は「図を引けない」ように見える。
    実力差なのかAPIの失敗なのかを呼び出し側が区別できないと、比較評価の結論を
    間違える（実際に踏んだ）。
    """
    conn = _FakeConn([(11, "images/決算.pdf/0001.png", "1ページ目", "決算.pdf")])
    monkeypatch.setattr(ingest, "get_conn", conn)
    monkeypatch.setattr(ingest.storage, "get_object", lambda key: (b"ok", "image/png"))
    monkeypatch.setattr(
        ingest,
        "embed_images",
        lambda datas, retry_waits=None: (_ for _ in ()).throw(RuntimeError("429")),
    )

    result = ingest.reindex_images(method="multimodal")

    assert result == {"documents": 1, "images": 1, "indexed": 0}


def test_reindex_passes_retry_waits_to_the_embedding_api(monkeypatch, no_api):
    """作り直しは待ってでも完走させる（途中で諦めると索引なしの画像が残る）。"""
    conn = _FakeConn([(11, "images/決算.pdf/0001.png", "1ページ目", "決算.pdf")])
    monkeypatch.setattr(ingest, "get_conn", conn)
    monkeypatch.setattr(ingest.storage, "get_object", lambda key: (b"ok", "image/png"))
    seen: list = []
    monkeypatch.setattr(
        ingest,
        "embed_images",
        lambda datas, retry_waits=None: (seen.append(retry_waits), [[0.1]])[1],
    )

    ingest.reindex_images(method="multimodal", retry_waits=[20, 40])

    assert seen == [[20, 40]]


def test_reindex_does_nothing_without_image_chunks(monkeypatch, no_api):
    conn = _FakeConn([])
    monkeypatch.setattr(ingest, "get_conn", conn)

    assert ingest.reindex_images() == {"documents": 0, "images": 0, "indexed": 0}
    assert conn.updates == []


# ---------------------------------------------------------------------------
# /admin/reindex-images
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app import main as main_module

    with patch.object(main_module, "init_db"):
        with TestClient(main_module.app) as c:
            yield c


def test_reindex_endpoint_reports_method_and_counts(client):
    with patch(
        "app.main.reindex_images", return_value={"documents": 2, "images": 7}
    ) as spy:
        resp = client.post("/admin/reindex-images?method=multimodal")

    assert resp.status_code == 200
    assert resp.json() == {"method": "multimodal", "documents": 2, "images": 7}
    assert spy.call_args.args == ("multimodal",)


def test_reindex_endpoint_rejects_unknown_method(client):
    resp = client.post("/admin/reindex-images?method=multimodel")

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_image_index_method"


# ---------------------------------------------------------------------------
# 検索側: image 手法（案B）と、キャプション画像のBM25の扱い
# ---------------------------------------------------------------------------

retrieval = pytest.importorskip("app.retrieval")


class _SqlSpy:
    """投げられたSQLと値だけを記録する偽コネクション。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return type("R", (), {"fetchall": lambda _s: self.rows})()


def test_image_search_uses_the_multimodal_vector_not_the_text_one(monkeypatch):
    """★空間が違うので query_vec を使ってはいけない★

    取り違えてもSQLはエラーにならず（どちらも1024次元）、ただ無意味な順位が
    返るだけなので、テストで固定しておく。
    """
    spy = _SqlSpy(
        [(1, "[画像] 1ページ目", "決算.pdf", "images/決算.pdf/0001.png", "1ページ目", 0.2)]
    )
    monkeypatch.setattr(retrieval, "get_conn", spy)
    monkeypatch.setattr(
        retrieval, "embed_multimodal_queries", lambda texts: [["should-not-be-used"]]
    )

    hits = retrieval.image_search(
        "売上の推移は？", query_vec=[9.9], image_query_vec=[0.5], k=3
    )

    sql, params = spy.calls[0]
    assert "image_embedding IS NOT NULL" in sql
    assert params[0] == [0.5] and params[-2] == [0.5]  # 使うのは image_query_vec だけ
    assert hits[0]["cosine_similarity"] == pytest.approx(0.8)
    # 5-2の評価と5-3の回答生成が「これは画像チャンクだ」と判定する手がかり
    assert hits[0]["image_path"] == "images/決算.pdf/0001.png"


def test_image_search_embeds_the_question_when_not_given(monkeypatch):
    """単発の検索では自分で質問をベクトル化する（評価はまとめて渡す）。"""
    spy = _SqlSpy([])
    monkeypatch.setattr(retrieval, "get_conn", spy)
    asked: list[list[str]] = []
    monkeypatch.setattr(
        retrieval,
        "embed_multimodal_queries",
        lambda texts: (asked.append(list(texts)), [[0.7]])[1],
    )

    retrieval.image_search("売上の推移は？")

    assert asked == [["売上の推移は？"]]
    assert spy.calls[0][1][0] == [0.7]


def test_bm25_corpus_includes_captioned_images_but_not_bare_ones(monkeypatch):
    """語を持つ画像だけをBM25のコーパスに入れる。

    語数0の行を混ぜると N と avgdl が動き、画像を1枚入れただけで既存文書の
    スコアが変わる。説明文が付いた画像は普通のチャンクと同じ資格で入る。
    """
    spy = _SqlSpy([])
    monkeypatch.setattr(retrieval, "get_conn", spy)

    retrieval.bm25_search("売上の推移は？")

    sql = spy.calls[0][0]
    assert "c.image_path IS NULL OR c.content_nouns IS NOT NULL" in sql


def test_image_is_a_selectable_retriever():
    assert "image" in retrieval.RETRIEVERS
    assert retrieval.resolve_retrievers(["vector", "image"]) == ["vector", "image"]
