"""Test helper: pin collection profiles in the owner's collection registry (069 v4 A2).

The registry reads ``/etc/polyrob/agent_nft_collections.json``; tests replace
``collection_registry.profiles`` with parsed profiles instead of writing that file.
"""
import hashlib

from core.wallet import collection_registry, erc6551

SUPPLY = 6551
#: A stand-in runtime for a pinned collection: the guard re-checks the profile's
#: ``runtime_sha256`` against ``eth_getCode`` (069 v4 A3), so a fake RPC answers this.
CODE = "0x6080604052" + "5b" * 27
RUNTIME_SHA256 = hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()


def code_rpc(inner=None, *, code=CODE, block=0x100):
    """A fake RPC for the guard's pinned-collection reads: ``eth_getCode`` of any address the
    *inner* fake does not answer = :data:`CODE`, ``eth_blockNumber`` = *block*, ``eth_getLogs``
    = no logs (an account with no open approval). *inner* (optional) is asked first."""
    def rpc(method, params):
        if inner is not None:
            try:
                got = inner(method, params)
            except NotImplementedError:
                got = NotImplemented
            if got is not NotImplemented:
                return got
        if method == "eth_getCode":
            return code
        if method == "eth_blockNumber":
            return hex(block)
        if method == "eth_getLogs":
            return []
        raise AssertionError(f"unexpected rpc {method} {params}")
    return rpc


def profile(address, *, chain_id=4663, max_supply=SUPPLY, capabilities=("mint", "reveal"),
            journal_prefix=None, runtime_sha256=RUNTIME_SHA256, deploy_block=1):
    raw = {
        "spec": collection_registry.SPEC_V1,
        "capabilities": list(capabilities),
        "chain_id": chain_id,
        "address": address,
        "runtime_sha256": runtime_sha256,
        "deploy_block": deploy_block,
        "max_supply": max_supply,
        "accounts": [{"registry": erc6551.REGISTRY, "implementation": erc6551.ACCOUNT_V3_IMPL,
                      "salt": 0}],
    }
    if journal_prefix:
        raw["journal_prefix"] = journal_prefix
    return raw


def pin(monkeypatch, *profiles):
    """Pin *profiles* (dicts from :func:`profile`, or bare addresses on 4663). No argument = none."""
    raws = [p if isinstance(p, dict) else profile(p) for p in profiles]
    parsed = collection_registry.parse({"profiles": raws})
    monkeypatch.setattr(collection_registry, "profiles", lambda: parsed)
    return parsed


def live_profile(address, rpc, **kw):
    """:func:`profile` with the runtime_sha256 of the code *rpc* serves at *address* (fork tests)."""
    code = rpc("eth_getCode", [address, "latest"])
    return profile(address, runtime_sha256=hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(), **kw)
