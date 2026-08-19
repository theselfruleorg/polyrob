"""Pure resolver: is this outbound target the owner, an allowlisted party, or
reachable under the outbound policy ladder (open/domains), or denied? Owner-address
map is injected (from core/instance resolvers at the call site) so this stays pure
and unit-testable.

``policy``/``domains`` (proposal 013 T5, ``core.surfaces.outbound_policy``) are
keyword-only, defaulted params — a caller that passes neither gets EXACTLY today's
allowlist-only behavior, byte-identical. Check order: owner -> policy="off" denies
everything else -> allowlisted -> policy="open" allows anything -> policy="domains"
allows an email-shaped target whose domain matches -> denied.
"""

import re

# Telegram public-username shape (5-32 chars, letter first). Kept conservative:
# only strings that can ONLY be a username get '@'-prefixed.
_TG_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TG_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^/?#\s]+)",
                         re.IGNORECASE)


def normalize_surface_target(surface: str, target):
    """Best-effort normalization of an agent-typed outbound target (pure).

    Telegram (live 2026-08-15..16 journal failures):
    - ``t.me/thepublicden`` / ``https://t.me/thepublicden`` → ``@thepublicden``
      (the Bot API takes ``@username`` or a numeric id, never a link — the raw
      link fails "Bad Request: chat not found").
    - bare ``thepublicden`` → ``@thepublicden`` (same failure shape).
    - numeric ids, ``-100…`` channel ids, ``@handles`` and invite links
      (``t.me/+hash``, ``t.me/joinchat/…`` — not addressable chat ids at all)
      pass through unchanged.
    Other surfaces: unchanged.
    """
    if surface != "telegram" or not isinstance(target, str):
        return target
    t = target.strip()
    m = _TG_LINK_RE.match(t)
    if m:
        seg = m.group(1)
        if seg.startswith("+") or seg.lower() == "joinchat":
            return t  # invite link: no addressable chat id exists to extract
        t = seg
    if t.startswith("@") or not t or t.lstrip("-").isdigit():
        return t
    if _TG_USERNAME_RE.match(t):
        return f"@{t}"
    return t


def is_bot_username(surface: str, target) -> bool:
    """True when a telegram target can only be a BOT account (``…bot`` handle).

    Telegram requires bot usernames to end in "bot", and the Bot API hard-fails
    bot→bot messages ("Forbidden: bots can't send messages to bots") — refusing
    before the send lets the agent read WHY instead of retrying a dead call.
    (A channel whose @username happens to end in "bot" is refused too; the
    refusal text points at using the channel's numeric ``-100…`` id instead.)
    """
    if surface != "telegram" or not isinstance(target, str):
        return False
    t = target.strip()
    return t.startswith("@") and t.lower().endswith("bot")


def resolve_target_tier(*, surface: str, target: str, user_id: str, allowlist,
                        owner_targets: dict, policy: str = "allowlist",
                        domains: tuple = ()) -> str:
    owner_addr = (owner_targets or {}).get(surface)
    if owner_addr is not None and str(target) == str(owner_addr):
        return "owner"
    if policy == "off":
        return "denied"
    if allowlist is not None and allowlist.is_allowed(user_id, surface, target):
        return "allowlisted"
    if policy == "open":
        return "open"
    if policy == "domains" and "@" in str(target):
        dom = str(target).rsplit("@", 1)[1].strip().lower()
        # domains entries are compared case-insensitively: the target's domain
        # is already lowercased above, but a pref-authored entry (e.g. hand-set
        # via /config, not the always-lowercased OUTBOUND_DOMAINS env parser)
        # may carry mixed case — without lowering it here too, an allowlist
        # entry like "Corp.IO" would silently never match (T5 review fix).
        if any(dom == d.strip().lower() or dom.endswith("." + d.strip().lower())
               for d in domains):
            return "open"
    return "denied"
