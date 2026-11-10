"""112 W4: the one draw and the hashes the agent can compute with no code exec."""
import hashlib

import pytest

from core.fair_draw import ComputeError, fair_draw, hash_text

SEED = "0x" + "ab" * 32
IDS = ["1", "2", "3", "10", "200"]


def _oracle(seed, ids, n):
    """The draw rule as a host script implemented it before 112 (the parity oracle)."""
    valid = sorted(ids)
    return sorted(valid, key=lambda uid: hashlib.sha256((seed + uid).encode()).hexdigest())[:n]


def test_draw_matches_the_collector_and_is_pinned():
    assert fair_draw(SEED, IDS, 5) == _oracle(SEED, IDS, 5) == ["10", "200", "1", "3", "2"]
    assert fair_draw(SEED, list(reversed(IDS)), 2) == ["10", "200"]


def test_draw_edges():
    assert fair_draw(SEED, IDS, 0) == [] and fair_draw(SEED, IDS, 99) == _oracle(SEED, IDS, 99)
    with pytest.raises(ComputeError):
        fair_draw(SEED, ["1", "1"], 1)
    with pytest.raises(ComputeError):
        fair_draw("", IDS, 1)


def test_seed_is_text_verbatim():
    assert fair_draw(SEED, IDS, 5) != fair_draw(SEED[2:], IDS, 5)


def test_hash_vectors():
    assert hash_text("sha256", "abc") == hashlib.sha256(b"abc").hexdigest()
    assert hash_text("sha3_256", "") == hashlib.sha3_256(b"").hexdigest()
    assert hash_text("sha256", "0x616263", "hex") == hash_text("sha256", "abc")
    with pytest.raises(ComputeError):
        hash_text("sha256", "zz", "hex")
    with pytest.raises(ComputeError):
        hash_text("md5", "abc")


def test_keccak_vector_or_plain_refusal():
    try:
        import eth_utils  # noqa: F401
    except Exception:
        with pytest.raises(ComputeError, match="crypto extra"):
            hash_text("keccak256", "")
        return
    assert hash_text("keccak256", "") == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")


def test_keccak_missing_never_falls_back_to_sha3(monkeypatch):
    import builtins
    real = builtins.__import__

    def _no_eth(name, *a, **k):
        if name.startswith("eth_utils"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _no_eth)
    with pytest.raises(ComputeError, match="crypto extra"):
        hash_text("keccak256", "")
