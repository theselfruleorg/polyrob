"""Pure resolver: is this outbound target the owner, an allowlisted party, or denied?
Owner-address map is injected (from core/instance resolvers at the call site) so this
stays pure and unit-testable."""

def resolve_target_tier(*, surface: str, target: str, user_id: str, allowlist,
                        owner_targets: dict) -> str:
    owner_addr = (owner_targets or {}).get(surface)
    if owner_addr is not None and str(target) == str(owner_addr):
        return "owner"
    if allowlist is not None and allowlist.is_allowed(user_id, surface, target):
        return "allowlisted"
    return "denied"
