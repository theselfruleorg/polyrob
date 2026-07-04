"""BotInstance / AgentIdentity + self-context loader (polyrob Phase A/C skeleton).

The instance abstraction is the inert skeleton for later multi-instance work:
instance_id defaults to "rob" so a single-instance deploy is byte-equivalent.
load_self_context reads operator-authored SOUL/IDENTITY docs from the instance
home dir (empty -> "" -> inert).
"""
import os
from pathlib import Path

import pytest

from core.instance import (
    AgentIdentity,
    BotInstance,
    console_display_name,
    resolve_instance_id,
    resolve_owner_principal,
    is_owner,
    is_owner_local_safe,
    load_self_context,
    SELF_CONTEXT_PER_DOC_MAX_CHARS,
)


def test_owner_principal_defaults_to_instance_id():
    # Auto-derive (2026-07-03): with nothing explicit bound, the owner principal is the
    # instance's own tenant (defaults to "rob"), so a single-user deploy unifies the
    # owner's chat/CLI with autonomy WITHOUT retyping the instance's name in env.
    assert resolve_owner_principal(env={}) == "rob"


def test_owner_principal_defaults_to_custom_instance_id():
    env = {"POLYROB_INSTANCE_ID": "acme"}
    assert resolve_owner_principal(env=env) == "acme"


def test_owner_principal_strict_returns_none_when_unbound():
    # default_to_instance=False is the STRICT resolution for diagnostics / layered
    # fallbacks (owner_admin summary, webgate.local_owner_id) that must distinguish an
    # explicitly-bound owner from the auto-derived instance default.
    assert resolve_owner_principal(env={}, default_to_instance=False) is None


def test_owner_principal_strict_still_honors_explicit_binding():
    assert resolve_owner_principal(
        env={"POLYROB_OWNER_USER_ID": "x"}, default_to_instance=False) == "x"


def test_owner_principal_from_polyrob_env():
    assert resolve_owner_principal(env={"POLYROB_OWNER_USER_ID": "u-owner"}) == "u-owner"


def test_owner_principal_falls_back_to_first_super_admin():
    # An explicit super-admin binding takes precedence over the instance-id default.
    env = {"SURFACE_SUPER_ADMIN_USER_IDS": "admin1, admin2"}
    assert resolve_owner_principal(env=env) == "admin1"


def test_owner_principal_polyrob_wins_over_super_admin():
    env = {"POLYROB_OWNER_USER_ID": "explicit", "SURFACE_SUPER_ADMIN_USER_IDS": "admin1"}
    assert resolve_owner_principal(env=env) == "explicit"


def test_is_owner_local_mode_is_always_owner():
    assert is_owner("anyone", owner_principal=None, local=True)


def test_is_owner_matches_principal_on_server():
    assert is_owner("u-owner", owner_principal="u-owner", local=False)
    assert not is_owner("someone-else", owner_principal="u-owner", local=False)


def test_is_owner_false_when_no_principal_and_not_local():
    assert not is_owner("anyone", owner_principal=None, local=False)


def test_is_owner_empty_user_never_owner():
    assert not is_owner("", owner_principal="", local=True)
    assert not is_owner(None, owner_principal=None, local=True)


# --- is_owner_local_safe (permissions audit F4) ----------------------------
# The blanket `is_owner(uid, local=True)` returns True for ANY non-empty uid, so a
# call site that reads the *global* POLYROB_LOCAL flag (with no surface filter, unlike
# access.py/pairing.py) would treat a network sender as the owner. is_owner_local_safe
# honors the local bypass ONLY for the genuine single-user local operator tenant
# ("local"); a network sender's hashed `u_…` id or owner alias never equals it.

def test_local_safe_allows_the_local_operator_tenant():
    # The CLI operator (LocalIdentity -> user_id "local") stays owner under local mode,
    # even though "local" != the owner principal.
    assert is_owner_local_safe("local", owner_principal="rob", local_enabled=True)


def test_local_safe_denies_network_sender_even_under_local():
    # THE FIX: a forgeable network uid must NOT become owner just because POLYROB_LOCAL
    # is set on a process that also has a network surface attached.
    assert not is_owner_local_safe("u_ab12cd", owner_principal="rob", local_enabled=True)


def test_local_safe_owner_by_principal_still_owner():
    # A network owner (telegram alias -> owner principal) is owner via principal match,
    # independent of the local bypass.
    assert is_owner_local_safe("rob", owner_principal="rob", local_enabled=True)
    assert is_owner_local_safe("alice", owner_principal="alice", local_enabled=False)


def test_local_safe_no_bypass_when_local_disabled():
    assert not is_owner_local_safe("local", owner_principal="rob", local_enabled=False)


def test_local_safe_empty_user_never_owner():
    assert not is_owner_local_safe("", owner_principal="rob", local_enabled=True)
    assert not is_owner_local_safe(None, owner_principal="rob", local_enabled=True)


def test_instance_id_defaults_to_rob():
    assert resolve_instance_id(env={}) == "rob"


def test_instance_id_from_bot_instance_id_env():
    assert resolve_instance_id(env={"BOT_INSTANCE_ID": "acme"}) == "acme"


def test_instance_id_polyrob_env_wins_over_legacy_rob_alias():
    # POLYROB_INSTANCE_ID is the canonical name; BOT_INSTANCE_ID also accepted.
    env = {"BOT_INSTANCE_ID": "legacy", "POLYROB_INSTANCE_ID": "canonical"}
    assert resolve_instance_id(env=env) == "canonical"


def test_instance_id_blank_falls_back_to_rob():
    assert resolve_instance_id(env={"BOT_INSTANCE_ID": "  "}) == "rob"


def test_agent_identity_and_bot_instance_are_frozen():
    ident = AgentIdentity(name="rob", role="specialist", voice=None, core_truths=[])
    inst = BotInstance(
        instance_id="rob",
        identity=ident,
        home_dir=Path("/tmp/.rob"),
        owner_principal=None,
        allowed_surfaces=frozenset({"cli"}),
        autonomy_policy={},
    )
    assert inst.instance_id == "rob"
    with pytest.raises(Exception):
        inst.instance_id = "other"  # frozen


def test_load_self_context_empty_when_no_docs(tmp_path):
    assert load_self_context(tmp_path) == ""


def test_load_self_context_reads_identity_and_operating(tmp_path):
    idir = tmp_path / "identity"
    idir.mkdir()
    (idir / "identity.md").write_text("I am ROB, a careful operator.")
    (idir / "operating.md").write_text("Always confirm before destructive ops.")
    out = load_self_context(tmp_path)
    assert "I am ROB, a careful operator." in out
    assert "Always confirm before destructive ops." in out
    # identity comes before operating
    assert out.index("I am ROB") < out.index("Always confirm")


def test_load_self_context_per_doc_char_cap(tmp_path):
    idir = tmp_path / "identity"
    idir.mkdir()
    big = "x" * (SELF_CONTEXT_PER_DOC_MAX_CHARS + 500)
    (idir / "identity.md").write_text(big)
    out = load_self_context(tmp_path)
    # the raw doc is truncated to the per-doc cap (plus a short marker)
    assert len(out) < len(big)
    assert "x" * 100 in out  # content still present


def test_load_self_context_ignores_blank_docs(tmp_path):
    idir = tmp_path / "identity"
    idir.mkdir()
    (idir / "identity.md").write_text("   \n  ")
    assert load_self_context(tmp_path) == ""


def test_console_display_name_default():
    assert console_display_name(env={}) == "POLYROB Console"


def test_console_display_name_env_override():
    env = {"POLYROB_CONSOLE_NAME": "Rob Console"}
    assert console_display_name(env=env) == "Rob Console"


def test_console_display_name_blank_override_falls_back():
    env = {"POLYROB_CONSOLE_NAME": "   "}
    assert console_display_name(env=env) == "POLYROB Console"
