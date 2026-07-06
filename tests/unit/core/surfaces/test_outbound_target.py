from core.surfaces.outbound_target import resolve_target_tier

class _AL:
    def __init__(self, allowed): self._a = set(allowed)
    def is_allowed(self, user_id, surface, target): return (user_id, surface, target) in self._a

def test_owner_target_is_owner():
    tier = resolve_target_tier(surface="telegram", target="999", user_id="rob",
                               allowlist=_AL([]), owner_targets={"telegram": "999"})
    assert tier == "owner"

def test_allowlisted():
    al = _AL([("rob", "telegram", "555")])
    tier = resolve_target_tier(surface="telegram", target="555", user_id="rob",
                               allowlist=al, owner_targets={"telegram": "999"})
    assert tier == "allowlisted"

def test_unknown_denied():
    tier = resolve_target_tier(surface="telegram", target="555", user_id="rob",
                               allowlist=_AL([]), owner_targets={"telegram": "999"})
    assert tier == "denied"

def test_owner_beats_allowlist_lookup():
    # owner target never needs an allowlist row
    tier = resolve_target_tier(surface="email", target="me@x.com", user_id="rob",
                               allowlist=_AL([]), owner_targets={"email": "me@x.com"})
    assert tier == "owner"
