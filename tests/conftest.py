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
        return tuple(name for name, _group, _default, _desc in CATALOG
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
def _tests_are_not_a_deployed_box(tmp_path, monkeypatch):
    """The suite runs as a DEV CHECKOUT, wherever it runs.

    ``core/admin_data_home.py`` reads the box's deployment evidence — the env
    file ``/etc/polyrob/polyrob.env`` and the ``polyrob*.service`` units — to
    pick the owner tenant, the deployed data home and (via the WS-G root guard
    in ``cli/_admin_home.py``) to REFUSE mutating owner verbs typed as root.
    On a deployed box's maintenance clone (sitting beside that env file, run as
    root) 80 CLI verb tests refused with the remedy text instead of exercising
    their verb, and the keys test resolved tenant ``rob`` from the deployed env
    (2026-09-21). Every seat under test points at ``tmp_path``, so the suite
    never touches the shared home: point the evidence at an EMPTY tmp tree so
    every box looks like a dev checkout. The guard's own tests patch
    ``_deployed_box`` / ``_deployment_evidence`` back themselves. Fail-open on
    import."""
    try:
        import core.admin_data_home as _adh
        empty = tmp_path / "_no_deploy"
        monkeypatch.setattr(_adh, "DEPLOYED_ENV_FILE", str(empty / "polyrob.env"))
        monkeypatch.setattr(_adh, "UNIT_DIRS", (str(empty / "units"),))
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_wallet_audit_sink(tmp_path, monkeypatch):
    """Keep the wallet PolicyGate/AgentWallet — and the durable audit sink they
    read real trailing-24h spend from — OUT of the developer's real data home,
    WITHOUT touching the global ``POLYROB_DATA_DIR`` env var.

    H3 fix round 2 (2026-08-22 review): ``core.wallet.factory.get_policy_gate()``
    / ``get_agent_wallet()`` are PROCESS-LEVEL singletons (module globals
    ``_cached``/``_resolved``) built from ``default_audit_sink()``, which
    resolves ``<POLYROB_DATA_DIR or resolve_data_home()>/wallet/audit.jsonl`` —
    on a dev machine running pytest from the repo root with no override, that is
    this repo's own real ``.polyrob/wallet/audit.jsonl``. H3 gave
    ``WALLET_DAILY_CAP_USD`` a finite $100 default (was unbounded); the FIRST
    unit test in a session that reaches ``get_policy_gate()`` — in ANY
    directory, not just ``tests/unit/core/wallet/``/``tests/unit/tools/x402/``
    (which already reset the cache themselves) — silently built its singleton
    from this developer's REAL, populated audit trail (797 entries / $880
    trailing-24h spend, diagnosed live) and then kept that cached object for the
    rest of the WHOLE pytest session regardless of any later test's own env.
    Four tests outside the wallet-owned directories
    (hyperliquid/polymarket/crypto_trade_gate) failed with "daily spend cap
    $100.00 would be exceeded" as a direct, confirmed result (toggling
    ``WALLET_DAILY_CAP_USD=none`` alone made all four pass). Unit tests must
    never be able to reach a real durable audit sink at all — a root-level
    guarantee, not a directory-by-directory one, mirroring
    ``_isolate_autonomy_state_store`` below for ``AUTONOMY_STATE_DURABLE``.

    ⚠️ FIRST ATTEMPT AT THIS FIX (still visible in git history) set
    ``POLYROB_DATA_DIR`` globally via ``monkeypatch.setenv`` — that broke FOUR
    unrelated tests that specifically depend on ``POLYROB_DATA_DIR`` being
    UNSET so their own ``monkeypatch.chdir(tmp_path)`` drives a CWD-relative
    resolution (``test_cli_container_workspace_is_cwd``,
    ``test_config_data_dir_resolves_under_base_dir``, the digest/init-guardrail
    prefs tests, and a telegram-harness test that failed outright because the
    isolated dir was never even created on disk for that consumer). A global
    env var is too broad a lever for a wallet-specific problem. This version
    instead monkeypatches the ONE shared resolver both the audit sink AND
    ``core.wallet.derivation`` funnel through —
    ``core.wallet.audit_sink._wallet_data_dir`` (both do a lazy
    ``from core.wallet.audit_sink import ...`` at call time, so patching the
    module attribute redirects both) — so ONLY wallet state is isolated and
    every other ``POLYROB_DATA_DIR`` consumer in the suite is completely
    unaffected.

    TWO escape hatches keep the wallet's own tests honest, because substituting
    unconditionally would silently NEUTER them (they would then assert the
    fixture's behaviour, not the production resolver's):
      * an explicit ``data_dir`` argument still reaches the REAL resolver;
      * a test that sets ``POLYROB_DATA_DIR`` itself still gets the REAL
        env-anchored resolution — this is what keeps
        ``test_wallet_meta_resolution_survives_cwd_change_via_data_dir_env`` and
        ``test_wallet_meta_path_and_audit_sink_share_directory_by_default``
        meaningful. This does not reopen the leak: the leak path is the
        NO-override branch (``resolve_data_home()`` -> ``cwd/.polyrob``), and
        ``_isolate_path_manager`` pops ``POLYROB_DATA_DIR`` before every test, so
        a value present at call time was set by the test itself and points at
        that test's own location.
    The substitution therefore covers exactly the one branch that could reach a
    real durable sink, and nothing else.

    Also resets ``core.wallet.factory``'s process-level singleton before AND
    after every test — closing the SEPARATE hazard where a stale cached
    PolicyGate/AgentWallet (built under one test's isolated dir, e.g. holding
    recorded spend from that test's own trades) survives into a LATER test
    with a different — but still isolated-away-from-disk — tmp path, causing
    cross-test spend-cap flakiness within the suite itself.
    """
    from core.wallet import audit_sink as _audit_sink_mod
    isolated_root = str(tmp_path / "wallet_isolate")
    _real_wallet_data_dir = _audit_sink_mod._wallet_data_dir

    def _isolated_wallet_data_dir(data_dir=None, *, for_meta=False):
        # Both escape hatches are evaluated at CALL time (inside the test body),
        # never at fixture-setup time. That is deliberate: pytest does NOT order
        # same-scope autouse fixtures by declaration order, and
        # ``_isolate_path_manager`` does a raw ``os.environ.pop("POLYROB_DATA_DIR")``
        # at ITS setup — a setup-time read here would race that pop. Reading the
        # env inside the substituted callable removes the ordering dependency
        # entirely: by the time production code calls this, every fixture has run.
        if data_dir is not None:
            return _real_wallet_data_dir(data_dir, for_meta=for_meta)
        if (os.environ.get("POLYROB_DATA_DIR") or "").strip():
            return _real_wallet_data_dir(None, for_meta=for_meta)
        return os.path.join(isolated_root, "wallet")

    monkeypatch.setattr(_audit_sink_mod, "_wallet_data_dir", _isolated_wallet_data_dir)
    from core.wallet.factory import reset_agent_wallet_cache
    reset_agent_wallet_cache()
    try:
        yield
    finally:
        reset_agent_wallet_cache()


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
        try:
            from modules.llm.provider_spec import reset_provider_registry_cache
            reset_provider_registry_cache()
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
def _isolate_credential_verdicts(tmp_path, monkeypatch):
    """Keep the external-rail verdict store OUT of the developer's real data home.

    057 WS-F made ``core.credential_verdicts`` durable (``verdicts.db`` under the
    data home). A test that records a rejected SMTP login or an X-API 402 would
    otherwise write a REAL verdict into ``~/.polyrob`` and suppress the
    developer's own email/X rails for the next 15 minutes. The module also caches
    its schema-init per path and keeps a fail-open fallback dict, so both are
    reset around every test.
    """
    # A SUBDIRECTORY: a stray file planted directly in ``tmp_path`` is visible to
    # every test that walks its own tmp dir (the knowledge-inventory suite does).
    monkeypatch.setenv("VERDICTS_DB_PATH", str(tmp_path / "_verdicts" / "verdicts.db"))
    from core import credential_verdicts as _cv
    _cv._reset_for_tests()
    yield
    _cv._reset_for_tests()


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
def _isolate_artifacts_db(tmp_path, monkeypatch):
    """Keep the artifact ledger OUT of the developer's real data home.

    Mirrors ``_isolate_deployed_apps_db``: ``default_artifacts_db()`` resolves
    under the data root, which on a dev machine is the repo's ``.polyrob``.
    The cached module singleton is dropped too, so a test that already built
    one cannot leak it into the next test's tmp path.
    """
    monkeypatch.setenv("ARTIFACTS_DB_PATH", str(tmp_path / "artifacts.db"))
    from core.artifacts import reset_artifact_ledger
    reset_artifact_ledger()
    yield
    reset_artifact_ledger()


@pytest.fixture(autouse=True)
def _isolate_publish_store(tmp_path, monkeypatch):
    """Keep the ship rail's DB and its SERVED FILE TREE out of the real data home.

    The publish root is a directory a test could otherwise populate under the
    developer's ~/.polyrob — and on a box where the vhost is live, those files
    would be publicly served.
    """
    monkeypatch.setenv("PUBLICATIONS_DB_PATH", str(tmp_path / "publications.db"))
    monkeypatch.setenv("PUBLISH_ROOT", str(tmp_path / "publish"))
    monkeypatch.setenv("PUBLISH_BASE_URL", "https://pub.test.invalid")
    from core.publish import reset_publish_store
    reset_publish_store()
    yield
    reset_publish_store()


@pytest.fixture(autouse=True)
def _isolate_app_services_db(tmp_path, monkeypatch):
    """032: keep the durable app registry out of the developer's real data home
    (mirrors ``_isolate_deployed_apps_db``; the app_service suite also passes an
    explicit db_path per test)."""
    monkeypatch.setenv("APP_SERVICES_DB_PATH", str(tmp_path / "app_services.db"))
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
def _isolate_autonomy_control_module_state():
    """031: ``core.autonomy_control`` keeps two process-local registries — the
    warn-once path set and the transition hooks — and ``core.autonomy_runtime``
    keeps a once-per-process sweep flag. A test that pauses, registers a hook or
    starts autonomy would otherwise leak that state into every later test in the
    same process (order-dependent failures). Reset both around each test."""
    try:
        from core import autonomy_control as _ac
    except Exception:
        yield
        return
    _ac._WARNED_PATHS.clear()
    _hooks = list(_ac._TRANSITION_HOOKS)
    _ac._TRANSITION_HOOKS.clear()
    try:
        yield
    finally:
        _ac._WARNED_PATHS.clear()
        _ac._TRANSITION_HOOKS[:] = _hooks
        try:
            from core import autonomy_runtime as _ar
            _ar._self_binding_sweep_scheduled = False
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolate_autonomy_pause_record(tmp_path, monkeypatch):
    """Keep the 031 pause record OUT of the developer's real data home.

    ``core.autonomy_control.state_bases`` ALWAYS appends the resolved data home,
    even when an explicit ``data_dir`` was passed. So ``ac.pause(tmp_path)`` in a
    test that does not ALSO pin the resolver writes a real
    ``AUTONOMY_PAUSE.json`` (and ``AUTONOMY_PAUSE.json.lock``, and an
    ``autonomy_state.db`` audit row) into ``cwd/.polyrob`` — this repo's own live
    dev home when pytest runs from the repo root. Verified: with the repo
    conftest loaded, ``state_bases(tmp)`` returns
    ``[<tmp>, '<repo>/.polyrob']``, and ``<repo>/.polyrob/AUTONOMY_PAUSE.json.lock``
    exists today as the fossil of exactly that. A leaked PAUSE record is the
    worst possible leak in this suite: ``read_state`` is fail-CLOSED, so every
    later goals/cron/money/social test in ANY directory would honestly refuse to
    run for a reason that has nothing to do with the test — the same failure
    class ``_credit_sentinel_off`` and ``_isolate_autonomy_state_store`` exist
    for. That every 031 test patches ``resolve_data_home`` by hand today (and
    ``test_twitter_pause_gate.py`` carries a "⚠️ Never call ac.pause() without
    isolating state_bases" comment) is the proof the footgun is real.

    Substitutes the ONE seam every base beyond the caller's own ``data_dir``
    funnels through — ``core.autonomy_control._resolved_home`` — exactly as
    ``_isolate_wallet_audit_sink`` substitutes ``_wallet_data_dir``. Patching
    ``state_bases`` itself would NOT work: ``core.surfaces.owner_admin`` imports
    that name at module import, so it holds its own binding.

    TWO escape hatches keep the pause tests honest (substituting
    unconditionally would make them assert the fixture, not production):
      * a test that sets ``POLYROB_DATA_DIR`` itself gets the REAL resolution
        (which honours that variable, so it points at the test's own dir);
      * a test that monkeypatches ``core.runtime_paths.resolve_data_home`` gets
        the REAL resolution too (it pinned the home deliberately).
    Both are evaluated at CALL time, never at fixture-setup time: pytest does not
    order same-scope autouse fixtures deterministically, and
    ``_isolate_path_manager`` pops ``POLYROB_DATA_DIR`` at ITS setup.
    """
    try:
        from core import autonomy_control as _ac
        import core.runtime_paths as _rp
    except Exception:
        yield
        return
    isolated = str(tmp_path / "autonomy_pause_isolate")
    _real_resolved_home = _ac._resolved_home
    _real_resolver = _rp.resolve_data_home

    def _isolated_resolved_home():
        if (os.environ.get("POLYROB_DATA_DIR") or "").strip():
            return _real_resolved_home()
        if _rp.resolve_data_home is not _real_resolver:
            return _real_resolved_home()
        return isolated

    monkeypatch.setattr(_ac, "_resolved_home", _isolated_resolved_home)
    yield


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
    "alice") pins the singleton to its tmp dir; every later test asking for a
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
