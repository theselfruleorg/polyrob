"""The Alchemy indexer must never log its API key.

`tools/defi/providers/alchemy_index.py` composes `f"{base_url}/{key}"` — the
credential lives in the URL PATH, where every name-keyed secret scrubber misses
it — and passed it to `httpx.post` inside a broad `except Exception:
logger.debug(..., exc_info=True)`. The identical pattern already leaked a real
key to the production journal from the x402 settlement scan, which was fixed
there with `_redact_rpc`; this second site was missed.

Pins BOTH halves: the endpoint is redacted, and the key is scrubbed out of the
exception text — so a future `raise_for_status()` (whose `HTTPStatusError`
renders "... for url 'https://host/v2/<key>'") cannot re-open the hole.
"""
import logging

import pytest

from core.security.redaction import redact_url, scrub_secret

KEY = "SUPERSECRETALCHEMYKEY123"


def test_redact_url_keeps_the_host_and_drops_the_credential_path():
    out = redact_url(f"https://base-mainnet.g.alchemy.com/v2/{KEY}")
    assert KEY not in out
    assert out == "https://base-mainnet.g.alchemy.com/…"


def test_redact_url_leaves_a_keyless_endpoint_readable():
    assert redact_url("https://mainnet.base.org") == "https://mainnet.base.org"


def test_scrub_secret_removes_a_key_embedded_in_an_exception_message():
    text = (f"Client error '401 Unauthorized' for url "
            f"'https://base-mainnet.g.alchemy.com/v2/{KEY}'")
    assert KEY not in scrub_secret(text, KEY)


def test_the_settlement_scan_and_the_indexer_share_one_helper():
    """One implementation, not two — the second site was missed precisely
    because the first one's fix was private to its module."""
    from modules.x402.settlement_watcher import _redact_rpc
    assert _redact_rpc is redact_url


def test_a_failed_fetch_never_logs_the_key(monkeypatch, caplog):
    """The real leak path: an httpx failure whose message carries the full URL,
    logged from the broad except in `fetch_balances`."""
    import httpx

    from tools.defi.providers import alchemy_index

    monkeypatch.setenv("ALCHEMY_API_KEY", KEY)

    def _boom(url, **kwargs):
        raise httpx.HTTPStatusError(
            f"Client error '401 Unauthorized' for url '{url}'",
            request=None, response=None)

    monkeypatch.setattr(httpx, "post", _boom)
    with caplog.at_level(logging.DEBUG, logger=alchemy_index.__name__):
        assert alchemy_index.fetch_balances("0x" + "1" * 40, chain="base") is None

    # caplog.text includes the formatted traceback, which is where the leak
    # actually lived: `exc_info=True` renders the HTTPStatusError message, and
    # that message carries the whole keyed URL.
    blob = caplog.text
    assert caplog.records, "the failure must still be diagnosable"
    assert KEY not in blob
    assert "alchemy.com" in blob, "the endpoint host is still useful to log"
