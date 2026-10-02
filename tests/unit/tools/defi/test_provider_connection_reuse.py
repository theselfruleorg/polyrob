"""Providers share ONE pooled HTTP client — a new connection costs ~6 s on this box.

⚠️ MEASURED on prod 2026-09-24, and it is a host defect the code has to live with.
Outbound IPv6 does not work, but the box advertises a global IPv6 address and an
IPv6 default route, so resolvers hand out AAAA records and `httpx`/`httpcore`
connect to the first resolved address rather than racing families the way curl
does. Every NEW connection therefore burns the IPv6 timeout and falls back:

    IPv4 connect 104.18.38.143:      0.00s OK
    IPv6 connect 2606:4700:4409::…:  3.07s FAILED

    getaddrinfo:               0.00s
    httpx.get (fresh client):  6.24s
    httpx.get (fresh client):  6.22s
    reused client, call 1:     6.52s
    reused client, call 2:     0.01s   <--
    reused client, call 3:     0.01s   <--
    trust_env=False:           6.22s   (not a proxy)

It is not one API: dexscreener 3.05s · gopluslabs 3.06s · geckoterminal 3.07s ·
api.telegram.org 3.10s · openrouter.ai 3.07s all fail IPv6 identically.

`dexscreener._get` called `httpx.get(...)`, which builds and discards a client
per call, so it paid that handshake EVERY time: measured 6.33 s mean per price
lookup with only 0.26 s spread between a 30-pool and a 1-pool token — a fixed
cost, not network weather. Across this wallet's 84 holdings that is **532 s**,
which is the 556–563 s that killed four consecutive SAFETY monitor runs.

Pooling turns it into one handshake plus ~0.01 s per call — about **7 s**.

The pool lives in `_http`, the seam that already exists for "the helpers every
provider carried by hand", because this repo's rule is to extend the one thing
rather than add a second. `httpx.Client` is thread-safe, which now matters: the
`defi_data` verbs run on worker threads via `asyncio.to_thread`.
"""
import threading

import httpx
import pytest

from tools.defi.providers import _http


# --- the pool itself ------------------------------------------------------- #

def test_the_client_is_reused_not_rebuilt():
    """The whole point. A fresh client per call pays the handshake per call."""
    assert _http.client() is _http.client()


def test_it_is_an_httpx_client_with_a_bounded_timeout():
    c = _http.client()
    assert isinstance(c, httpx.Client)
    assert c.timeout.connect is not None, "an unbounded connect would hang a rail"


def test_concurrent_callers_get_the_same_instance():
    """These verbs run on worker threads now (asyncio.to_thread), so two threads
    can race the lazy construction. Two clients would mean two connection pools
    and the handshake paid twice."""
    seen, barrier = [], threading.Barrier(8)

    def _grab():
        barrier.wait()
        seen.append(_http.client())

    threads = [threading.Thread(target=_grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(c) for c in seen}) == 1, "the lazy init is not thread-safe"


def test_the_pool_carries_the_project_user_agent():
    assert _http.USER_AGENT in str(_http.client().headers.get("user-agent", ""))


# --- the providers actually go through it ----------------------------------- #

# --- why the providers are NOT driven at runtime here ----------------------- #
#
# This directory's `conftest.py` deliberately blocks `dexscreener._get` and
# `geckoterminal._get` outright — "unit tests must not reach the network", added
# because a test that stubbed only `search_fn` once fell through to a live
# GeckoTerminal call and got 15 real USDC rows for a case written as "the index
# found nothing". Defeating that guard to prove a plumbing change would weaken a
# guard that exists for a better reason than this test needs, so the contract is
# pinned at the SOURCE instead, by the ratchet below. Stated plainly rather than
# implied by omission: the pooled call path itself is exercised in production,
# where it was measured (45.6 s portfolio read, and 0.01 s per reused call).


# --- the ratchet ----------------------------------------------------------- #

def test_no_provider_builds_its_own_client():
    """Extend the one seam; never add a second. A module-level `httpx.get(...)`
    or `httpx.post(...)` in a provider is a per-call handshake by construction.

    ⚠️ Scoped past comments and past `_http` itself. My first version matched the
    measurement table inside `_http`'s own explanatory comment — the third time
    tonight a test of mine caught its own prose (the ticker-collision test scanned
    for "real", the budget test forbade a phrase inside its own prohibition). The
    lesson is the same each time: a test that reads source must look at CODE.
    """
    import pathlib
    import re

    root = pathlib.Path(_http.__file__).parent
    seam = pathlib.Path(_http.__file__).name
    offenders = []
    pat = re.compile(r"\bhttpx\.(get|post|put|delete|request)\s*\(")
    for path in sorted(root.rglob("*.py")):
        if path.name == seam:
            continue                      # the seam is allowed to name httpx
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pat.search(code) and "noqa: pooled" not in line:
                offenders.append(f"{path.relative_to(root)}:{i}")
    assert offenders == [], (
        "these call httpx directly instead of the pooled client in _http, so they "
        f"pay ~6 s of connection setup on every call: {offenders}")


def test_the_seam_itself_builds_exactly_one_client():
    """And the seam builds a client in ONE place, so there is one pool."""
    import pathlib
    import re

    src = pathlib.Path(_http.__file__).read_text()
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert len(re.findall(r"httpx\.Client\s*\(", code)) == 1
