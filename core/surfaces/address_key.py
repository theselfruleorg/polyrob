"""How an external address is KEYED, in one place.

Two rules live here on purpose, and the difference is a known debt:

* :func:`norm_addr` — case/space-insensitive. Correct for email, harmless for
  a phone number or a numeric chat id. The correspondent registry and the
  dead-target store key on this.
* :func:`canonical_addr` — the same, plus a leading ``@`` and a ``t.me/``
  wrapper folded away. The conversation store keys on this since the
  2026-09-15 prod review (C8): one Telegram channel was stored as THREE
  conversations, so "have we spoken" and the owner-resend cooldown each saw a
  third of the history.

Every store keys on :func:`canonical_addr` now: the correspondent registry
and the dead-target store adopted it on 2026-09-17 with the same once-per-file
legacy-row collapse the conversation store shipped (``CorrespondentRegistry.
_merge_legacy_spellings``), so a ``@handle`` correspondent and its
conversation resolve to ONE key. :func:`norm_addr` remains for a caller that
must key on the raw lowercase spelling (none in the tree today).
"""
from __future__ import annotations

import re

_LINK_PREFIX_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/",
                             re.IGNORECASE)


def norm_addr(address: str) -> str:
    return (address or "").strip().lower()


def canonical_addr(address: str) -> str:
    a = (address or "").strip()
    a = _LINK_PREFIX_RE.sub("", a)
    if a.startswith("@"):
        a = a[1:]
    return a.lower()
