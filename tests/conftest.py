"""Shared pytest fixtures for the ROB test suite."""
import asyncio
import os

import pytest


# ---------------------------------------------------------------------------
# Operator-env sandbox (§3.5, 2026-07-16): tests must never read the dev box
# operator's REAL env files. The first test that built a CLI container ran
# core.bootstrap.load_env(local_mode=True), which read ~/.polyrob/.env (real
# provider keys / owner binding) and could backfill config/.env.production
# secrets into os.environ — poisoning every later test (the order-dependent
# failures: chat_resolver_parity, budget_gate, identity, protected_config_guard).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _operator_env_file_sandbox():
    """Disable the config/.env.production key backfill for the whole session.

    Without this, the first keyless test that ran load_env(local_mode=True)
    adopted up to ~144 production secrets into os.environ. NOTE: deliberately
    does NOT redirect POLYROB_HOME/HOME — many tests isolate by patching
    ``Path.home`` or setting HOME themselves, and a session-wide POLYROB_HOME
    would shadow that (it regressed ~20 init/identity/mcp-config tests when
    tried). The cross-test leak from ~/.polyrob/.env and the legacy ~/.rob/.env
    is handled by the per-test restore guard below instead.
    """
    prior = os.environ.get("POLYROB_ENV_KEY_BACKFILL")
    os.environ["POLYROB_ENV_KEY_BACKFILL"] = "0"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("POLYROB_ENV_KEY_BACKFILL", None)
        else:
            os.environ["POLYROB_ENV_KEY_BACKFILL"] = prior


# The narrow var set an operator env file can inject through load_env paths the
# session sandbox cannot redirect (the legacy direct ~/.rob/.env read): provider/
# model pins, the owner binding, and provider keys. Restored around EVERY test so
# one test's load_env cannot poison later tests. Deliberately a NAMED list, not a
# full environ snapshot — >function-scoped env fixtures stay intact (none touch
# these today; keep it that way).
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
)


@pytest.fixture(autouse=True)
def _restore_operator_env_vars():
    """Undo raw os.environ writes of the operator-file var set after each test.

    monkeypatch-based changes tear down before this runs (LIFO), so this only
    catches UNMANAGED writes — exactly the load_env injection class.
    """
    before = {k: os.environ.get(k) for k in _OPERATOR_ENV_VARS}
    yield
    for k, v in before.items():
        if os.environ.get(k) != v:
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
def _isolate_autonomy_state_store():
    """Keep restart-durable autonomy state OUT of the developer's real data home.

    ``get_autonomy_state_store()`` resolves ``autonomy_state.db`` under
    ``get_data_root()`` — on a dev machine that is the repo's ``.polyrob``. Any test
    touching the ReentryBudget singleton or an orchestrator would persist budget/
    delegation rows there and leak them into later test runs (this bit
    test_self_wake's singleton test on its second run). Durability tests inject an
    explicit store/tmp path, so forcing the flag off here doesn't reduce coverage.
    A test may still opt in by setting AUTONOMY_STATE_DURABLE inside its own body.
    """
    prev = os.environ.get("AUTONOMY_STATE_DURABLE")
    os.environ["AUTONOMY_STATE_DURABLE"] = "off"
    try:
        from agents.task.agent.core.self_wake import reset_reentry_budget
        reset_reentry_budget()
    except Exception:
        pass
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("AUTONOMY_STATE_DURABLE", None)
        else:
            os.environ["AUTONOMY_STATE_DURABLE"] = prev


@pytest.fixture(autouse=True)
def _reset_autonomy_marker_global():
    """The in-process autonomous-session marker is a module-global set; any test
    that runs a goal/cron helper (run_task_to_outcome marks ids like "s1")
    leaks it into unrelated suites — the forged-turn guard then misreads a
    genuine owner turn as autonomous (this bit tools/controller tests).
    Promoted from the goals-suite conftest to global scope. Fail-open."""
    try:
        from agents.task.goals import autonomy_marker
        autonomy_marker._SESSIONS.clear()
    except Exception:
        pass
    yield
    try:
        from agents.task.goals import autonomy_marker
        autonomy_marker._SESSIONS.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_telemetry_event_log(tmp_path, monkeypatch):
    """Keep the durable telemetry event log OUT of the developer's real data home.

    ``get_event_log()`` resolves ``telemetry_events.db`` under the data root — on
    a dev machine that is the repo's ``.polyrob``. Tests that trigger owner
    pushes/escalations (or, since §3.2, ANY user-bound delivery — the rail's
    dedup/rate memory lives in this log) would persist rows there and leak
    dedup state across test runs. Redirect the default to a per-test tmp db;
    tests that want a specific store still pass an explicit path/instance.
    """
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH",
                       str(tmp_path / "telemetry_events.db"))
    yield


@pytest.fixture(autouse=True)
def _isolate_deployed_apps_db(tmp_path, monkeypatch):
    """Keep the hf_deploy ``deployed_apps.db`` OUT of the developer's real data home.

    ``default_deployed_apps_db()`` resolves under the data root — on a dev
    machine that is the repo's ``.polyrob``. Redirect the default to a
    per-test tmp db (mirrors ``_isolate_telemetry_event_log`` above); the
    hf_deploy suite additionally passes an explicit ``db_path`` per test
    (belt), so this is the suspenders for any code path that resolves the
    default (e.g. the tool's production registry getter, the boot-reconcile
    sweep).
    """
    monkeypatch.setenv("DEPLOYED_APPS_DB_PATH", str(tmp_path / "deployed_apps.db"))
    yield


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
def _isolate_autonomy_halt_probe(tmp_path, monkeypatch):
    """Keep ``AutonomyConfig.autonomy_halted()``'s kill-switch probe out of the
    developer's real local data home.

    ``autonomy_halted()`` (agents/task/constants.py) is fail-CLOSED (H6 leg 3):
    besides the ``AUTONOMY_HALT`` env flag, it checks for an ``AUTONOMY_HALT``
    FILE at ``POLYROB_DATA_DIR``/``DATA_ROOT``/the RESOLVED data home
    (``core.runtime_paths.resolve_data_home()``). With none of those env vars
    set, ``resolve_data_home()`` converges on ``cwd/.polyrob`` — this repo's own
    real local dev data dir when pytest runs from the repo root. A developer who
    has ever run ``polyrob owner halt`` (or hand-touched the file) in this tree
    would otherwise have EVERY money-path test (PolicyGate, wallet, trading
    approval) that doesn't itself mock the probe silently fail-closed for a
    reason that has nothing to do with the test — and, worse, misattribute it to
    "the kill-switch is on" (see the PolicyGate probe-failure-reason fix).

    Close every lever the probe reads, without touching the ``resolve_data_home``
    FUNCTION object itself (several tests — ``test_data_home_resolver.py``,
    ``test_owner_halt.py`` — import/patch that function directly and must see
    the REAL implementation): drop the ``AUTONOMY_HALT``/``DATA_ROOT`` env
    overrides, and give every test its own throwaway ``POLYROB_DATA_DIR``
    (fresh, empty ``tmp_path`` subdir — never contains an ``AUTONOMY_HALT``
    file). ``resolve_data_home()`` itself honors ``POLYROB_DATA_DIR`` first, so
    one env var closes both the explicit ``bases[0]`` check AND the resolved-
    data-home check in the SAME call.

    A test that wants to exercise a REAL halt file or a specific data home sets
    its own env/monkeypatch inside the test body (e.g.
    ``test_autonomy_halted_fail_closed.py``, ``test_owner_halt.py``,
    ``test_data_home_resolver.py``) — applied after this fixture's setup, those
    calls win normally, so existing halt/data-home tests are unaffected.
    Fail-open (never raise from this fixture).
    """
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "autonomy_halt_isolate"))
    yield


@pytest.fixture(autouse=True)
def _reset_skill_usage_singleton():
    """Unbind the first-caller-wins skill-usage store singleton after every test.

    ``modules.skills.skill_usage.get_skill_usage_store`` binds the process-global
    ``_STORE`` to the FIRST data_dir it is called with. A test that records skill
    provenance (e.g. ``test_self_evolution.py`` creating authored skills for
    "gleb") pins the singleton to its tmp dir; every later test asking for a
    DIFFERENT data home silently reads that stale store — which made the recap
    "nothing to report" test order-dependent (2026-07-12 parity wave sweep).
    Fail-open, post-yield (mirrors the telemetry/event-log isolation above).
    """
    yield
    try:
        import modules.skills.skill_usage as _su
        with _su._STORE_LOCK:
            _su._STORE = None
    except Exception:
        pass
