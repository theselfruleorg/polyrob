"""The account journal signing template (050 §6.6; 069 v4 §5 rule 6).

A journal entry is signed by the NFT's OWNER key (the treasury that owns the NFT) as a
plain EIP-191 ``personal_sign`` over a human-readable template — never an opaque digest.
069 v4 removed the account binding and the signer lock-down that used to restrict the
key to this template; what stays is the template itself, so an entry is recognisable and
verifiable by anyone.

⚠️ The owner of an NFT is an ERC-1271 signer of its account, and AccountV3 puts no account
in the digest: whatever this key signs can be presented as the account's signature. Sign
only what you mean to stand behind as the account.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

#: The journal prefix when a collection profile sets none (069 §10.4).
DEFAULT_JOURNAL_PREFIX = "agent"
#: What a profile's ``journal_prefix`` may look like: printable, one line, no ``=``.
JOURNAL_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}")

#: The journal signing template (050 §6.6) — human-readable, never an opaque digest.
JOURNAL_TEMPLATE = ("{prefix} account journal\naccount={account}\nchain={chain}\nseq={seq}\n"
                    "kind={kind}\ntext_sha256={text_sha256}\nprev={prev}")
JOURNAL_KINDS = ("thesis", "entry", "exit", "tend", "note", "handover")
_TEMPLATE_BODY = (
    r" account journal\n"
    r"account=0x[0-9a-f]{40}\n"
    r"chain=[1-9][0-9]{0,9}\n"
    r"seq=(0|[1-9][0-9]{0,15})\n"
    r"kind=(" + "|".join(JOURNAL_KINDS) + r")\n"
    r"text_sha256=[0-9a-f]{64}\n"
    r"prev=([0-9a-f]{64}|genesis)")


def allowed_journal_prefixes() -> Tuple[str, ...]:
    """The prefixes a journal entry may carry: the default plus every pinned collection
    profile's ``journal_prefix`` (the owner-written registry). Nothing looser. An
    unreadable registry contributes nothing (the default alone — the narrower answer)."""
    prefixes = [DEFAULT_JOURNAL_PREFIX]
    try:
        from core.wallet import collection_registry
        for prefix in collection_registry.journal_prefixes():
            if prefix not in prefixes:
                prefixes.append(prefix)
    except Exception:  # noqa: BLE001 — the default alone is the fail-closed direction
        logger.debug("collection registry unreadable; journal prefix = default only",
                     exc_info=True)
    return tuple(prefixes)


def _template_re(prefixes: Iterable[str]):
    alts = "|".join(re.escape(p) for p in prefixes)
    return re.compile(r"(?:" + alts + r")" + _TEMPLATE_BODY)


def journal_payload(*, account: str, chain: int, seq: int, kind: str, text_sha256: str,
                    prev: str, prefix: str = DEFAULT_JOURNAL_PREFIX) -> bytes:
    """The exact bytes the owner key signs (EIP-191) for one journal entry. ``prefix`` is
    the collection profile's ``journal_prefix`` (default ``agent``); it must be one of
    :func:`allowed_journal_prefixes`."""
    if kind not in JOURNAL_KINDS:
        raise ValueError(f"unknown journal kind {kind!r}")
    if not JOURNAL_PREFIX_RE.fullmatch(str(prefix)):
        raise ValueError(f"journal prefix {prefix!r} is not a valid prefix")
    out = JOURNAL_TEMPLATE.format(prefix=prefix, account=str(account).lower(), chain=int(chain),
                                  seq=int(seq), kind=kind, text_sha256=text_sha256,
                                  prev=prev).encode("utf-8")
    if not is_journal_template(out):
        raise ValueError("journal payload does not match the template (is the prefix pinned?)")
    return out


def is_journal_template(data: bytes, prefixes: Optional[Iterable[str]] = None) -> bool:
    """True when *data* is EXACTLY one journal entry under a configured prefix."""
    try:
        text = bytes(data).decode("utf-8")
        allowed = tuple(prefixes) if prefixes is not None else allowed_journal_prefixes()
        return bool(_template_re(allowed).fullmatch(text))
    except Exception:  # noqa: BLE001
        return False
