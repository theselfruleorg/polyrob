"""090 P0 — the off-chain signing classifier (core/wallet/sign_policy.py).

Only class A (a login / challenge text bound to its site) is signable. Everything
blind is D; a text not bound to its site is C. A property test fuzzes binary
shapes: none may ever classify as A.
"""
import random
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from core.wallet.sign_policy import (A, C, D, SignContext, SignRefused, SignRequest,
                                     classify, names_host, origin_host, same_site,
                                     sign_classified, site_of)

KEY = "0x" + "11" * 32
HOLDER = Account.from_key(KEY).address
NOW = 1_800_000_000.0
CHAINS = frozenset({8453, 1})


def _iso(delta_sec: float) -> str:
    return datetime.fromtimestamp(NOW + delta_sec, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


def siwe(domain="app.example.com", address=HOLDER, uri=None, chain=8453, nonce="abcd1234XY",
         expires=600, issued=0, extra=""):
    lines = [f"{domain} wants you to sign in with your Ethereum account:", address, "",
             "Sign in to the example app.", "",
             f"URI: {uri or 'https://' + domain + '/login'}", "Version: 1",
             f"Chain ID: {chain}", f"Nonce: {nonce}", f"Issued At: {_iso(issued)}"]
    if expires is not None:
        lines.append(f"Expiration Time: {_iso(expires)}")
    return "\n".join(lines) + extra


def _ctx(holder=HOLDER):
    return SignContext(holder=holder, now=NOW, chain_ids=CHAINS)


def _cls(text, origin="app.example.com", scheme="eip191", fetch_bound=False, holder=HOLDER):
    return classify(SignRequest(scheme, text, origin, fetch_bound), _ctx(holder))


# -- class A ---------------------------------------------------------------------

def test_a_bound_siwe_message_is_a():
    v = _cls(siwe())
    assert v.cls == A, v.reason
    assert v.decoded["kind"] == "siwe" and v.decoded["chain_id"] == 8453
    assert v.replay_key == ("app.example.com", "abcd1234XY")
    assert v.digest.startswith("0x") and len(v.digest) == 66


def test_a_fetch_bound_challenge_is_a_even_when_it_does_not_name_the_site():
    text = "Enroll wallet as merchant Rob. Nonce 6f1c2a. Expires in 5 minutes."
    v = _cls(text, origin="https://evidence.example", fetch_bound=True)
    assert v.cls == A and v.owner_turn_only


def test_a_text_that_names_its_site_is_a():
    text = "evidence.example asks you to prove you control this wallet. Nonce: 1234abcd"
    assert _cls(text, origin="evidence.example").cls == A


# -- class C ---------------------------------------------------------------------

def test_a_handed_over_text_that_does_not_name_the_site_is_c():
    v = _cls("Prove you own this wallet: 1234abcd", origin="evidence.example")
    assert v.cls == C and "does not name" in v.reason


def test_a_text_with_no_site_is_c():
    assert _cls("hello there", origin="").cls == C


def test_a_lookalike_host_does_not_count_as_naming_the_site():
    assert not names_host("sign for notexample.com please", "example.com")
    assert names_host("sign for api.example.com please", "example.com")
    assert _cls("login to notexample.com nonce 12345678", origin="example.com").cls == C


# -- class D: sign-in messages -----------------------------------------------------

@pytest.mark.parametrize("kw,needle", [
    (dict(domain="bank.example"), "bound to"),                      # relayed sign-in
    (dict(address="0x" + "22" * 20), "not this wallet"),
    (dict(uri="https://evil.example/login"), "URI"),
    (dict(chain=999999), "Chain ID"),
    (dict(nonce="short"), "nonce"),
        (dict(expires=None, issued=-3600), "no Expiration Time"),     # open-ended, stale
    (dict(expires=-10), "expired"),
    (dict(expires=3 * 86400), "more than"),
    (dict(issued=3600), "future"),
])
def test_a_bad_siwe_message_is_d(kw, needle):
    v = _cls(siwe(**kw))
    assert v.cls == D and needle.lower() in v.reason.lower(), v.reason


def test_a_siwe_message_cannot_be_signed_with_the_solana_key():
    v = _cls(siwe(), scheme="solana_message", holder="9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")
    assert v.cls == D


def test_a_malformed_sign_in_header_is_d():
    assert _cls("app.example.com wants you to sign in with your wallet please").cls == D


def test_a_repeated_field_is_d():
    assert _cls(siwe(extra="\nNonce: zzzzzzzz9")).cls == D


# -- class D: blind payloads ---------------------------------------------------------

@pytest.mark.parametrize("text,needle", [
    ("0x" + "ab" * 32, "hex"),                        # a digest
    ("ab" * 40, "hex"),
    ("Zq9" * 40, "opaque"),                            # base64/base58 of something
    ("hello\x00world app.example.com", "control"),
    ("", "empty"),
    ("app.example.com login\rURI: https://evil.example", "line break"),
    ("app.example.com login\u2028Resources: x", "line break"),
    ("x app.example.com " * 400, "bytes"),
])
def test_a_blind_text_is_d(text, needle):
    v = _cls(text, fetch_bound=True)
    assert v.cls == D and needle in v.reason, v.reason


def test_typed_data_is_d_in_p0():
    v = classify(SignRequest("eip712", {"domain": {}}, "app.example.com"), _ctx())
    assert v.cls == D and "EIP-712" in v.reason


def test_an_unknown_scheme_is_d():
    assert classify(SignRequest("eth_sign", "x", "a.example"), _ctx()).cls == D


def test_never_a_for_random_binary():
    rng = random.Random(90)
    for _ in range(400):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 300)))
        text = blob.decode("latin-1")
        for fetch_bound in (True, False):
            v = _cls(text, origin="app.example.com", fetch_bound=fetch_bound)
            if v.cls == A:
                # Only printable text may be A; random bytes almost never are.
                assert all(ch.isprintable() or ch in "\n\r\t" for ch in text), repr(text)


@pytest.mark.parametrize("raw", [
    bytes.fromhex("f86c808504a817c800825208943535353535353535353535353535353535353535"
                  "880de0b6b3a76400008025a028ef61340bd939bc2195fe537567866003e1a15d3c71ff"
                  "63e1590620aa636276a067cbe9d8997f761aecb703304b3800ccf555c9f3dc64214b297fb1966a3b6d83"),
    bytes([0x02]) + bytes([0xc5, 0x01, 0x02, 0x03, 0x04, 0x05]),
])
def test_an_encoded_transaction_is_d(raw):
    v = _cls(raw.decode("latin-1"), fetch_bound=True)
    assert v.cls == D


def test_origin_host_forms():
    assert origin_host("https://App.Example.com/x") == "app.example.com"
    assert origin_host("app.example.com:8443") == "app.example.com"
    assert origin_host("") == ""


# -- signing -----------------------------------------------------------------------

class _Local:
    def __init__(self):
        self._acct = Account.from_key(KEY)
        self.address = self._acct.address

    def sign_message(self, data):
        return self._acct.sign_message(encode_defunct(data)).signature.hex()


def test_sign_classified_signs_a_and_the_signature_recovers():
    text = siwe()
    sig, v = sign_classified(_Local(), SignRequest("eip191", text, "app.example.com"),
                             now=NOW, chain_ids=CHAINS)
    assert v.cls == A and sig.startswith("0x")
    assert Account.recover_message(encode_defunct(text.encode()), signature=sig) == HOLDER


@pytest.mark.parametrize("text,origin", [("0x" + "ab" * 32, "a.example"),
                                         ("prove it 12345678", "a.example")])
def test_sign_classified_refuses_c_and_d_without_touching_the_key(text, origin):
    class Boom(_Local):
        def sign_message(self, data):
            raise AssertionError("the key was used")
    with pytest.raises(SignRefused):
        sign_classified(Boom(), SignRequest("eip191", text, origin), now=NOW)


def test_a_remote_signer_gets_the_request_not_bytes():
    seen = []

    class Remote(_Local):
        def sign_classified(self, req, verdict):
            seen.append((req.payload, verdict.cls))
            return "0xremote"
    sig, _ = sign_classified(Remote(), SignRequest("eip191", siwe(), "app.example.com"),
                             now=NOW, chain_ids=CHAINS)
    assert sig == "0xremote" and seen == [(siwe(), A)]


def test_solana_sign_in_with_the_solana_key():
    pytest.importorskip("solders")
    from core.wallet.solana_signer import SolanaSigner
    from solders.keypair import Keypair
    signer = SolanaSigner(Keypair.from_seed(bytes(range(32))))
    text = (f"app.example.com wants you to sign in with your Solana account:\n{signer.address}\n\n"
            f"URI: https://app.example.com\nNonce: abcd1234XY\nIssued At: {_iso(0)}\n"
            f"Expiration Time: {_iso(600)}")
    sig, v = sign_classified(signer, SignRequest("solana_message", text, "app.example.com"),
                             now=NOW)
    assert v.cls == A and v.decoded["kind"] == "siws"
    from solders.signature import Signature
    from solders.pubkey import Pubkey
    assert Signature.from_string(sig).verify(Pubkey.from_string(signer.address), text.encode())


# -- review fixes (2026-10-27) ---------------------------------------------------------

def test_an_expiry_hidden_in_the_statement_is_d():
    lines = siwe(expires=None).split("\n")
    lines[3] = f"Expiration Time: {_iso(600)}"
    assert _cls("\n".join(lines)).cls == D


def test_fields_out_of_order_or_unknown_are_d():
    lines = siwe().split("\n")
    i, j = lines.index("Version: 1"), [k for k, l in enumerate(lines) if l.startswith("URI:")][0]
    lines[i], lines[j] = lines[j], lines[i]
    assert _cls("\n".join(lines)).cls == D
    assert _cls(siwe(extra="\nFoo: bar")).cls == D


def test_siwe_needs_version_1():
    assert _cls(siwe().replace("Version: 1", "Version: 2")).cls == D


def test_a_recap_or_authorizing_statement_is_c():
    recap = siwe(extra="\nResources:\n- urn:recap:eyJhdHQiOnt9fQ")
    assert _cls(recap).cls == C
    auth = siwe().replace("Sign in to the example app.", "I further authorize the stated URI")
    assert _cls(auth).cls == C


@pytest.mark.parametrize("domain", ["victim.com@attacker.com", "victim.com\\@attacker.com",
                                    "x@app.example.com"])
def test_a_userinfo_or_backslash_domain_is_d(domain):
    assert _cls(siwe(domain=domain), origin="attacker.com").cls == D


def test_a_port_is_the_same_site_and_the_same_replay_key():
    v = _cls(siwe(domain="app.example.com:443", uri="https://app.example.com/login"))
    assert v.cls == A and v.decoded["domain"] == "app.example.com"
    assert v.replay_key == ("app.example.com", "abcd1234XY")


def test_names_host_rejects_a_suffix_attack():
    assert not names_host("login for app.example.com.attacker.io", "app.example.com")


@pytest.mark.parametrize("text", [
    "Access zkSync account.\n\nOnly sign this message for a trusted client!",
    "Sign this to generate your StarkKey for app.example.com",
    "app.example.com: derive your trading key",
    "Create XMTP identity for app.example.com",
])
def test_a_key_derivation_text_is_d(text):
    assert _cls(text, fetch_bound=True).cls == D


def test_a_challenge_naming_another_site_is_c():
    v = _cls("Sign in to opensea.io. Nonce 12345678", fetch_bound=True)
    assert v.cls == C and "opensea.io" in v.reason


def test_an_unnamed_fetched_challenge_is_owner_turn_only():
    v = _cls("Enroll wallet as merchant Rob. Nonce 6f1c2a99", fetch_bound=True)
    assert v.cls == A and v.owner_turn_only
    v2 = classify(SignRequest("eip191", "Enroll wallet. Nonce 6f1c2a99", "app.example.com", True),
                  SignContext(holder=HOLDER, now=NOW, trust_fetch_bound=False))
    assert v2.cls == C


def test_the_eip4361_spec_example_with_resources_is_a():
    addr = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    text = (f"service.invalid wants you to sign in with your Ethereum account:\n{addr}\n\n"
            "I accept the ServiceOrg Terms of Service: https://service.invalid/tos\n\n"
            "URI: https://service.invalid/login\nVersion: 1\nChain ID: 1\nNonce: 32891756\n"
            f"Issued At: {_iso(0)}\nExpiration Time: {_iso(600)}\nResources:\n"
            "- ipfs://bafybeiemxf5abjwjbikoz4mc3a3dla6ual3jsgpdr4cjr3oz3evfyavhwq/\n"
            "- https://example.com/my-web2-claim.json")
    v = classify(SignRequest("eip191", text, "service.invalid"),
                 SignContext(holder=addr, now=NOW, chain_ids=frozenset({1})))
    assert v.cls == A, v.reason


def test_phantoms_standard_sign_in_statement_is_a():
    sol = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    text = (f"example.com wants you to sign in with your Solana account:\n{sol}\n\n"
            "Clicking Sign or Approve only means you have proved this wallet is owned by you.\n\n"
            "URI: https://example.com\nVersion: 1\nChain ID: mainnet\nNonce: oBbLoEldZs\n"
            f"Issued At: {_iso(0)}\nExpiration Time: {_iso(600)}")
    v = classify(SignRequest("solana_message", text, "example.com"),
                 SignContext(holder=sol, now=NOW))
    assert v.cls == A, v.reason


MOB = ("402Mob merchant enrollment\nOrigin: https://evidence.example\nWallet: {w}\n"
       "Merchant name: Rob\nChallenge: f7e654fd-11a1-4e31-9fcf-f8ae1a3d57f5\n"
       "Issued: 2026-10-27T16:52:40.260Z\nExpires: 2026-10-27T16:57:40.260Z\n"
       "Transaction proof address: 0xd73f6b2aef6c6e948d9aae6073cb8546e748575c\n"
       "This creates or replaces a merchant API key. It does not authorize a payment.")


def test_the_real_402mob_challenge_shape_is_a():
    """Prod 2026-10-27: the live 402Mob text (host neutralized) — it names its origin
    and says it does NOT authorize a payment."""
    v = _cls(MOB.format(w=HOLDER), origin="evidence.example", fetch_bound=True)
    assert v.cls == A and not v.owner_turn_only, v.reason


@pytest.mark.parametrize("text", [
    "evidence.example login. It does not authorize a payment, but it authorizes withdrawals.",
    "evidence.example: this never transfers funds. Sign to grant spending rights.",
    "evidence.example login. Please do not hesitate to transfer my tokens.",
])
def test_a_negation_does_not_hide_a_real_grant(text):
    assert _cls(text, origin="evidence.example", fetch_bound=True).cls == C


# -- gaps closed after the 2026-10-28 402Mob review ---------------------------------

def test_a_fresh_siwe_message_with_no_expiry_is_a():
    """The siwe library sends no Expiration Time by default; a fresh one signs."""
    v = _cls(siwe(expires=None, issued=-30))
    assert v.cls == A and v.decoded.get("open_ended"), v.reason


def test_a_siwe_message_with_neither_expiry_nor_issued_at_is_d():
    text = "\n".join(l for l in siwe(expires=None).split("\n") if not l.startswith("Issued At"))
    v = _cls(text)
    assert v.cls == D and "neither" in v.reason


@pytest.mark.parametrize("text", [
    "evidence.example login. This signature does not authorize any transfer of funds.",
    "evidence.example login. It will not trigger a transaction or spend any funds.",
    "evidence.example login. This does not give anyone permission to transfer your assets.",
    "evidence.example login. No funds will be transferred.",
    "evidence.example login without granting any allowance. Nonce 1234abcd",
])
def test_a_common_disclaimer_is_not_a_grant(text):
    v = _cls(text, origin="evidence.example", fetch_bound=True)
    assert v.cls == A, v.reason


@pytest.mark.parametrize("text", [
    "evidence.example login. This does not authorize a transfer, and it grants withdrawals.",
    "evidence.example login. No gas will be spent; sign to delegate your votes.",
])
def test_a_grant_after_the_disclaimer_still_counts(text):
    assert _cls(text, origin="evidence.example", fetch_bound=True).cls == C


def test_a_warning_about_the_private_key_is_not_a_key_derivation():
    text = ("Welcome to evidence.example. We will never ask for your private key or seed "
            "phrase. Nonce 1234abcd")
    assert _cls(text, origin="evidence.example").cls == A


def test_a_real_key_derivation_after_a_warning_is_still_d():
    text = ("evidence.example: we never ask for your seed phrase. Sign to derive your "
            "trading key. Nonce 1234abcd")
    assert _cls(text, origin="evidence.example").cls == D


@pytest.mark.parametrize("host,site", [
    ("api.example.com", "example.com"), ("example.com", "example.com"),
    ("a.b.example.co.uk", "example.co.uk"), ("me.github.io", "me.github.io"),
    ("x.me.github.io", "me.github.io"), ("10.0.0.1", "10.0.0.1"),
])
def test_site_of(host, site):
    assert site_of(host) == site


def test_sibling_subdomains_are_one_site_but_shared_hosting_is_not():
    assert same_site("api.example.com", "app.example.com")
    assert not same_site("example.com.evil.io", "example.com")
    assert not same_site("alice.github.io", "bob.github.io")
    assert not same_site("notexample.com", "example.com")


def test_a_challenge_fetched_from_api_that_names_the_app_host_is_a():
    text = "Sign in to app.example.com. Nonce 1234abcd"
    v = _cls(text, origin="api.example.com", fetch_bound=True)
    assert v.cls == A and not v.owner_turn_only, v.reason


def test_a_siwe_message_for_a_sibling_host_is_a():
    assert _cls(siwe(domain="app.example.com"), origin="api.example.com").cls == A
