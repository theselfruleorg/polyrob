"""M11 (2026-09-23): SIWE verify binds domain, URI, nonce, chain and time.

Before: verify ignored the message's domain/URI and its own `Nonce:` line, and a
FUTURE `Issued At` stayed fresh forever; a free-text `timestamp:` fallback was
also accepted.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from modules.auth.siwe_auth import SIWEAuthenticator, parse_siwe_message
from modules.database.auth_tables import AuthTables
from modules.database.connection import DatabaseConnection

DOMAIN = "app.example.com"


async def _auth(tmp_path):
    db = DatabaseConnection(tmp_path / "auth.db")
    await db.connect()
    await AuthTables(db).create_tables()
    return SIWEAuthenticator(db)


def _ts(delta_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _msg(address, nonce, *, domain=DOMAIN, uri=None, chain=1, issued=None,
         expires=None, statement="Sign in"):
    lines = [f"{domain} wants you to sign in with your Ethereum account:", address, "",
             statement, "",
             f"URI: {uri or 'https://' + domain}", "Version: 1", f"Chain ID: {chain}",
             f"Nonce: {nonce}", f"Issued At: {issued or _ts()}"]
    if expires:
        lines.append(f"Expiration Time: {expires}")
    return "\n".join(lines)


async def _verify(auth, acct, message, nonce, **kw):
    sig = Account.sign_message(encode_defunct(text=message), private_key=acct.key)
    return await auth.verify_signature(acct.address, message, sig.signature.hex(),
                                       nonce, expected_domain=DOMAIN, **kw)


@pytest.mark.asyncio
async def test_well_formed_message_verifies(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    assert await _verify(auth, acct, _msg(acct.address, nonce), nonce) is True


@pytest.mark.asyncio
async def test_wrong_domain_is_refused(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    m = _msg(acct.address, nonce, domain="evil.example")
    assert await _verify(auth, acct, m, nonce) is False


@pytest.mark.asyncio
async def test_wrong_uri_host_is_refused(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    m = _msg(acct.address, nonce, uri="https://evil.example/login")
    assert await _verify(auth, acct, m, nonce) is False


@pytest.mark.asyncio
async def test_domain_defaults_to_webview_domain(tmp_path, monkeypatch):
    auth, acct = await _auth(tmp_path), Account.create()
    monkeypatch.setenv("WEBVIEW_DOMAIN", DOMAIN)
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    m = _msg(acct.address, nonce)
    sig = Account.sign_message(encode_defunct(text=m), private_key=acct.key)
    assert await auth.verify_signature(acct.address, m, sig.signature.hex(), nonce) is True
    monkeypatch.setenv("WEBVIEW_DOMAIN", "other.example")
    nonce2 = await auth.generate_nonce(acct.address, chain_id=1)
    m2 = _msg(acct.address, nonce2)
    sig2 = Account.sign_message(encode_defunct(text=m2), private_key=acct.key)
    assert await auth.verify_signature(acct.address, m2, sig2.signature.hex(), nonce2) is False


@pytest.mark.asyncio
async def test_in_message_nonce_must_equal_submitted_nonce(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    live = await auth.generate_nonce(acct.address, chain_id=1)
    # An old signed message carrying a DIFFERENT nonce, submitted with a live one.
    m = _msg(acct.address, "stale-nonce-from-an-old-signature")
    assert await _verify(auth, acct, m, live) is False


@pytest.mark.asyncio
async def test_missing_nonce_is_refused(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    assert await _verify(auth, acct, _msg(acct.address, nonce), None) is False


@pytest.mark.asyncio
async def test_chain_id_must_match_the_issued_chain(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    assert await _verify(auth, acct, _msg(acct.address, nonce, chain=137), nonce) is False
    # The refused attempt did not burn the nonce: the right chain still works.
    assert await _verify(auth, acct, _msg(acct.address, nonce, chain=1), nonce) is True


@pytest.mark.asyncio
async def test_future_issued_at_beyond_skew_is_refused(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    m = _msg(acct.address, nonce, issued=_ts(3600))
    assert await _verify(auth, acct, m, nonce) is False


@pytest.mark.asyncio
async def test_small_future_skew_is_tolerated(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    m = _msg(acct.address, nonce, issued=_ts(20))
    assert await _verify(auth, acct, m, nonce) is True


@pytest.mark.asyncio
async def test_old_or_expired_message_is_refused(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    assert await _verify(auth, acct, _msg(acct.address, nonce, issued=_ts(-600)), nonce) is False
    m = _msg(acct.address, nonce, expires=_ts(-5))
    assert await _verify(auth, acct, m, nonce) is False


@pytest.mark.asyncio
async def test_nonce_consumed_once_under_concurrency(tmp_path):
    auth, acct = await _auth(tmp_path), Account.create()
    nonce = await auth.generate_nonce(acct.address, chain_id=1)
    m = _msg(acct.address, nonce)
    results = await asyncio.gather(*[_verify(auth, acct, m, nonce) for _ in range(4)])
    assert results.count(True) == 1


def test_timestamp_fallback_is_gone(tmp_path):
    import time
    auth = SIWEAuthenticator(db=None)
    assert auth._check_message_freshness(f"hello timestamp: {int(time.time())}") is False


def test_statement_nonce_line_is_not_the_nonce():
    m = _msg("0xabc", "real", statement="Nonce: fake")
    assert parse_siwe_message(m)["Nonce"] == "real"


def test_duplicate_field_is_rejected():
    m = _msg("0xabc", "real") + "\nNonce: second"
    assert parse_siwe_message(m) is None
