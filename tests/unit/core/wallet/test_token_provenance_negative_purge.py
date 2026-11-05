"""102 3.5: the negative cache drops a row past its TTL when a new one goes in."""
import core.wallet.token_provenance as tp


def test_a_new_negative_purges_the_expired_rows(monkeypatch, tmp_path):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", [lambda *a: None])
    monkeypatch.setattr(tp, "_probe", lambda key: None)
    clock = [1000.0]
    monkeypatch.setattr(tp.time, "monotonic", lambda: clock[0])
    path = str(tmp_path / "p.db")
    address = "0x" + "ab" * 20
    tp._NEGATIVE[(path, "base", "0xold")] = clock[0] - tp._NEGATIVE_TTL_S - 1
    tp._NEGATIVE[(path, "base", "0xfresh")] = clock[0] - 1
    assert tp.own_token("base", address, probe=True, db_path=path) is None
    assert (path, "base", "0xold") not in tp._NEGATIVE
    assert (path, "base", "0xfresh") in tp._NEGATIVE
    assert (path,) + tp._norm("base", address) in tp._NEGATIVE
    tp._reset_for_tests()
