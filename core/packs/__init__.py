"""Packs (067 P2): optional first- and third-party extensions, discovered through
the ``polyrob.packs`` entry-point group.

- :mod:`core.packs.spec` — the code half of the contract (``PackSpec``).
- :mod:`core.packs.manifest` — the data half (``pack.toml``), read without import.
- :mod:`core.packs.loader` — phase 1 (policy rows) and phase 2 (code).
- :mod:`core.packs.state` — installed / enabled / loaded / refused, with reasons.

Core never imports a pack by name; packs call into core registries.
"""
