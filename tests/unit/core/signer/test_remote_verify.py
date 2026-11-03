"""098 CO-10: remote signer verification is a machine (unreachable re-checks)."""
import pytest

from core.signer import remote


class _Client:
    def __init__(self, identity=None):
        self.identity, self.calls = identity, 0

    def call(self, op, params=None, timeout=None):
        self.calls += 1
        if self.identity is None:
            raise ConnectionError("down")
        return self.identity


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    remote._reset_for_tests()
    now = [100.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    yield now
    remote._reset_for_tests()


def test_table_is_well_formed():
    assert remote.VERIFY.validate() == []


def test_unreachable_then_reachable_verifies(_clock):
    c = _Client()
    remote._check_identity(c, {"evm": {"base": "0xabc"}})
    assert not remote.remote_verified() and remote._state == "unreachable"
    c.identity = {"evm": {"base": "0xabc"}}
    assert not remote.remote_verified() and c.calls == 1  # at most once a minute
    _clock[0] += remote.REVERIFY_SEC
    assert remote.remote_verified() and remote._state == "verified" and c.calls == 2


def test_mismatch_stays_refused(_clock, monkeypatch):
    monkeypatch.setattr(remote, "_record_matches", lambda ident, rec: "evm: 0xdef vs 0xabc")
    c = _Client({"evm": {"base": "0xdef"}})
    remote._check_identity(c, {"evm": {"base": "0xabc"}})
    assert remote._state == "mismatch" and not remote.remote_verified()
    _clock[0] += 10 * remote.REVERIFY_SEC
    assert not remote.remote_verified() and c.calls == 1
