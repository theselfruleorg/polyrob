"""Shared pytest fixtures for the ROB test suite."""
import asyncio
import importlib
import itertools
import os
import pathlib
import re
import sys
import threading

import pytest

# 067 P2/P3: the pack loader's phase 1 (policy rows from each installed pack's
# pack.toml) runs where the CLI's main() and api/app.py run it — before any
# policy view module is imported — and phase 2 (the enabled packs' tools and
# gates) runs once, as the tool tier's own registrations used to at import.
# Without this an installed pack's tools (anysite, perplexity) are unclassified
# in the test process. Never raises; a refusal is recorded in core.packs.state.
from core.packs.loader import load_packs as _load_packs, register_policies as _register_policies

_register_policies()
_load_packs()


# ---------------------------------------------------------------------------
# Operator-env sandbox (§3.5, 2026-07-16): tests must never read the dev box
# operator's REAL env files. The first test that built a CLI container ran
# polyrob.app.bootstrap.load_env(local_mode=True), which read ~/.polyrob/.env (real
# provider keys / owner binding) and could backfill config/.env.production
# secrets into os.environ — poisoning every later test (the order-dependent
# failures: chat_resolver_parity, budget_gate, identity, protected_config_guard).
# ---------------------------------------------------------------------------

# The config/.env.production key backfill is deleted (polyrob/app/bootstrap.py), so no session
# fixture has to switch it off; the per-test restore guard below handles ~/.rob/.env.


# The narrow var set an operator env file can inject through load_env paths the
# session sandbox cannot redirect (the legacy direct ~/.rob/.env read): provider/
# model pins, the owner binding, and provider keys. Restored around EVERY test so
# one test's load_env cannot poison later tests. Deliberately a NAMED list, not a
# full environ snapshot — >function-scoped env fixtures stay intact (none touch
# these today; keep it that way).
def _provider_key_env_vars() -> tuple:
    """Every provider key var the spec registry knows (ZAI_API_KEY, GLM_API_KEY,
    CEREBRAS_API_KEY, …). Derived, not hand-listed: the hand-list missed
    ZAI_API_KEY the day the dev box gained one in ~/.polyrob/.env (2026-08-14) —
    a doctor CliRunner test leaked it into os.environ and a later test in the
    same file saw a phantom usable provider. Fail-open to () — the guard then
    degrades to the explicit names below, never breaks collection.
    """
    try:
        from modules.llm.provider_spec import get_specs
        out = []
        for s in get_specs():
            out.extend(s.env_key_chain())
        return tuple(dict.fromkeys(out))
    except Exception:
        return ()


def _wallet_env_vars() -> tuple:
    """Every ``AGENT_WALLET_*`` flag the env-flag catalog knows.

    Derived, not hand-listed — for the same reason as
    ``_provider_key_env_vars``. The hand-list missed AGENT_WALLET_DERIVATION,
    so a CLI test's load_env injected the dev box's real scheme ('bip44') and
    every later test deriving a key from a TEST seed died on "not a valid
    BIP-39 mnemonic" (21 failures, 2026-08-27). The catalog is the env-flag
    SSOT and a reverse contract test already forces every new flag into it, so
    the next wallet flag is covered the day it is documented. Fail-open to ()
    — the named money-rail entries below stay the floor.
    """
    try:
        from core.flags_catalog import CATALOG
        return tuple(name for name, *_ in CATALOG
                     if name.startswith("AGENT_WALLET_") and "<" not in name)
    except Exception:
        return ()


_OPERATOR_ENV_VARS = (
    "DEFAULT_PROVIDER", "DEFAULT_MODEL", "CHAT_PROVIDER", "CHAT_MODEL",
    "POLYROB_OWNER_USER_ID", "POLYROB_OWNER_EMAIL", "POLYROB_OWNER_USERNAME",
    "POLYROB_OWNER_TELEGRAM_ID", "POLYROB_OWNER_PASSWORD_HASH",
    "BOT_OWNER_USER_ID", "BOT_OWNER_EMAIL",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
    "PERPLEXITY_API_KEY",
    # Frozen-security-flag class: `polyrob init` applies these to os.environ
    # in-process ("authoritative" by design), so a CliRunner init test leaks
    # them into every later test's _refreeze_* baseline (POLYROB_LOCAL is
    # already popped by the workspace-lock fixture).
    "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER",
    "PAYMENT_APPROVAL_MODE", "PAYMENT_APPROVAL_TIMEOUT_SEC",
    "APPROVAL_GRANT_TTL_HOURS", "AGENT_COMPUTE_POSTURE",
    "AUTONOMY_POSTURE", "AUTONOMY_MODE",
    # Identity/character pins: an operator env file (e.g. a dev box's
    # ./.polyrob/.env pinning the migrated legacy "rob" identity after the
    # DEFAULT_INSTANCE_ID "rob" -> "polyrob" flip) injects these through a
    # CliRunner load_env and would make every later test resolve the WRONG
    # instance tree (identity/rob vs identity/polyrob write/read mismatches).
    "POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID",
    "PERSONALITY_DEFAULT_CHARACTER", "DEFAULT_CHARACTER",
    # POLYROB_PROFILE is the third resolve_instance_id tier: apply_profile_env
    # raw-writes it, and monkeypatch.delenv(raising=False) on an ABSENT var
    # registers NO undo — so a profile-activation test leaks it and every later
    # test resolves the leaked profile name as the instance id.
    "POLYROB_PROFILE", "POLYROB_PROFILE_SOURCE",
    # Activation also pins the registry and home with raw writes. A leaked
    # registry can select a prior test's sticky profile, redirecting subsequent
    # CLI writes away from their patched Path.home(). Restore; never override
    # these globally, since tests intentionally exercise the normal home tier.
    "POLYROB_PROFILES_ROOT", "POLYROB_HOME", "POLYROB_PROFILE_RESOLVED",
    # Money-rail class (2026-08-24): X402_TREASURY_FROM_WALLET defaults ON, so
    # on any box where the wallet vars are in the ambient env (a dev machine,
    # the prod maintenance loop) `resolve_treasury_address()` would return the
    # operator's REAL address inside unit tests — a test that clears
    # X402_PAYMENT_RECIPIENT to assert "unconfigured" would instead get a live
    # treasury and issue real 402 challenges/scans against it.
    "AGENT_WALLET_ENABLED", "AGENT_WALLET_MASTER_SEED",
    "AGENT_WALLET_OPERATIONAL_VENUE",
    "X402_TREASURY_FROM_WALLET", "X402_PAYMENT_RECIPIENT", "X402_PAYMENT_ADDRESS",
    "X402_SETTLE_ONCHAIN_DETECT", "X402_SETTLEMENT_RPC", "X402_DEFAULT_CHAIN",
) + _provider_key_env_vars() + _wallet_env_vars()


def _try_import(name):
    """The module, or None when it cannot import in this install (a missing
    extra). Only ImportError is swallowed — any other import failure is a bug
    the suite must show."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _loaded(name):
    """The module if this process already imported it, else None. A reset of a
    module nobody imported is a no-op — and importing it just to reset it cost
    every test the import and put modules in ``sys.modules`` the test never used."""
    return sys.modules.get(name)


def _reset_singletons(after: bool) -> None:
    # The signer envelope caches the signer's caps per process (and keeps the
    # last good ones). A test that fakes a signer must not clamp the next test.
    m = _loaded("core.wallet.signer_envelope")
    if after and m is not None:
        try:
            m.reset_cache()
        except Exception:
            pass
    # 066 P0: the custody-secret holder and the hardening result are PROCESS-
    # level. A seed one test's load_wallet_config() took out of os.environ must
    # not be what a later, seedless test reads back.
    for name in ("core.security.custody_env", "core.security.process_hardening"):
        m = _loaded(name)
        if m is not None:
            try:
                m._reset_for_tests()
            except Exception:
                pass
    # 025: the session -> memory-scope binding is a process-global LRU; a scope
    # one test bound must never reach another test's recall.
    m = _loaded("modules.memory.scope")
    if m is not None:
        try:
            m.reset_bindings()
        except Exception:
            pass
    # The autonomous-session marker is a module-global set; a goal/cron helper
    # (run_task_to_outcome marks ids like "s1") leaked it and the forged-turn
    # guard misread a genuine owner turn as autonomous (tools/controller tests).
    m = _loaded("core.autonomy_marker")
    if m is not None:
        m._SESSIONS.clear()
    if after:
        # get_skill_usage_store binds _STORE to the FIRST data_dir it sees; a test
        # recording skill provenance pinned it to its tmp dir and the recap
        # "nothing to report" test became order-dependent (2026-07-12).
        m = _loaded("modules.skills.skill_usage")
        if m is not None:
            with m._STORE_LOCK:
                m._STORE = None
        # 064 F4: the bot-pair loop guard counts in process memory; a test that
        # sends bot lines must not spend a later test's budget.
        m = _loaded("core.surfaces.bot_pair_guard")
        if m is not None:
            try:
                m.reset()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _reset_process_singletons():
    """Reset the process-global singletons around every test (W10: seven
    fixtures merged into one; each block above names its incident).

    031: core.autonomy_control keeps two process-local registries (the warn-once
    path set and the transition hooks) and polyrob.app.autonomy_runtime a once-per-
    process sweep flag; a test that pauses, registers a hook or starts autonomy
    leaked them into every later test (order-dependent failures). The hooks a
    test found are restored, never dropped."""
    _reset_singletons(after=False)
    _ac = _loaded("core.autonomy_control")
    _hooks = None
    if _ac is not None:
        _ac._WARNED_PATHS.clear()
        _hooks = list(_ac._TRANSITION_HOOKS)
        _ac._TRANSITION_HOOKS.clear()
    try:
        yield
    finally:
        _ac = _ac or _loaded("core.autonomy_control")
        if _ac is not None:
            _ac._WARNED_PATHS.clear()
            _ac._TRANSITION_HOOKS[:] = _hooks or []
        _ar = _loaded("polyrob.app.autonomy_runtime")
        if _ar is not None:
            _ar._self_binding_sweep_scheduled = False
        _reset_singletons(after=True)


@pytest.fixture(autouse=True)
def _restore_operator_env_vars():
    """Undo raw os.environ writes after each test — the WHOLE environment.

    monkeypatch-based changes tear down before this runs (LIFO), so this only
    catches UNMANAGED writes: the load_env injection class, and code under test
    that setdefault()s surface flags or a POLYROB_OVERRIDES line. Restoring only
    the operator var set let 27 tests leak (2026-10-11: a leaked
    POLYROB_OVERRIDES / CORRESPONDENT_ACCESS_ENABLED denied every later
    dispatcher route on that xdist worker).
    """
    before = dict(os.environ)
    yield
    if dict(os.environ) != before:
        os.environ.clear()
        os.environ.update(before)
    # 026 P1.1: load_env now re-freezes the frozen policy flags ONCE per
    # process — a CliRunner test that triggered the first load_env with a
    # monkeypatched env would otherwise leave the frozen globals drifted for
    # the rest of the suite. Realign them with the just-restored env.
    try:
        from core.config_policy.policy import (_refreeze_compute_posture,
                                               _refreeze_payment_approval_flags)
        _refreeze_compute_posture()
        _refreeze_payment_approval_flags()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _cancel_leaked_agent_tasks():
    """Cancel TaskAgent's fire-and-forget periodic loops after each test.

    TaskAgent.initialize() spawns ``_periodic_cleanup``/``_periodic_workspace_cleanup``
    via ``asyncio.create_task`` with no owner. When a test's event loop tears down they
    are GC'd while pending → "Task was destroyed but it is pending" + "I/O operation on
    closed file" stderr spam. We cancel them by coroutine name on teardown. Fail-open.
    """
    yield
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        return
    if loop.is_closed():
        return
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    except RuntimeError:
        return
    cancelled = []
    for t in pending:
        coro = t.get_coro()
        name = getattr(coro, "__qualname__", "") or getattr(coro, "__name__", "")
        if "_periodic_cleanup" in name or "_periodic_workspace_cleanup" in name:
            t.cancel()
            cancelled.append(t)
    # Cancellation only takes effect when the loop steps again; drain here so the
    # tasks are actually finished (not merely flagged) before the loop closes.
    if cancelled and not loop.is_running():
        try:
            loop.run_until_complete(asyncio.gather(*cancelled, return_exceptions=True))
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolate_path_manager():
    """Reset process-global path state around every test (E1 leak guard).

    set_path_manager (used by build_cli_container and some integration fixtures)
    mutates a module-global singleton with no automatic teardown, so without this
    a project-scoped pm() set in one test would leak into later tests. We also drop
    POLYROB_WORKSPACE_LOCK_DIR (set via os.environ.setdefault in build_cli_container) and
    reset the interactive busy-depth, so the cross-process workspace lock doesn't
    leak a stale lock dir into an unrelated test. Reset before AND after so each test
    starts from — and leaves — the lazy default. Fail-open.
    """
    os.environ.pop("POLYROB_WORKSPACE_LOCK_DIR", None)
    # build_cli_container also does os.environ.setdefault("POLYROB_LOCAL", "1")
    # (bootstrap.py), which persists process-wide and flips the SAFE autonomy
    # defaults (e.g. CODING_TOOLS_ENABLED) ON — leaking into unrelated tests.
    os.environ.pop("POLYROB_LOCAL", None)
    # The `--project` CLI callback (cli/polyrob.py) does a RAW os.environ set of
    # POLYROB_PROJECT_DIR; a test that exercises it with monkeypatch.delenv(...,
    # raising=False) on an ABSENT key registers no teardown, so the value leaks
    # process-wide and poisons data-home/path-injection tests that read these vars.
    # Clear both project/data-home vars around every test. Fail-open.
    os.environ.pop("POLYROB_PROJECT_DIR", None)
    os.environ.pop("POLYROB_DATA_DIR", None)
    try:
        from agents.task.path import reset_path_manager
        reset_path_manager()
    except Exception:
        reset_path_manager = None  # type: ignore
    try:
        import core.interactive_gate as _ig
        _ig._busy_depth = 0
    except Exception:
        pass
    try:
        yield
    finally:
        os.environ.pop("POLYROB_WORKSPACE_LOCK_DIR", None)
        os.environ.pop("POLYROB_LOCAL", None)
        os.environ.pop("POLYROB_PROJECT_DIR", None)
        os.environ.pop("POLYROB_DATA_DIR", None)
        if reset_path_manager is not None:
            reset_path_manager()


@pytest.fixture(autouse=True)
def _local_data_home_follows_cwd(monkeypatch):
    """Keep the suite out of the developer's real ``~/.polyrob/data``.

    The product's local data home is ``<polyrob_home>/data`` (DATA-4: a cloned
    directory must not supply ``cwd/.polyrob``). The suite isolates by
    ``monkeypatch.chdir(tmp_path)``, so tests resolve the unconfigured local
    home under the cwd instead. Tests of the real default import
    ``core.runtime_paths._local_default_data_home`` at module load and patch it
    back. Fail-open on import."""
    try:
        from pathlib import Path as _P
        import core.runtime_paths as _rp
        monkeypatch.setattr(_rp, "_local_default_data_home",
                            lambda: (_P.cwd() / ".polyrob").resolve())
    except Exception:
        pass
    yield


# ---------------------------------------------------------------------------
# W10 (2026-10-10): the per-test temp directory, made O(1).
# pytest's own ``tmp_path`` allocates with ``make_numbered_dir``, which LISTS the
# whole base temp directory on every call to find the next free number. Over a
# run of N tests that is O(N^2): the webview suite (6,444 tests) spent 129 s of
# SYSTEM time listing directories. This override keeps the contract (a fresh,
# empty, unique directory per test under the session's base temp, kept by the
# normal base-temp rotation) and allocates by counter into shards of 1,000.
# ``tmpdir`` (the legacy py.path fixture) is built on ``tmp_path``, so it follows.
# ---------------------------------------------------------------------------

_FAST_TMP_SEQ = itertools.count()


def _fast_tmp_dir(tmp_path_factory, name: str) -> pathlib.Path:
    n = next(_FAST_TMP_SEQ)
    shard = tmp_path_factory.getbasetemp() / f"t{n // 1000:04d}"
    path = shard / f"{re.sub(r'[^A-Za-z0-9_]', '_', name)[:30]}-{n}"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    """A fresh, empty directory unique to this test (O(1) allocation; see above)."""
    return _fast_tmp_dir(tmp_path_factory, request.node.name)


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, tmp_path_factory, monkeypatch):
    """ONE per-test isolation of every durable store a test could reach (W10).

    This was fourteen autouse fixtures, each asking for ``tmp_path`` and each
    paying its own setup. They are one fixture now; every protection and every
    path is unchanged (the stores still live under the test's own ``tmp_path``,
    in the same sub-paths, so a test that opens ``tmp_path / "telemetry_events.db"``
    still reads the store the code under test wrote). Each block names the
    incident it exists for. Same-scope autouse fixtures run in NAME order
    (pytest collects a conftest's fixtures alphabetically), so this one runs
    after ``_isolate_path_manager``.
    """
    # -- the suite is a DEV CHECKOUT, wherever it runs (2026-09-21) -----------
    # core/admin_data_home.py reads the box's deployment evidence (the env file
    # /etc/polyrob/polyrob.env and the polyrob*.service units) to pick the owner
    # tenant and the deployed home, and the WS-G root guard REFUSES mutating owner
    # verbs typed as root. On the prod maintenance clone 80 CLI verb tests refused
    # instead of exercising their verb. Point the evidence at an EMPTY tmp tree;
    # the guard's own tests patch _deployed_box/_deployment_evidence back.
    try:
        import core.admin_data_home as _adh
        empty = tmp_path / "_no_deploy"
        monkeypatch.setattr(_adh, "DEPLOYED_ENV_FILE", str(empty / "polyrob.env"))
        monkeypatch.setattr(_adh, "UNIT_DIRS", (str(empty / "units"),))
    except Exception:
        pass

    # -- the wallet audit sink (H3 round 2, 2026-08-22) -----------------------
    # get_policy_gate()/get_agent_wallet() are PROCESS singletons built from
    # default_audit_sink(); the first test to reach them read the developer's REAL
    # audit trail (797 entries / $880 trailing-24h) and four unrelated money tests
    # failed "daily spend cap $100.00 would be exceeded". Substitute the ONE
    # resolver the sink and core.wallet.derivation share — NOT a global
    # POLYROB_DATA_DIR (the first attempt; it broke four cwd-relative tests).
    # Escape hatches, read at CALL time (fixture order is not guaranteed): an
    # explicit data_dir, or a POLYROB_DATA_DIR the TEST set, reach the REAL
    # resolver (test_wallet_audit_sink_isolation.py pins both). The factory
    # singleton is reset before and after every test.
    # A store whose module cannot import here (an install without its extra —
    # the static CI shard runs with none) cannot be reached by any test either.
    _audit_sink_mod = _try_import("core.wallet.audit_sink")
    _wallet_factory = _try_import("core.wallet.factory")
    if _audit_sink_mod is not None:
        wallet_root = str(tmp_path / "wallet_isolate")
        _real_wallet_data_dir = _audit_sink_mod._wallet_data_dir

        def _isolated_wallet_data_dir(data_dir=None, *, for_meta=False):
            if data_dir is not None:
                return _real_wallet_data_dir(data_dir, for_meta=for_meta)
            if (os.environ.get("POLYROB_DATA_DIR") or "").strip():
                return _real_wallet_data_dir(None, for_meta=for_meta)
            return os.path.join(wallet_root, "wallet")

        monkeypatch.setattr(_audit_sink_mod, "_wallet_data_dir", _isolated_wallet_data_dir)
    if _wallet_factory is not None:
        _wallet_factory.reset_agent_wallet_cache()

    # -- per-store default paths (each resolved under the data root before) ----
    # telemetry_events.db: owner pushes / ANY user-bound delivery keep their
    #   dedup/rate memory here; a real-home row leaked dedup state across runs.
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    # verdicts.db (057 WS-F): a rejected SMTP login / X-API 402 in a test would
    #   suppress the developer's own rails for 15 minutes. A SUBDIRECTORY: a file
    #   directly in tmp_path is visible to tests that walk their tmp dir.
    monkeypatch.setenv("VERDICTS_DB_PATH", str(tmp_path / "_verdicts" / "verdicts.db"))
    # deployed_apps.db (hf_deploy) and app_services.db (032): the registry getter
    #   and the boot-reconcile sweep resolve the default.
    monkeypatch.setenv("DEPLOYED_APPS_DB_PATH", str(tmp_path / "deployed_apps.db"))
    monkeypatch.setenv("APP_SERVICES_DB_PATH", str(tmp_path / "app_services.db"))
    # artifacts.db: the ledger and its cached singleton.
    monkeypatch.setenv("ARTIFACTS_DB_PATH", str(tmp_path / "artifacts.db"))
    # the ship rail: its DB AND its SERVED FILE TREE — on a box with a live vhost,
    #   files under the real publish root would be publicly served.
    monkeypatch.setenv("PUBLICATIONS_DB_PATH", str(tmp_path / "publications.db"))
    monkeypatch.setenv("PUBLISH_ROOT", str(tmp_path / "publish"))
    monkeypatch.setenv("PUBLISH_BASE_URL", "https://pub.test.invalid")

    # -- the session tree (pm() default root) --------------------------------
    # With DATA_ROOT and POLYROB_DATA_DIR unset (the normal test state, above),
    # resolve_session_data_root() falls back to the legacy ./data/task under the
    # cwd — the repo root, shared by every xdist worker and the developer's own
    # tree. Session-dir stores (core.session_control's control.sqlite3, the
    # feed, workspaces) then collided across workers: test_run_budget_gate's
    # `sess-budget` live-run row was seen by another worker. Point the fallback at
    # this test's tmp_path; a test that sets DATA_ROOT/POLYROB_DATA_DIR still
    # gets the env-driven branches unchanged.
    try:
        import core.runtime_paths as _rp_sessions
        monkeypatch.setattr(_rp_sessions, "_LEGACY_SESSIONS_DEFAULT",
                            str(tmp_path / "data" / "task"))
    except Exception:
        pass

    # -- the kill-switch probe (H6 leg 3) ------------------------------------
    # autonomy_halted() is fail-CLOSED and checks for an AUTONOMY_HALT FILE under
    # POLYROB_DATA_DIR / DATA_ROOT / the resolved home; a developer who ever ran
    # `polyrob owner halt` here would fail every money-path test as "kill-switch
    # on". Drop the env overrides (the resolve_data_home FUNCTION is left real —
    # its own tests patch it). The fixture this replaced also set a per-test
    # POLYROB_DATA_DIR, but autouse fixtures run in NAME order and
    # _isolate_path_manager (later in that order) popped it before every test, so
    # tests have always run with POLYROB_DATA_DIR UNSET — several depend on that
    # (cwd-relative homes), and the wallet/pause escape hatches above and below
    # read "set" as "the TEST set it". Kept that way on purpose.
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)

    # -- the 031 pause record -------------------------------------------------
    # state_bases ALWAYS appends the resolved home, so ac.pause(tmp_path) wrote a
    # real AUTONOMY_PAUSE.json into cwd/.polyrob; read_state is fail-CLOSED, so a
    # leaked PAUSE refuses every later goals/cron/money/social test. Substitute
    # the one seam (_resolved_home; state_bases is imported by name elsewhere).
    # Escape hatches at CALL time: a test-set POLYROB_DATA_DIR, or a test-patched
    # resolve_data_home, get the REAL resolution (test_autonomy_pause_isolation.py).
    try:
        from core import autonomy_control as _ac
        import core.runtime_paths as _rp
        pause_home = str(tmp_path / "autonomy_pause_isolate")
        _real_resolved_home = _ac._resolved_home
        _real_resolver = _rp.resolve_data_home

        def _isolated_resolved_home():
            if (os.environ.get("POLYROB_DATA_DIR") or "").strip():
                return _real_resolved_home()
            if _rp.resolve_data_home is not _real_resolver:
                return _real_resolved_home()
            return pause_home

        monkeypatch.setattr(_ac, "_resolved_home", _isolated_resolved_home)
    except Exception:
        pass

    # -- run safety state and the owner login budget --------------------------
    refusal_taint = _try_import("core.security.refusal_taint")
    refusal_taint_store = _try_import("core.security.refusal_taint_store")
    if refusal_taint_store is not None:
        monkeypatch.setattr(refusal_taint_store, "path", lambda: tmp_path / "refusal_taint.db")
    if refusal_taint is not None:
        refusal_taint.reset_for_tests()
    login_limits = _try_import("webview.login_limits")
    if login_limits is not None:
        monkeypatch.setattr(login_limits, "_db_path", lambda: tmp_path / "login_attempts.db")

    # -- credential verdicts / artifacts / publish: drop the cached singletons --
    _cv = _try_import("core.credential_verdicts")
    _artifacts = _try_import("core.artifacts")
    _publish = _try_import("core.publish")

    def _drop_store_singletons():
        if _cv is not None:
            _cv._reset_for_tests()
        if _artifacts is not None:
            _artifacts.reset_artifact_ledger()
        if _publish is not None:
            _publish.reset_publish_store()

    _drop_store_singletons()

    # -- the action-card store: every quote on a money verb writes a card row --
    # Its own directory, outside tmp_path (a test that lists its tmp_path must not
    # see cards.db), created on FIRST USE — most tests never touch a card.
    _cards = None
    try:
        from core.surfaces import cards as _cards
        _cards.set_store(_LazyCardStore(_cards, tmp_path_factory))
    except Exception:
        _cards = None

    # -- 066 P1: never install into / import from the operator's lazy overlays,
    # never run a real network install by accident. Both roots point into a
    # per-test directory (outside tmp_path); the trusted install refuses unless a
    # test patches it back.
    _ld = importlib.import_module("core.lazy_deps")
    overlay = pathlib.Path(str(tmp_path) + "-lazy-overlay")

    def _overlay(leaf):
        overlay.mkdir(exist_ok=True)
        return overlay / leaf

    monkeypatch.setattr(_ld, "system_root", lambda: _overlay("system"))
    monkeypatch.setattr(_ld, "local_root", lambda: _overlay("local"))

    def _blocked(feature, *, wheel_only):
        raise _ld.FeatureUnavailable(
            f"{feature}: real lazy installs are blocked under pytest (tests/conftest.py). "
            f"Remedy: {_ld.remedy(feature)}", "test_blocked")

    monkeypatch.setattr(_ld, "_trusted_install", _blocked)

    try:
        yield
    finally:
        if _wallet_factory is not None:
            _wallet_factory.reset_agent_wallet_cache()
        if refusal_taint is not None:
            refusal_taint.reset_for_tests()
        _drop_store_singletons()
        if _cards is not None:
            _cards.set_store(None)
            _cards._LISTENERS.clear()


class _LazyCardStore:
    """A ``CardStore`` whose directory and schema are made on first use.

    Creating the sqlite file eagerly cost ~2.5 ms on every test of the suite."""

    def __init__(self, cards_mod, tmp_path_factory):
        self._mod = cards_mod
        self._factory = tmp_path_factory
        self._real = None
        # A test may first touch the store from two threads at once (a tap
        # thread polling while the action inserts); both must get ONE store.
        self._lock = threading.Lock()

    def _store(self):
        if self._real is None:
            with self._lock:
                if self._real is None:
                    d = _fast_tmp_dir(self._factory, "cards")
                    self._real = self._mod.CardStore(str(d / "cards.db"))
        return self._real

    def __getattr__(self, name):
        return getattr(self._store(), name)


@pytest.fixture(autouse=True)
def _credit_sentinel_off():
    """Default the provider-credit sentinel OFF per-test (mirrors
    ``_isolate_autonomy_state_store`` below).

    Any test that drives a fake 402/credit-death through the REAL
    error-recovery/trip path writes a fresh ``CREDIT_SENTINEL`` file under the
    shared data home; every later goals/cron test then honestly refuses to
    dispatch ("provider-credit sentinel active") — this is what turned the
    public 0.8.0 CI red across 31 unrelated tests. Sentinel-behavior tests
    opt back in with ``monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "true")``
    (plus a tmp ``POLYROB_DATA_DIR``)."""
    prev = os.environ.get("CREDIT_SENTINEL_ENABLED")
    os.environ["CREDIT_SENTINEL_ENABLED"] = "off"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CREDIT_SENTINEL_ENABLED", None)
        else:
            os.environ["CREDIT_SENTINEL_ENABLED"] = prev


@pytest.fixture(autouse=True)
def _isolate_autonomy_state_store(monkeypatch, tmp_path_factory):
    """Keep restart-durable autonomy state OUT of the developer's real data home.

    ``get_autonomy_state_store()`` resolves ``autonomy_state.db`` under
    ``get_data_root()`` — on a dev machine that is the repo's ``.polyrob``. Any test
    touching the ReentryBudget singleton or an orchestrator would persist budget/
    delegation rows there and leak them into later test runs (this bit
    test_self_wake's singleton test on its second run). Durability tests inject an
    explicit store/tmp path, so the default store is None here (the in-memory
    registries) and the default path points at an empty temp dir, so the boot
    recovery sweep never reads the real DB. A test may still monkeypatch
    ``get_autonomy_state_store`` itself to opt in.
    """
    try:
        from agents.task.agent import autonomy_state
        monkeypatch.setattr(autonomy_state, "get_autonomy_state_store", lambda: None)
        _iso = tmp_path_factory.getbasetemp() / "_autonomy_state_iso"
        monkeypatch.setattr(autonomy_state, "default_autonomy_state_db",
                            lambda: str(_iso / "autonomy_state.db"))
    except Exception:
        pass
    try:
        from agents.task.agent.core.self_wake import reset_reentry_budget
        reset_reentry_budget()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_provider_registry():
    """Keep the developer's real ``~/.polyrob/providers.yaml`` OUT of unit runs.

    The ProviderSpec registry (proposal 024) merges user-declared providers over
    the built-ins; several tests pin the exact six-provider set, so a provider
    declared on the dev machine would flake them. ``LLM_CUSTOM_PROVIDERS`` set-but-
    empty disables user-file loading; the cache reset ensures no snapshot built
    under a different env leaks across tests. A test may still opt in by setting
    ``LLM_CUSTOM_PROVIDERS`` inside its own body (after a cache reset).
    """
    prev = os.environ.get("LLM_CUSTOM_PROVIDERS")
    os.environ["LLM_CUSTOM_PROVIDERS"] = ""

    def _reset():
        m = _loaded("modules.llm.provider_spec")
        if m is not None:
            try:
                m.reset_provider_registry_cache()
            except Exception:
                pass

    _reset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("LLM_CUSTOM_PROVIDERS", None)
        else:
            os.environ["LLM_CUSTOM_PROVIDERS"] = prev
        _reset()


@pytest.fixture(autouse=True)
def _restore_container_singleton():
    """Restore ``DependencyContainer._instance`` after every test.

    A test that builds a ``TaskAgent(config=…, container=…)`` (or calls
    ``get_instance(config)``) left the process-wide singleton behind, so every
    LATER test that read ``get_instance()`` got that test's double — the
    2026-09-21 doctor-endpoint flake was exactly this: a leaked container made
    ``build_ledger``'s wallet leg run and mint a telemetry store between the two
    halves of one comparison. Save/restore (never blanket-None) keeps a
    module-scoped container a test set up on purpose."""
    from core.container import DependencyContainer
    before = DependencyContainer._instance
    yield
    DependencyContainer._instance = before


@pytest.fixture(autouse=True)
def _isolate_external_skill_roots(monkeypatch):
    """Default external (agentskills.io ecosystem) skill discovery to zero roots for
    every test (Task 14, ``skill_discovery.user_external_roots``).

    That function reads real host paths (``~/.agents/skills``, ``~/.claude/skills``)
    by default. On a dev machine where either is populated — e.g. a developer using
    Claude Code has plugin skills under ``~/.claude/skills`` — a plain
    ``SkillManager()`` would silently pick up host-dependent catalog entries,
    breaking hermetic test isolation (this bit two pre-existing catalog tests before
    this guard was added). A test that wants to exercise the real merge overrides
    this explicitly via ``monkeypatch.setattr`` in its own body (see
    ``test_skill_discovery_user_scope.py``), which wins over this default because it
    runs later, inside the test. Fail-open.
    """
    try:
        from agents.task.agent import skill_discovery
        monkeypatch.setattr(skill_discovery, "user_external_roots", lambda: [], raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_inbound_denial_budgets(monkeypatch):
    from core.rate_limit import SlidingWindowLimiter
    from core.surfaces import command_reply
    monkeypatch.setattr(command_reply, '_DENIAL_SENDERS', SlidingWindowLimiter(max_calls=1, window_seconds=60, max_keys=4096))
    monkeypatch.setattr(command_reply, '_DENIAL_TOTAL', SlidingWindowLimiter(max_calls=50, window_seconds=60, max_keys=1))


# ---------------------------------------------------------------------------
# W10: the `slow` marker, applied by rule rather than by hand. A test module is
# slow when it spawns a real process, browser or container (the source names
# subprocess.run/Popen/check_*, asyncio.create_subprocess_*, sys.executable,
# playwright, or a docker CLI/SDK call) or lives under tests/install/ (pip,
# venvs, wheels). CI runs these in their own shard; the inner loop is
# `pytest -m "not slow"`. A hand-written @pytest.mark.slow still works.
# ---------------------------------------------------------------------------

_SLOW_SOURCE = re.compile(
    r"subprocess\.(?:run|Popen|check_output|check_call|call)\("
    r"|asyncio\.create_subprocess_|sys\.executable"
    r"|async_playwright|sync_playwright|chromium\.launch"
    r"|docker\.from_env|[\"']docker[\"']\s*,")
_SLOW_DIRS = ("tests/install/",)
_SLOW_BY_FILE: dict = {}


def _module_is_slow(path: pathlib.Path) -> bool:
    hit = _SLOW_BY_FILE.get(path)
    if hit is None:
        rel = path.as_posix()
        hit = any(f"/{d}" in rel for d in _SLOW_DIRS)
        if not hit:
            try:
                hit = bool(_SLOW_SOURCE.search(path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                hit = False
        _SLOW_BY_FILE[path] = hit
    return hit


def pytest_collection_modifyitems(config, items):
    slow = pytest.mark.slow
    for item in items:
        path = getattr(item, "path", None)
        if path is not None and path.suffix == ".py" and _module_is_slow(path):
            item.add_marker(slow)
