"""Every pinned EVM address must carry a checksum that VALIDATES, or none at all.

Live incident, 2026-09-13. `chains.py` pinned Robinhood Chain's WETH as
`0x0bD7d308F8e1639FAb988DF18a8011f41EacaD73` -- mixed case, and a mixed-case
address is read as CHECKSUMMED. That one was wrong, so `onchain.token_balances`
refused it by design ("a failed checksum usually means a typo or a swapped
address"). `bridge_guard.token_balance_raw` caught the ValueError and returned
None, and the bridge -- correctly -- refuses to start a transfer whose arrival it
cannot measure.

The owner saw "guard: could not read 0xcAda... on robinhood", three times, on a
chain his agent was reading fine the whole while (portfolio reads NATIVE, which
never touches this constant). A dead constant looked exactly like a flaky RPC:
the agent retried, blamed the provider, and told him to run the command himself,
where it would have failed identically.

An address is either lower/upper case (carries NO checksum, accepted as-is) or
mixed case (carries one, and it must be right). There is no third option, and a
wrong one fails far from where it was written.
"""
import re

from eth_utils import to_checksum_address

_ADDR = re.compile(r'(\w+)\s*=\s*"(0x[0-9a-fA-F]{40})"')


def _pinned_addresses():
    """(field, address, line) for every hardcoded EVM address in chains.py."""
    src = open("core/wallet/chains.py").read()
    for m in _ADDR.finditer(src):
        yield m.group(1), m.group(2), src[:m.start()].count("\n") + 1


def test_there_are_pinned_addresses_to_check():
    """Guard the guard: a refactor that moves these must not silently pass."""
    assert list(_pinned_addresses()), "no pinned addresses found — did the format change?"


def test_every_mixed_case_pinned_address_has_a_VALID_checksum():
    broken = []
    for field, addr, line in _pinned_addresses():
        if addr == addr.lower() or addr == addr.upper():
            continue  # carries no checksum; accepted as-is by design
        if addr != to_checksum_address(addr.lower()):
            broken.append(
                f"chains.py:{line} {field}\n"
                f"       stored : {addr}\n"
                f"       correct: {to_checksum_address(addr.lower())}")
    assert not broken, (
        "Pinned address(es) carry a BROKEN EIP-55 checksum. Every read through "
        "`onchain.token_balances` will raise, and each caller turns that into a "
        "silent None that reads as 'unreadable RPC' far from here:\n  "
        + "\n  ".join(broken)
        + "\n\n⚠️ Do NOT just re-case it. Verify the address is the contract you "
          "meant (eth_getCode + symbol + decimals) FIRST — the checksum exists to "
          "catch a swapped address, and correcting the case on a WRONG address "
          "converts a loud refusal into a live misroute.")


def test_every_wrapped_native_is_readable_as_a_checksummed_address():
    """The 2026-09-13 regression, stated as a property of the REGISTRY.

    Deliberately does NOT re-declare the address: `chains.py` is the SSOT, and a
    test that pins a second copy of a constant only guarantees the two copies
    agree with each other. What must hold is that every `wrapped_native` survives
    the checksum path its readers put it through -- which is the step that
    actually failed.
    """
    from core.wallet import chains
    for name in chains.names():
        row = chains.get(name)
        weth = getattr(row, "wrapped_native", None)
        if not weth or not str(weth).startswith("0x"):
            # ⚠️ Solana's wrapped native is a base58 MINT
            # (So1111…1112), not an EVM address. base58 is case-SENSITIVE and
            # carries no EIP-55 checksum, so case-folding it would corrupt a
            # real address -- the exact hazard `relay_svm` warns about. Only
            # 0x-hex is checksummed here.
            continue
        assert weth == to_checksum_address(weth.lower()), (
            f"{name}.wrapped_native {weth} fails EIP-55; every ERC-20 balance "
            f"read on {name} will raise and be swallowed as 'unreadable'")
