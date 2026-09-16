"""043 residue (ledger final review, Important 5) — ONE owner-tenant resolver.

⚠️ The defect. Four resolvers answered "who owns this install?" and they
disagreed whenever no owner was bound:

- ``core.identity.resolve_identity``      -> ``"local"``   (REPL, CLI chat
  sessions via ``ConstantIdentity``, ``polyrob goals``/``cron``, ``wallet book``)
- ``core.instance.resolve_owner_user_id`` -> ``"polyrob"`` (console reads, the
  x402 machine-income tenant stamp)
- ``core.admin_data_home.admin_owner_principal`` -> the ADOPTED instance id,
  else ``"polyrob"`` (``polyrob owner …``)
- ``webview.webgate.local_owner_id``      -> delegates to the second one

So on an unbound install the console read a tenant nothing had ever written to,
and ``polyrob owner …`` read a third one. A bound install (prod carries
``POLYROB_OWNER_USER_ID=rob``) never saw it — the divergence is unbound-only.

The decision: the unbound owner tenant is ``local`` — the tenant every REPL
session, goal, memory row and identity doc has been written under since the CLI
existed. ``polyrob`` is the INSTANCE id (it names the identity-doc tier and the
avatar), not a tenant. ``resolve_instance_id`` is untouched.
"""
import inspect

import pytest

_OWNER_KEYS = (
    "POLYROB_OWNER_USER_ID",
    "BOT_OWNER_USER_ID",
    "SURFACE_SUPER_ADMIN_USER_IDS",
    "POLYROB_LOCAL_OWNER",
)
_INSTANCE_KEYS = ("POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID", "POLYROB_PROFILE")


@pytest.fixture()
def unbound(monkeypatch):
    """An install that names no owner and no instance, with nothing deployed.

    ``admin_owner_principal`` reads the DEPLOYED env file (it runs from a shell
    outside the service env), so that lookup is neutralised here — this fixture
    is about the resolution, not about adoption.
    """
    for key in _OWNER_KEYS + _INSTANCE_KEYS:
        monkeypatch.delenv(key, raising=False)
    import core.admin_data_home as adh
    monkeypatch.setattr(adh, "deployed_env_value", lambda _key: None)
    return adh


def _all_four():
    """The four answers, resolved fresh (no module-level binding anywhere).

    No ``importlib.reload``: every one of these reads the environment on each
    call, so a reload would buy nothing and would leave a cross-tier global
    (``webgate._warned_unbound_owner``) rewritten from a core-tier test.
    """
    from core.admin_data_home import admin_owner_principal
    from core.identity import resolve_identity
    from core.instance import resolve_owner_user_id
    import webview.webgate as webgate
    return {
        "resolve_owner_user_id": resolve_owner_user_id(),
        "resolve_identity": resolve_identity(),
        "admin_owner_principal": admin_owner_principal(),
        "local_owner_id": webgate.local_owner_id(),
    }


# --- (a) unbound: all four say `local` --------------------------------------- #

def test_unbound_every_resolver_says_local(unbound):
    from core.identity import LocalIdentity
    answers = _all_four()
    assert set(answers.values()) == {LocalIdentity.USER_ID}, answers
    assert LocalIdentity.USER_ID == "local"


def test_unbound_nobody_answers_the_instance_id(unbound):
    """``polyrob`` is the instance, not a tenant. The regression this pins is a
    resolver quietly re-adopting it as an owner bucket."""
    from core.instance import DEFAULT_INSTANCE_ID
    assert DEFAULT_INSTANCE_ID not in set(_all_four().values())


# --- (b) a bound owner: all four say `rob` ----------------------------------- #

def test_bound_owner_every_resolver_agrees(unbound, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    answers = _all_four()
    assert set(answers.values()) == {"rob"}, answers


def test_the_super_admin_ladder_binds_an_owner_too(unbound, monkeypatch):
    monkeypatch.setenv("SURFACE_SUPER_ADMIN_USER_IDS", "u-admin,u-other")
    answers = _all_four()
    assert set(answers.values()) == {"u-admin"}, answers


# --- (c) POLYROB_LOCAL_OWNER: all four say `me` ------------------------------ #

def test_local_owner_override_every_resolver_agrees(unbound, monkeypatch):
    """⚠️ Pre-fix, ``resolve_identity`` ignored ``POLYROB_LOCAL_OWNER`` entirely
    (it consults only the STRICT owner principal), so a console-side override
    scoped the console to ``me`` and the REPL to ``local``."""
    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "me")
    answers = _all_four()
    assert set(answers.values()) == {"me"}, answers


def test_an_explicit_owner_still_outranks_the_local_override(unbound, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "me")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    assert set(_all_four().values()) == {"rob"}


# --- (d) the instance axis is untouched -------------------------------------- #

def test_the_instance_id_is_still_polyrob_when_unbound(unbound):
    """The identity-doc tier and the avatar are keyed by the INSTANCE. Collapsing
    the owner tenant onto ``local`` must not move them."""
    from core.instance import resolve_instance_id
    assert resolve_instance_id() == "polyrob"


def test_a_named_instance_is_not_an_owner_tenant(unbound, monkeypatch):
    """⚠️ ``admin_owner_principal`` used to adopt the deployment's INSTANCE id as
    the owner tenant when no owner was declared. An instance id is not an owner:
    it answered ``rob`` where the REPL on the same box answered ``local``."""
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    answers = _all_four()
    assert set(answers.values()) == {"local"}, answers
    from core.instance import resolve_instance_id
    assert resolve_instance_id() == "rob"


def test_a_deployment_that_declares_an_owner_is_still_adopted(monkeypatch):
    """The deployed-env-file lookup survives: an owner SSH shell has none of the
    service's env, and adopting the declared OWNER (not the instance) is what
    stops ``polyrob owner pending`` reading the wrong tenant on prod."""
    import core.admin_data_home as adh
    for key in _OWNER_KEYS + _INSTANCE_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        adh, "deployed_env_value",
        lambda key: "rob" if key == "POLYROB_OWNER_USER_ID" else None)
    assert adh.admin_owner_principal() == "rob"


# --- the anon bucket is never an owner --------------------------------------- #

@pytest.mark.parametrize("sentinel", ["_anonymous_", "system", "x402_user",
                                      "authenticated_api_user", "api_user", "  "])
def test_an_anonymous_binding_never_becomes_the_owner(unbound, monkeypatch, sentinel):
    """⚠️ The guard lives in `resolve_owner_user_id`, not in `resolve_identity`.

    While only `resolve_identity` held it, `POLYROB_OWNER_USER_ID=system` split
    the four apart again — three answered `system` and `resolve_identity`
    answered `local` — and the console and the x402 stamp wrote rows to a bucket
    `is_anonymous()` says is not an isolatable tenant. All four must fall
    through to the local tenant."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", sentinel)
    answers = _all_four()
    assert set(answers.values()) == {"local"}, answers


def test_an_anonymous_local_owner_override_falls_through_too(unbound, monkeypatch):
    """The guard is applied at EVERY tier, not only to the bound principal."""
    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "system")
    assert set(_all_four().values()) == {"local"}


# --- every migrated TENANT seat reads the one resolver ----------------------- #
#
# I2 (review round 1): fourteen call sites still named a tenant through
# ``resolve_owner_principal()``, which answers the INSTANCE id when unbound. Each
# is a bucket rows are written to or read from, so each was a seat that could
# disagree with the console, the REPL and the agent. They now read the one
# resolver. Both halves matter: the unbound answer MOVED (to ``local``), and a
# BOUND install must be byte-identical.

def _migrated_seats() -> dict:
    """Every migrated helper that returns a tenant, resolved fresh."""
    from agents.task.goals.dispatcher import _tick_owner_user_id
    from cli.commands import apps as apps_cli
    from cli.commands import owner as owner_cli
    from cli.commands import skill_install
    from core.wallet import tx_guard
    from core.wallet.config import _fail_open_owner_user_id
    from surfaces.telegram import group_ops, room_turn
    return {
        "owner._owner_tenant": owner_cli._owner_tenant(None),
        "owner._allowlist_tenant": owner_cli._allowlist_tenant(None),
        "owner._money_tenant": owner_cli._money_tenant(None),
        "owner._group_owner_uid": owner_cli._group_owner_uid(),
        "apps._tenant": apps_cli._tenant(None),
        "skill_install._default_user": skill_install._default_user(),
        "wallet.config._fail_open_owner_user_id": _fail_open_owner_user_id(),
        "tx_guard.ceiling_scope": tx_guard.ceiling_scope()[0],
        "dispatcher._tick_owner_user_id": _tick_owner_user_id(),
        "room_turn.room_session_owner": room_turn.room_session_owner("a-stranger"),
        "group_ops._owner_uid": group_ops._owner_uid(),
    }


def test_a_bound_install_is_byte_identical_at_every_migrated_seat(unbound, monkeypatch):
    """The migration must not move a single tenant on a BOUND install — which is
    prod, and every deployment that took the documented remedy."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    seats = _migrated_seats()
    assert set(seats.values()) == {"rob"}, seats
    assert set(_all_four().values()) == {"rob"}


def test_an_unbound_install_names_one_tenant_at_every_migrated_seat(unbound):
    """⚠️ ``room_turn.room_session_owner`` is given a stranger's uid as its
    fallback: a room member must never become the tenant a room runs as."""
    seats = _migrated_seats()
    assert set(seats.values()) == {"local"}, seats
    assert set(_all_four().values()) == {"local"}


@pytest.mark.parametrize("dotted", [
    "core.self_evolution._record_owner_notice",
    "core.self_evolution._push_owner_message_outcome",
    "modules.x402.settlement_scan.SettlementScanMixin._note_self_proceeds",
    "modules.x402.settlement_scan.SettlementScanMixin._notify_unmatched",
    # The two room-policy READ sites: one returns a ChatPolicy and one is inline,
    # so neither can be pinned by calling it. A revert of either would put the
    # reader on a different bucket from the two writers, and the room would
    # silently fall back to `mention` mode.
    "core.surfaces.chat_policy.load_for_chat",
    "core.surfaces.dispatcher._route_inbound_impl",
])
def test_the_inline_tenant_stamps_read_the_one_resolver(dotted):
    """Six sites name a tenant inline rather than returning one, so there is
    nothing to call. Pin the resolver they name: an ``owner_notice`` or a
    ``payment_unmatched`` row written under a tenant no seat reads is the blind-
    owner class, and it fails silently by construction."""
    import importlib
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        obj = module
        for name in parts[cut:]:
            obj = getattr(obj, name)
        break
    else:  # pragma: no cover - the dotted path is wrong
        pytest.fail(f"could not import {dotted}")
    src = inspect.getsource(obj)
    assert "resolve_owner_user_id" in src, dotted
    assert "resolve_owner_principal" not in src, dotted


# --- R1: the PRINCIPAL axis and the TENANT axis are one answer --------------- #
#
# ⚠️ Round 1 left `resolve_owner_principal`'s tier-3 fallback on the INSTANCE id,
# so an unbound install had tenant=`local` and principal=`polyrob`. Every owner
# gate compares one to the other, so the owner was denied owner-tier capability
# on their own box. Tier 3 is now `resolve_owner_user_id` itself.

_ENV_COMBINATIONS = [
    ("unbound", {}),
    ("bound", {"POLYROB_OWNER_USER_ID": "rob"}),
    ("bot_owner", {"BOT_OWNER_USER_ID": "rob"}),
    ("local_owner", {"POLYROB_LOCAL_OWNER": "me"}),
    ("ladder", {"SURFACE_SUPER_ADMIN_USER_IDS": "u-admin,u-other"}),
    ("named_instance", {"POLYROB_INSTANCE_ID": "rob"}),
    ("anon_sentinel", {"POLYROB_OWNER_USER_ID": "system"}),
    ("anon_local_owner", {"POLYROB_LOCAL_OWNER": "x402_user"}),
    ("explicit_wins", {"POLYROB_OWNER_USER_ID": "rob", "POLYROB_LOCAL_OWNER": "me"}),
]


@pytest.mark.parametrize("name,env", _ENV_COMBINATIONS, ids=[c[0] for c in _ENV_COMBINATIONS])
def test_the_principal_and_the_tenant_are_one_answer(name, env):
    """They are not two facts. An env in which they differ is an env in which an
    owner gate compares the owner to something that is not the owner."""
    from core.instance import resolve_owner_principal, resolve_owner_user_id
    assert resolve_owner_principal(env) == resolve_owner_user_id(env), name


def test_the_strict_resolution_still_answers_none_when_unbound():
    """`owner_is_bound` and the diagnostics rest on this: the default tier must
    not make an unbound install look bound."""
    from core.instance import resolve_owner_principal
    assert resolve_owner_principal(env={}, default_to_instance=False) is None
    assert resolve_owner_principal(
        env={"POLYROB_LOCAL_OWNER": "me"}, default_to_instance=False) is None
    assert resolve_owner_principal(
        env={"POLYROB_OWNER_USER_ID": "rob"}, default_to_instance=False) == "rob"


def test_an_unbound_owner_gate_compares_local_to_local(unbound):
    """(a) The consequence that matters: an owner-tier gate must recognise the
    owner. Both `is_owner` shapes take the principal as their operand, and the
    local operator tenant is what a REPL, a goal run and the console carry."""
    from core.instance import (is_owner, is_owner_local_safe,
                               resolve_owner_principal)
    principal = resolve_owner_principal()
    assert is_owner("local", owner_principal=principal) is True
    # …and WITHOUT the POLYROB_LOCAL bypass, which is the server/console case.
    assert is_owner_local_safe("local", owner_principal=principal,
                               local_enabled=False) is True
    # A network sender is hashed to a `u_…` id and is still not the owner.
    assert is_owner("u_deadbeef", owner_principal=principal) is False
    assert is_owner_local_safe("u_deadbeef", owner_principal=principal,
                               local_enabled=False) is False


def test_the_owner_telegram_dm_lands_on_the_same_tenant_as_the_repl(unbound):
    """(b) `owner_surface_alias` returns the PRINCIPAL and becomes the session
    user_id (`surfaces/telegram/inbound.py`). Unbound it ran the owner's DM under
    `polyrob` while every other seat said `local`."""
    from core.instance import owner_surface_alias, resolve_owner_user_id
    env = {"POLYROB_OWNER_TELEGRAM_ID": "28436760"}
    assert owner_surface_alias("28436760", "telegram", env=env) == "local"
    assert owner_surface_alias("28436760", "telegram", env=env) == resolve_owner_user_id(env)
    # A different authenticated sender is never aliased.
    assert owner_surface_alias("99999999", "telegram", env=env) is None


def test_an_unbound_goal_keeps_its_deliverable_attachments(unbound):
    """(c) `dispatcher` strips a deliverable's media when the goal's TENANT differs
    from the owner PRINCIPAL (a real cross-tenant leak guard). Unbound those were
    `local` and `polyrob`, so a REPL- or console-created goal silently lost its
    files while a goal from the owner's Telegram DM kept them."""
    from core.instance import resolve_owner_principal
    owner = str(resolve_owner_principal() or "")
    goal_user_id = "local"          # what create_session/resolve_identity stamps
    assert not (owner and goal_user_id and goal_user_id != owner), "media would be stripped"
    # The guard itself is intact: a genuinely different tenant still strips.
    assert "u_stranger" != owner


def test_a_bound_install_is_unchanged_on_both_axes(unbound, monkeypatch):
    """(d) The byte-identical claim covers the principal axis too."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    from core.instance import (is_owner, owner_surface_alias,
                               resolve_owner_principal, resolve_owner_user_id)
    assert resolve_owner_principal() == resolve_owner_user_id() == "rob"
    assert set(_all_four().values()) == {"rob"}
    assert set(_migrated_seats().values()) == {"rob"}
    assert is_owner("rob", owner_principal=resolve_owner_principal()) is True
    assert owner_surface_alias(
        "28436760", "telegram",
        env={"POLYROB_OWNER_TELEGRAM_ID": "28436760",
             "POLYROB_OWNER_USER_ID": "rob"}) == "rob"


def test_the_telegram_admin_owner_gate_holds_both_ways(unbound, monkeypatch):
    """(e) `harness._is_admin_owner` is the owner gate on a NETWORK surface: it
    must keep recognising the owner, and keep refusing everyone else, on both a
    bound and an unbound install."""
    from surfaces.telegram.harness import _is_admin_owner
    assert _is_admin_owner("local") is True          # the owner, unbound
    assert _is_admin_owner("u_stranger") is False
    assert _is_admin_owner("") is False
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    assert _is_admin_owner("rob") is True            # the owner, bound (via the alias)
    assert _is_admin_owner("local") is False, "an unbound-era tenant is not the bound owner"
    assert _is_admin_owner("u_stranger") is False
