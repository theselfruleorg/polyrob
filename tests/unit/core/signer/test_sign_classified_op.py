"""090 P0 — ``sign.classified``: the signer signs a class-A text it classified ITSELF.

The agent's class is not on the wire. A blind or unbound text refuses here even
when a compromised agent asks; the pause does not stop a login (D3); every
request is a decision row; the remote and shadow signers route through the op.
"""
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from core.signer import protocol
from core.signer.remote import RemoteEvmSigner
from core.wallet.agent_wallet import WalletSigningUnavailable
from core.wallet.sign_policy import SignRefused, SignRequest, sign_classified
from tests.unit.core.signer.conftest import LoopbackClient
from tests.unit.core.signer.test_066_p2_signer_schemas import _ok, _refused


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _siwe(rig, domain="app.example.com"):
    now = rig.service._now()
    address = rig.service.wallet.operational_signer().address
    return (f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\n"
            f"URI: https://{domain}/login\nVersion: 1\nChain ID: 8453\nNonce: abcd1234XY\n"
            f"Issued At: {_iso(now)}\nExpiration Time: {_iso(now + 600)}")


def _body(text, origin="app.example.com", fetch_bound=False, **extra):
    return dict({"scheme": "eip191", "payload": text, "bound_origin": origin,
                 "fetch_bound": fetch_bound}, **extra)


def _recover(text, sig):
    return Account.recover_message(encode_defunct(text.encode()), signature=sig)


def _decisions(rig):
    return [r for r in rig.service.store.recent_decisions() if r["op"] == "sign.classified"]


def test_the_signer_signs_a_bound_siwe_message(rig):
    text = _siwe(rig)
    out = _ok(rig.call("sign.classified", _body(text)))
    assert out["cls"] == "A"
    assert _recover(text, out["signature"]) == out["address"]
    assert out["address"] == rig.service.wallet.operational_signer().address
    row = _decisions(rig)[0]
    assert row["allowed"] == 1 and out["signature"] not in str(row)


def test_a_challenge_that_names_its_site_signs(rig):
    out = _ok(rig.call("sign.classified", _body("app.example.com enrollment. Nonce 9f8e7d6c")))
    assert out["cls"] == "A"


def test_the_signer_never_believes_the_wires_fetch_bound(rig):
    """A compromised agent says fetch_bound=true; the signer cannot prove the fetch."""
    resp = _refused(rig.call("sign.classified",
                             _body("Enroll this wallet. Nonce 9f8e7d6c", fetch_bound=True)),
                    protocol.GUARD_REFUSED)
    assert resp["cls"] == "C"


def test_the_same_text_signs_again_with_the_same_signature(rig):
    """A fixed login text (no nonce) must log in twice; a deterministic signature
    repeated reveals nothing new."""
    body = _body("app.example.com enrollment. Nonce 11112222")
    first = _ok(rig.call("sign.classified", body))
    again = _ok(rig.call("sign.classified", body))
    assert first["signature"] == again["signature"]


def test_the_signer_daily_cap(rig, monkeypatch):
    from core.signer import sign_ops
    monkeypatch.setattr(sign_ops, "SIGN_DAILY_CAP", 2)
    for i in range(2):
        _ok(rig.call("sign.classified", _body(f"app.example.com login {i:08d}")))
    resp = _refused(rig.call("sign.classified", _body("app.example.com login 99999999")),
                    protocol.GUARD_REFUSED)
    assert "cap" in resp["reason"]


def test_verdict_only_rows_do_not_count_as_signatures(rig):
    body = _body("app.example.com enrollment. Nonce 33334444")
    _ok(rig.call("sign.classified", dict(body, verdict_only=True)))
    _ok(rig.call("sign.classified", body))


def test_the_remote_side_refuses_a_signature_that_does_not_verify(rig):
    class Liar(LoopbackClient):
        def call(self, op, body=None, *, timeout=None):
            out = dict(super().call(op, body, timeout=timeout))
            out["signature"] = "0x" + "11" * 65
            return out
    address = rig.service.wallet.operational_signer().address
    signer = RemoteEvmSigner(Liar(rig.service), address, "base", verified_fn=lambda: True)
    with pytest.raises(WalletSigningUnavailable):
        sign_classified(signer, SignRequest("eip191", _siwe(rig), "app.example.com"),
                        now=rig.service._now())


@pytest.mark.parametrize("text,origin,fetch_bound", [
    ("0x" + "ab" * 32, "app.example.com", True),          # a digest, whatever the agent says
    ("prove it 12345678", "app.example.com", False),       # C: not bound
])
def test_the_signer_refuses_what_it_does_not_classify_a(rig, text, origin, fetch_bound):
    resp = _refused(rig.call("sign.classified", _body(text, origin, fetch_bound)),
                    protocol.GUARD_REFUSED)
    assert resp.get("cls") in ("C", "D")
    assert _decisions(rig)[0]["allowed"] == 0


def test_a_relayed_sign_in_for_another_site_refuses(rig):
    _refused(rig.call("sign.classified", _body(_siwe(rig, "bank.example"))),
             protocol.GUARD_REFUSED)


def test_unknown_or_missing_keys_are_unknown_shape(rig):
    _refused(rig.call("sign.classified", _body("x", cls="A")), protocol.UNKNOWN_SHAPE)
    _refused(rig.call("sign.classified", {"scheme": "eip191", "payload": "x"}),
             protocol.UNKNOWN_SHAPE)
    _refused(rig.call("sign.classified", _body(b"x".hex(), fetch_bound="yes")),
             protocol.UNKNOWN_SHAPE)


def test_the_spend_pause_does_not_stop_a_login(rig):
    assert rig.call("pause.set", {"paused": True}, uid=0)["ok"]
    _ok(rig.call("sign.classified", _body(_siwe(rig))))


def test_verdict_only_signs_nothing(rig):
    out = _ok(rig.call("sign.classified", _body(_siwe(rig), verdict_only=True)))
    assert "signature" not in out and out["cls"] == "A"


def _remote(rig, verified=True):
    address = rig.service.wallet.operational_signer().address
    client = LoopbackClient(rig.service)
    return RemoteEvmSigner(client, address, "base", verified_fn=lambda: verified), client


def test_the_agent_side_remote_signer_goes_through_the_op(rig):
    signer, client = _remote(rig)
    text = _siwe(rig)
    sig, verdict = sign_classified(signer, SignRequest("eip191", text, "app.example.com"),
                                   now=rig.service._now())
    assert _recover(text, sig) == signer.address
    assert [op for op, _ in client.calls] == ["sign.classified"]


def test_the_agent_side_refuses_before_asking(rig):
    signer, client = _remote(rig)
    with pytest.raises(SignRefused):
        sign_classified(signer, SignRequest("eip191", "0x" + "ab" * 32, "a.example", True))
    assert client.calls == []


def test_an_unverified_remote_signer_refuses(rig):
    signer, _client = _remote(rig, verified=False)
    with pytest.raises(WalletSigningUnavailable):
        sign_classified(signer, SignRequest("eip191", _siwe(rig), "app.example.com"),
                        now=rig.service._now())


def test_the_shadow_signer_compares_then_signs_locally(rig, monkeypatch, tmp_path):
    from core.signer import shadow
    logged = []
    monkeypatch.setattr(shadow, "log_comparison", lambda kind, **f: logged.append((kind, f)))
    local = rig.service.wallet.operational_signer()
    sh = shadow.ShadowEvmSigner(local, client=LoopbackClient(rig.service))
    text = _siwe(rig)
    sig, _ = sign_classified(sh, SignRequest("eip191", text, "app.example.com"),
                             now=rig.service._now())
    assert _recover(text, sig) == local.address
    assert logged and logged[0][0] == "agree" and logged[0][1]["op"] == "sign.classified"


def test_the_shadow_signer_signs_even_when_the_signer_is_down(rig, monkeypatch):
    from core.signer import shadow
    logged = []
    monkeypatch.setattr(shadow, "log_comparison", lambda kind, **f: logged.append(kind))
    local = rig.service.wallet.operational_signer()
    sh = shadow.ShadowEvmSigner(local, client=LoopbackClient(rig.service, down=True))
    sig, _ = sign_classified(sh, SignRequest("eip191", _siwe(rig), "app.example.com"),
                             now=rig.service._now())
    assert sig.startswith("0x") and logged == ["unreachable"]


def test_a_fetched_text_the_signer_cannot_prove_is_agent_only_not_a_disagreement(rig, monkeypatch):
    from core.signer import shadow
    from core.wallet.sign_policy import SignContext, classify
    logged = []
    monkeypatch.setattr(shadow, "log_comparison", lambda kind, **f: logged.append(kind))
    local = rig.service.wallet.operational_signer()
    sh = shadow.ShadowEvmSigner(local, client=LoopbackClient(rig.service))
    req = SignRequest("eip191", "Enroll this wallet. Nonce 9f8e7d6c", "app.example.com",
                      fetch_bound=True)
    verdict = classify(req, SignContext(holder=local.address, now=rig.service._now()))
    assert verdict.owner_turn_only
    sig = sh.sign_classified(req, verdict)
    assert _recover(req.payload, sig) == local.address and logged == ["agent_only"]


def test_a_real_refusal_is_still_a_disagreement(rig, monkeypatch):
    from core.signer import shadow
    from core.wallet.sign_policy import SignVerdict
    logged = []
    monkeypatch.setattr(shadow, "log_comparison", lambda kind, **f: logged.append(kind))
    local = rig.service.wallet.operational_signer()
    sh = shadow.ShadowEvmSigner(local, client=LoopbackClient(rig.service))
    # a compromised agent says A for a digest; the signer says D
    sh.sign_classified(SignRequest("eip191", "0x" + "ab" * 32, "app.example.com"),
                       SignVerdict("A", "lie", "eip191", owner_turn_only=True))
    assert logged == ["disagree"]


def test_the_shadow_wallet_compares_solana_text_signatures(monkeypatch):
    from core.signer import shadow
    logged, calls = [], []
    monkeypatch.setattr(shadow, "log_comparison", lambda kind, **f: logged.append(kind))

    class _Client:
        def call(self, op, body=None, **kw):
            calls.append((op, body))
            return {"cls": "A"}

    class _Local:
        address = "So1ana1111111111111111111111111111111111111"
        def sign_message(self, data):
            return "base58sig"
        def sign_transaction(self, tx):
            return ("signed", tx)

    sh = shadow.ShadowSolanaSigner(_Local(), client=_Client())
    req = SignRequest("solana_message", "app.example.com login 12345678", "app.example.com")
    assert sh.sign_classified(req, SimpleVerdict()) == "base58sig"
    assert calls[0][0] == "sign.classified" and calls[0][1]["verdict_only"] is True
    assert logged == ["agree"]
    assert sh.address == _Local.address and sh.sign_transaction(1) == ("signed", 1)


class SimpleVerdict:
    digest = "d"
    cls = "A"
    owner_turn_only = False
