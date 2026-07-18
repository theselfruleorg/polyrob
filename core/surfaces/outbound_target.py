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
