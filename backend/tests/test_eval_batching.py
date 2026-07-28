"""evaluate() が質問のベクトル化を1リクエストにまとめることのテスト。

1問ずつ埋め込むと「質問数 = APIリクエスト数」になり、Voyage 無料枠(3 RPM)では
4問目で 429 になって評価が完走しない。DBの質問が増えても壊れないよう、
まとめ方をテストで固定する。DB・外部APIは触らずモックする。
"""
import pytest

from app import eval as eval_module

GOLD = [
    {"question": "Q1", "expected_source": "a.txt"},
    {"question": "Q2", "expected_source": "b.txt"},
    {"question": "Q3", "expected_source": "c.txt"},
    {"question": "Q4", "expected_source": "d.txt"},
]


@pytest.fixture
def spies(monkeypatch):
    """埋め込み呼び出しと hybrid_search の引数を記録する。"""
    embed_calls: list[list[str]] = []
    search_calls: list[dict] = []

    def fake_embed(texts, input_type="document", retry_waits=None):
        embed_calls.append(list(texts))
        return [[float(i)] for i in range(len(texts))]

    def fake_search(question, **kwargs):
        search_calls.append({"question": question, **kwargs})
        return [{"id": 1, "content": "本文", "source": "a.txt"}]

    monkeypatch.setattr(eval_module, "embed_texts", fake_embed)
    monkeypatch.setattr(eval_module, "hybrid_search", fake_search)
    return embed_calls, search_calls


def test_embeds_all_questions_in_one_request(spies, monkeypatch):
    embed_calls, search_calls = spies
    monkeypatch.setattr(eval_module, "resolve_retrievers", lambda names: ["vector"])

    eval_module.evaluate(gold=GOLD)

    assert len(embed_calls) == 1, "質問ごとに埋め込みAPIを呼んでいる"
    assert embed_calls[0] == ["Q1", "Q2", "Q3", "Q4"]
    assert len(search_calls) == 4


def test_passes_the_matching_vector_to_each_question(spies, monkeypatch):
    _, search_calls = spies
    monkeypatch.setattr(eval_module, "resolve_retrievers", lambda names: ["vector"])

    eval_module.evaluate(gold=GOLD)

    # i番目の質問には i番目のベクトルが渡る（ズレると評価が別物になる）
    for i, call in enumerate(search_calls):
        assert call["query_vec"] == [float(i)]


def test_skips_embedding_when_vector_search_is_not_used(spies, monkeypatch):
    """trgm/bm25 だけの構成では埋め込みAPIを一切呼ばない。"""
    embed_calls, search_calls = spies
    monkeypatch.setattr(eval_module, "resolve_retrievers", lambda names: ["trgm"])

    eval_module.evaluate(gold=GOLD, retrievers=["trgm"])

    assert embed_calls == []
    assert all(call["query_vec"] is None for call in search_calls)


def test_no_questions_means_no_embedding(spies, monkeypatch):
    embed_calls, search_calls = spies
    monkeypatch.setattr(eval_module, "resolve_retrievers", lambda names: ["vector"])

    report = eval_module.evaluate(gold=[])

    assert embed_calls == [] and search_calls == []
    assert report["n"] == 0 and report["hit_at_k"] == 0.0
