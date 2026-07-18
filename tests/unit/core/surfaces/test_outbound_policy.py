"""Outbound policy ladder (proposal 013 T5): open|domains|allowlist|off.

Pure model only — enforcement at the send gates is T6. Reuses the
`_enable_full` env recipe from tests/unit/agents/task/test_autonomy_mode.py.
"""
from agents.task import constants
from core.prefs import write_preference
from core.surfaces.outbound_policy import POLICY_LADDER, resolve_outbound_policy
from core.surfaces.outbound_target import resolve_target_tier


class _Allow:
    def __init__(self, allowed=()):
        self._a = set(allowed)

    def is_allowed(self, user_id, surface, target):
        return (surface, target) in self._a


def _enable_full(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    constants.reset_autonomy_mode_warnings()


# ---------------------------------------------------------------------------
# resolve_outbound_policy — mode-aware default, env override
# ---------------------------------------------------------------------------


def test_default_policy_supervised_is_allowlist(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    assert resolve_outbound_policy("rob", "email")[0] == "allowlist"


def test_default_policy_autonomous_is_open(monkeypatch):
    _enable_full(monkeypatch)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    assert resolve_outbound_policy("rob", "email")[0] == "open"


def test_env_tightens_over_mode_default(monkeypatch):
    _enable_full(monkeypatch)
    monkeypatch.setenv("OUTBOUND_POLICY", "domains")
    assert resolve_outbound_policy("rob", "email")[0] == "domains"


def test_policy_ladder_shape():
    assert POLICY_LADDER == ("open", "domains", "allowlist", "off")


# ---------------------------------------------------------------------------
# resolve_target_tier — keyword-only extension, default args byte-identical
# ---------------------------------------------------------------------------


def test_tier_allowlist_unchanged_by_default_args():
    # exact today-behavior when callers pass no policy/domains kwargs
    assert resolve_target_tier(surface="email", target="a@b.com", user_id="rob",
                               allowlist=_Allow(), owner_targets={}) == "denied"
    assert resolve_target_tier(surface="email", target="a@b.com", user_id="rob",
                               allowlist=_Allow({("email", "a@b.com")}),
                               owner_targets={}) == "allowlisted"
    assert resolve_target_tier(surface="email", target="own@x.com", user_id="rob",
                               allowlist=_Allow(),
                               owner_targets={"email": "own@x.com"}) == "owner"


def test_tier_open_policy_allows_unknown():
    assert resolve_target_tier(surface="email", target="new@any.org", user_id="rob",
                               allowlist=_Allow(), owner_targets={},
                               policy="open") == "open"


def test_tier_domains_policy_email_suffix():
    assert resolve_target_tier(surface="email", target="dev@corp.io", user_id="rob",
                               allowlist=_Allow(), owner_targets={},
                               policy="domains", domains=("corp.io",)) == "open"
    assert resolve_target_tier(surface="email", target="dev@evil.io", user_id="rob",
                               allowlist=_Allow(), owner_targets={},
                               policy="domains", domains=("corp.io",)) == "denied"


def test_tier_domains_policy_non_email_behaves_as_allowlist():
    assert resolve_target_tier(surface="telegram", target="@somebody", user_id="rob",
                               allowlist=_Allow(), owner_targets={},
                               policy="domains", domains=("corp.io",)) == "denied"


def test_tier_off_denies_even_allowlisted():
    assert resolve_target_tier(surface="email", target="a@b.com", user_id="rob",
                               allowlist=_Allow({("email", "a@b.com")}),
                               owner_targets={}, policy="off") == "denied"
    # owner is still reachable under off
    assert resolve_target_tier(surface="email", target="own@x.com", user_id="rob",
                               allowlist=_Allow(), owner_targets={"email": "own@x.com"},
                               policy="off") == "owner"


# ---------------------------------------------------------------------------
# pref merge — tighten-only (a pref can never loosen beyond env/mode default)
# ---------------------------------------------------------------------------


def test_pref_tightens_open_to_domains(monkeypatch, tmp_path):
    _enable_full(monkeypatch)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    write_preference(tmp_path, "rob", "outbound.policy", "domains")
    policy, _domains = resolve_outbound_policy("rob", "email", home_dir=tmp_path)
    assert policy == "domains"


def test_pref_tightens_open_to_off(monkeypatch, tmp_path):
    _enable_full(monkeypatch)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    write_preference(tmp_path, "rob", "outbound.policy", "off")
    policy, _domains = resolve_outbound_policy("rob", "email", home_dir=tmp_path)
    assert policy == "off"


def test_pref_cannot_loosen_allowlist_to_open(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    write_preference(tmp_path, "rob", "outbound.policy", "open")
    policy, _domains = resolve_outbound_policy("rob", "email", home_dir=tmp_path)
    assert policy == "allowlist"


def test_no_home_dir_skips_pref_layer(monkeypatch, tmp_path):
    # home_dir=None (default) must not consult the pref layer at all.
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    write_preference(tmp_path, "rob", "outbound.policy", "off")
    policy, _domains = resolve_outbound_policy("rob", "email")
    assert policy == "allowlist"


# ---------------------------------------------------------------------------
# outbound.domains — narrow-only merge (allowlist polarity, T5 review fix)
#
# union would invert the polarity here: a tenant pref adding a domain the
# operator never listed would WIDEN the reachable set past an operator env
# restriction, violating this module's own "loosening beyond the mode default
# is impossible from a pref" contract. narrow_list intersects instead.
# ---------------------------------------------------------------------------


def test_domains_pref_cannot_widen_past_env(monkeypatch, tmp_path):
    """Reviewer's exact scenario: env restricts to corp.io; a tenant pref of
    attacker.com must NOT reach the effective domain set at all."""
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.setenv("OUTBOUND_DOMAINS", "corp.io")
    write_preference(tmp_path, "rob", "outbound.domains", ["attacker.com"])
    _policy, domains = resolve_outbound_policy("rob", "email", home_dir=tmp_path)
    assert domains == ()
    # ...and attacker.com is therefore denied at the target-tier resolver too.
    assert resolve_target_tier(surface="email", target="dev@attacker.com", user_id="rob",
                               allowlist=_Allow(), owner_targets={},
                               policy="domains", domains=domains) == "denied"


def test_domains_pref_matching_env_survives_intersection(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.setenv("OUTBOUND_DOMAINS", "corp.io")
    write_preference(tmp_path, "rob", "outbound.domains", ["corp.io"])
    _policy, domains = resolve_outbound_policy("rob", "email", home_dir=tmp_path)
    assert domains == ("corp.io",)


def test_domains_pref_defines_set_when_env_empty(monkeypatch, tmp_path):
    # No operator restriction at all == no ceiling to widen past.
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("OUTBOUND_DOMAINS", raising=False)
    write_preference(tmp_path, "rob", "outbound.domains", ["corp.io"])
    _policy, domains = resolve_outbound_policy("rob", "email", home_dir=tmp_path)
    assert domains == ("corp.io",)


def test_tier_domains_policy_matches_case_insensitively():
    """The target domain is already lowercased; a pref-authored allowlist entry
    (unlike the always-lowercased OUTBOUND_DOMAINS env parser) may carry mixed
    case and must still match (T5 review fix)."""
    assert resolve_target_tier(surface="email", target="dev@Corp.IO", user_id="rob",
                               allowlist=_Allow(), owner_targets={},
                               policy="domains", domains=("Corp.IO",)) == "open"
