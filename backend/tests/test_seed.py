"""埋め込みAPIのレート制限に対する再試行のユニットテスト。

`task seed` は文書数ぶん連続で埋め込みAPIを叩くため、無料枠(3 RPM)では
4件目から 429 が返る。バッチ処理は落ちずに待って再試行することがここの要件。
再試行は埋め込み呼び出しだけを包む（文脈生成をやり直して Claude を無駄に
呼ばない）ので、テストも llm.embed_texts に対して書く。
"""
import json
from unittest.mock import MagicMock, patch

import pytest
import voyageai

from app import llm, seed


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """待ち時間は飛ばす（何秒待とうとしたかだけ記録する）。"""
    waited: list[int] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: waited.append(s))
    return waited


def _rate_limit() -> voyageai.error.RateLimitError:
    return voyageai.error.RateLimitError("3 RPM")


def _embeddings(n: int = 1) -> MagicMock:
    result = MagicMock()
    result.embeddings = [[0.1] * 4 for _ in range(n)]
    return result


def test_retries_after_rate_limit_and_succeeds(no_sleep):
    with patch.object(llm, "_voyage") as voyage:
        voyage.embed.side_effect = [_rate_limit(), _embeddings()]
        assert llm.embed_texts(["a"], retry_waits=[20, 40]) == [[0.1] * 4]

    assert voyage.embed.call_count == 2
    assert no_sleep == [20]  # 1回だけ待った


def test_gives_up_after_all_retries(no_sleep):
    with patch.object(llm, "_voyage") as voyage:
        voyage.embed.side_effect = _rate_limit()
        with pytest.raises(voyageai.error.RateLimitError):
            llm.embed_texts(["a"], retry_waits=[20, 40])

    assert no_sleep == [20, 40]  # 待ちを使い切ってから諦める


def test_does_not_retry_by_default(no_sleep):
    """既定(None)は再試行しない。APIリクエストの処理中に待たせないため。"""
    with patch.object(llm, "_voyage") as voyage:
        voyage.embed.side_effect = _rate_limit()
        with pytest.raises(voyageai.error.RateLimitError):
            llm.embed_texts(["a"])

    assert voyage.embed.call_count == 1
    assert no_sleep == []


def test_does_not_retry_other_errors(no_sleep):
    """429 以外（認証エラー等）は待っても直らないので即座に投げる。"""
    with patch.object(llm, "_voyage") as voyage:
        voyage.embed.side_effect = voyageai.error.AuthenticationError("bad key")
        with pytest.raises(voyageai.error.AuthenticationError):
            llm.embed_texts(["a"], retry_waits=[20, 40])

    assert voyage.embed.call_count == 1
    assert no_sleep == []


def test_seed_passes_retry_waits_to_ingest(monkeypatch, tmp_path):
    """app.seed は待ち時間を渡す（＝バッチだけが待つ）。"""
    (tmp_path / "a.txt").write_text("本文", encoding="utf-8")
    monkeypatch.setattr(seed, "SEED_DIR", tmp_path)
    monkeypatch.setattr(seed, "RETRY_WAITS", [5, 10])
    monkeypatch.setattr(seed, "init_db", lambda: None)
    monkeypatch.setattr("sys.argv", ["app.seed"])

    captured = {}

    def fake_ingest(source, text, project=None, topic=None, embed_retry_waits=None):
        captured["source"] = source
        captured["waits"] = embed_retry_waits
        return {"chunks_created": 1, "replaced": 0, "skipped": False}

    monkeypatch.setattr(seed, "ingest_text", fake_ingest)
    seed.main()

    assert captured == {"source": "a.txt", "waits": [5, 10]}


def test_seed_reports_skipped_documents(monkeypatch, tmp_path, capsys):
    """差分検知でスキップされた文書は「登録」と区別して出す（2回目以降の seed）。"""
    (tmp_path / "a.txt").write_text("本文", encoding="utf-8")
    monkeypatch.setattr(seed, "SEED_DIR", tmp_path)
    monkeypatch.setattr(seed, "init_db", lambda: None)
    monkeypatch.setattr("sys.argv", ["app.seed"])
    monkeypatch.setattr(
        seed,
        "ingest_text",
        lambda **kwargs: {"chunks_created": 3, "replaced": 0, "skipped": True},
    )

    seed.main()

    out = capsys.readouterr().out
    assert "変更なし" in out
    assert "チャンク登録" not in out


def test_load_scopes_maps_source_to_project_and_topic(tmp_path):
    manifest = tmp_path / "documents.json"
    manifest.write_text(
        json.dumps(
            [
                {"source": "a.txt", "project": "社内規程", "topic": "労務"},
                {"source": "b.txt", "project": "社内規程"},  # topic 省略可
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    scopes = seed.load_scopes(manifest)

    assert scopes["a.txt"] == {"project": "社内規程", "topic": "労務"}
    assert scopes["b.txt"] == {"project": "社内規程", "topic": None}


def test_load_scopes_without_manifest_is_empty(tmp_path):
    """マニフェストが無くても取り込みは動く（全文書が区分なしになるだけ）。"""
    assert seed.load_scopes(tmp_path / "nope.json") == {}


def test_seed_passes_scope_from_manifest(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("本文", encoding="utf-8")
    (tmp_path / "z.txt").write_text("本文", encoding="utf-8")  # マニフェスト未掲載
    monkeypatch.setattr(seed, "SEED_DIR", tmp_path)
    monkeypatch.setattr(seed, "init_db", lambda: None)
    monkeypatch.setattr("sys.argv", ["app.seed"])
    monkeypatch.setattr(
        seed,
        "load_scopes",
        # main() はどのマニフェストを読むかを引数で渡す（--corpus で切り替わる）
        lambda path=None: {"a.txt": {"project": "社内規程", "topic": "労務"}},
    )

    captured = {}

    def fake_ingest(source, text, project=None, topic=None, embed_retry_waits=None):
        captured[source] = (project, topic)
        return {"chunks_created": 1, "replaced": 0, "skipped": False}

    monkeypatch.setattr(seed, "ingest_text", fake_ingest)
    seed.main()

    assert captured["a.txt"] == ("社内規程", "労務")
    assert captured["z.txt"] == (None, None)  # 未掲載は区分なし
