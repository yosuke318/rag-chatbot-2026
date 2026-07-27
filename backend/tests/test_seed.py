"""app.seed の再試行のユニットテスト。

埋め込みAPIは呼ばず、ingest_text をモックして分岐だけ確かめる。
`task seed` は文書数ぶん連続で埋め込みAPIを叩くため、無料枠(3 RPM)では
4件目から 429 が返る。そこで落ちずに待って再試行することがここの要件。
"""
import pytest
import voyageai

from app import seed


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """待ち時間はテストでは飛ばす（待ったかどうかだけ記録する）。"""
    waited: list[int] = []
    monkeypatch.setattr(seed.time, "sleep", lambda s: waited.append(s))
    monkeypatch.setattr(seed, "RETRY_WAITS", [20, 40])
    return waited


def _rate_limit() -> voyageai.error.RateLimitError:
    return voyageai.error.RateLimitError("3 RPM")


def test_retries_after_rate_limit_and_succeeds(monkeypatch, no_sleep):
    calls = []

    def fake_ingest(source, text):
        calls.append(source)
        if len(calls) == 1:
            raise _rate_limit()
        return {"chunks_created": 3, "replaced": 0}

    monkeypatch.setattr(seed, "ingest_text", fake_ingest)

    assert seed.ingest_with_retry("a.txt", "本文")["chunks_created"] == 3
    assert len(calls) == 2
    assert no_sleep == [20]  # 1回だけ待った


def test_gives_up_after_all_retries(monkeypatch, no_sleep):
    def always_limited(source, text):
        raise _rate_limit()

    monkeypatch.setattr(seed, "ingest_text", always_limited)

    with pytest.raises(voyageai.error.RateLimitError):
        seed.ingest_with_retry("a.txt", "本文")
    assert no_sleep == [20, 40]  # 待ちを使い切ってから諦める


def test_does_not_retry_other_errors(monkeypatch, no_sleep):
    """429 以外（認証エラー等）は待っても直らないので即座に投げる。"""
    calls = []

    def boom(source, text):
        calls.append(source)
        raise voyageai.error.AuthenticationError("bad key")

    monkeypatch.setattr(seed, "ingest_text", boom)

    with pytest.raises(voyageai.error.AuthenticationError):
        seed.ingest_with_retry("a.txt", "本文")
    assert len(calls) == 1
    assert no_sleep == []


def test_no_retry_when_first_attempt_succeeds(monkeypatch, no_sleep):
    monkeypatch.setattr(
        seed, "ingest_text", lambda source, text: {"chunks_created": 1, "replaced": 1}
    )
    assert seed.ingest_with_retry("a.txt", "本文")["replaced"] == 1
    assert no_sleep == []
