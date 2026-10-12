"""The ``polyrob`` namespace (W11).

Today it holds ONE module, :mod:`polyrob.sdk` — the stable host surface a pack
imports. The rest of the code still lives in the flat top-level packages
(``core``, ``tools``, …); moving them under this namespace is the 2.0 plan
(structural review D6). Keep this file free of imports: ``import polyrob``
must have no side effect.
"""
