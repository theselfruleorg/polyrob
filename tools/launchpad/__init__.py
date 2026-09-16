"""Token launchpads — launch a token, and trade one on its bonding curve (042).

A launchpad is neither a deploy nor a swap: it is a create-and-seed in ONE
transaction against a protocol we did not write, so it gets its own tool rather
than being bolted onto either.

Pieces:

- ``pons_abi``  — the PINNED Pons V2 addresses, code hashes and ABI shapes,
  every one read from chain 4663 and cross-checked four ways. Selectors are
  DERIVED from the shapes, never pasted.
- ``pons``      — the provider: live pin verification, live launch terms, exact
  bonding-curve pricing (verified against on-chain simulation, delta 0), and
  the calldata builders.
- ``execute``   — the shared guarded broadcast, so a launch, a buy and a sell
  cannot drift into three ideas of what is asserted.
- ``tool``      — the ``launchpad`` tool and its five verbs.

Gate: ``LAUNCHPAD_ENABLED`` (default OFF). Capabilities: money + high_impact +
delegate_blocked. Never in the default tool_ids.

⚠️ Everything a launchpad lists is a memecoin on a bonding curve, and the
opening snipe tax is 99%. These rails bound what can be spent and assert what is
received; none of that makes a launch or a buy a good idea.
"""

__all__ = ["register_launchpad_tool", "launchpad_enabled"]


def __getattr__(name):
    # Lazy re-export so importing the package never pulls the tool module.
    if name in __all__:
        from tools.launchpad import registration
        return getattr(registration, name)
    raise AttributeError(name)
