"""Tests for the lazy embedder proxy (keeps the heavy torch/model load off the boot path)."""

from core.embedding import LazyEmbedder


class _FakeModel:
    def __init__(self):
        self.calls = 0

    def encode(self, text):
        self.calls += 1
        return [0.0, 1.0]


def test_does_not_build_until_used():
    built = []

    def builder(name):
        built.append(name)
        return _FakeModel()

    emb = LazyEmbedder("some-model", builder=builder)
    assert emb.loaded is False
    assert built == []  # constructing the proxy must NOT build the model


def test_builds_on_first_use_then_caches():
    built = []

    def builder(name):
        built.append(name)
        return _FakeModel()

    emb = LazyEmbedder("some-model", builder=builder)
    assert emb.encode("hi") == [0.0, 1.0]   # first use triggers the build
    assert emb.loaded is True
    assert built == ["some-model"]
    emb.encode("again")                     # second use reuses the same model
    assert built == ["some-model"]          # builder not called again
