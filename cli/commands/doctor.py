"""`polyrob doctor` — read-only health + config legibility check.

Surfaces which provider keys are present (and whether they're actually usable), the
resolved provider/model, the active memory backend, and the POLYROB_LOCAL footgun (it
flips a group of safe autonomy flags ON). Pure ``doctor_report`` does the work so it
is testable without a live container.
"""
import importlib.util
import os
import sys
from pathlib import Path

import click

from agents.task.constants import _FALSEY
from cli.config_store import resolve_provider_model
from core.path_safety import is_within_root
from core.runtime_paths import resolve_runtime_paths
from modules.llm.profiles import PROFILES, providers_with_keys, usable_providers_with_keys


def local_flag_on(env: dict, absent_means_on: bool = True) -> bool:
    """Resolve POLYROB_LOCAL for a given execution context (pure).

    The CLI (build_cli_container) does ``os.environ.setdefault("POLYROB_LOCAL",
    "1")``, so for run/chat an ABSENT value means ON (``absent_means_on=True``,
    the CLI default). A server process (e.g. the webview) never gets that
    setdefault — it passes ``absent_means_on=False`` so the report matches what
    the runtime factories (``bool_env(..., False)``) actually resolve.
    An explicitly-falsey value reads off in both contexts.
    """
    raw = env.get("POLYROB_LOCAL")
    if raw is None:
        return absent_means_on
    return str(raw).strip().lower() not in _FALSEY


def resolve_memory_backend(env: dict, rob_local: bool) -> str:
    """Mirror modules.memory.backend_factory's default: local_vector under
    POLYROB_LOCAL, else sqlite. An explicit MEMORY_BACKEND always wins."""
    default = "local_vector" if rob_local else "sqlite"
    return env.get("MEMORY_BACKEND", default) or default


def python_version_line() -> str:
    """Python-version floor check (O6): POLYROB targets Python >= 3.11."""
    v = sys.version_info
    line = f"python: {v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < (3, 11):
        line += "  ! POLYROB requires Python >= 3.11"
    return line


def server_extra_line() -> str:
    """[server]-extra presence (O6) — `polyrob serve` raises a raw ImportError
    without it; say so BEFORE the user hits that."""
    missing = [m for m in ("fastapi", "uvicorn") if importlib.util.find_spec(m) is None]
    if not missing:
        return "server extra: present (`polyrob serve` available)"
    return ("server extra: absent (" + ", ".join(missing) + " missing) — "
            "run `pip install 'polyrob[server]'` before `polyrob serve`")


def playwright_line(env: dict) -> str:
    """Playwright + chromium probe (O6) — the guide's own most-common issue.
    Filesystem heuristic over the browsers cache; never launches anything."""
    if importlib.util.find_spec("playwright") is None:
        return "playwright: not installed (browser tool unavailable; web_fetch unaffected)"
    override = (env.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if override:
        candidates = [Path(override)]
    else:
        home = Path.home()
        candidates = [
            home / "Library" / "Caches" / "ms-playwright",   # macOS
            home / ".cache" / "ms-playwright",               # Linux
        ]
        local_app = (env.get("LOCALAPPDATA") or "").strip()
        if local_app:
            candidates.append(Path(local_app) / "ms-playwright")  # Windows
    for cand in candidates:
        try:
            if cand.is_dir() and any(p.name.startswith("chromium") for p in cand.iterdir()):
                return "playwright: installed, chromium present"
        except OSError:
            continue
    return ("playwright: installed but NO chromium browser — run "
            "`python -m playwright install chromium`")


def live_activity_line() -> str:
    """Live-activity pipeline check (019 P0.3): telemetry constructs.

    A failed TelemetryManager init silently kills EVERY live tool/step line
    for a session (the orchestrator falls back to a no-op dummy) — the exact
    "agent looks idle while working" failure. Probe the construction path here
    so the breakage is visible before a session hits it. Never raises.
    """
    try:
        from agents.task.telemetry.manager import TelemetryManager

        TelemetryManager(session_id="doctor-probe", agent_id="doctor_doctor-probe")
        return "live activity: OK (telemetry pipeline constructs; feed events will render)"
    except Exception as e:
        return (
            f"live activity: BROKEN — telemetry init failed ({type(e).__name__}: {e}); "
            "sessions will run with NO live tool/step lines"
        )


def schema_status_line(env: dict) -> str:
    """DB-schema-vs-code check (U10): compare bot.db's recorded schema version
    against the code's migration HEAD. Read-only; never raises."""
    try:
        from migrations.version_manager import latest_migration_version
        head = latest_migration_version()
    except Exception:
        return "db schema: unknown (could not resolve code schema version)"
    data_home = (env.get("POLYROB_DATA_DIR") or "").strip()
    if not data_home:
        try:
            from core.runtime_paths import resolve_data_home
            data_home = str(resolve_data_home())
        except Exception:
            data_home = "data"
    db_path = Path(data_home) / "database" / "bot.db"
    if not db_path.is_file():
        return f"db schema: no bot.db yet (baselined at {head} on first boot)"
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT version FROM schema_versions").fetchall()
        finally:
            con.close()
    except Exception:
        rows = None
    if rows is None:
        return "db schema: not versioned yet (stamped at next boot)"
    if not rows:
        return "db schema: not versioned yet (stamped at next boot)"

    def _key(v: str):
        try:
            return tuple(int(p) for p in v.split("."))
        except Exception:
            return (0,)

    current = max((r[0] for r in rows), key=_key)
    if current == head:
        return f"db schema: {current} (up to date)"
    return (f"! db schema {current} behind code {head} — run "
            f"`python -m migrations.migrate upgrade`")


def setup_lines(env: dict) -> list[str]:
    """Onboarding-completeness view (informational, never-failing): avatar,
    surfaces, SOUL. The wallet line lives in doctor_report (Task 4). Pure over
    ``env`` for detection; each unset item carries its one-command remedy."""
    out: list[str] = []

    # data home (mirror schema_status_line's resolution)
    data_home = (env.get("POLYROB_DATA_DIR") or "").strip()
    if not data_home:
        try:
            from core.runtime_paths import resolve_data_home
            data_home = str(resolve_data_home())
        except Exception:
            data_home = "data"

    # avatar
    try:
        from core.instance import pfp_path, resolve_instance_id
        instance_id = resolve_instance_id(env)
        png = pfp_path(Path(data_home), instance_id)
        if png.is_file():
            out.append(f"avatar: generated ({png})")
        else:
            out.append("avatar: not generated (optional — `polyrob pfp generate` or /pfp)")
    except Exception:
        out.append("avatar: unknown")

    # surfaces (env-detectable configuration signals only).
    # `polyrob gateway` gates telegram/discord/slack on their *_SURFACE_ENABLED flag
    # (see cli/commands/gateway.py ~123-139/196/313/333) — a stale token alone does
    # NOT make the gateway start the surface. Their standalone commands
    # (`polyrob telegram`/`discord`/`slack`) DO run off the token alone (they
    # `os.environ.setdefault(..._SURFACE_ENABLED, "true")` before starting), so a
    # token-only reading is still actionable, just not via the gateway. The four
    # flag-only surfaces (email/signal/x-dm/whatsapp) have no separate token signal
    # to cross-check, so their semantics are unchanged.
    try:
        def _flag_on(v) -> bool:
            # Mirror core.env.bool_env/parse_bool's falsey-DENYlist semantics (any
            # value not in _FALSEY is truthy) — NOT an allow-list. An allow-list here
            # previously gave backward guidance: e.g. DISCORD_SURFACE_ENABLED=enabled
            # actually starts the surface (bool_env), but an allow-list read it as off.
            raw = str(v or "").strip().lower()
            return raw not in _FALSEY

        token_surfaces = (
            ("telegram", "TELEGRAM_SURFACE_ENABLED", "TELEGRAM_BOT_TOKEN"),
            ("discord", "DISCORD_SURFACE_ENABLED", "DISCORD_BOT_TOKEN"),
            ("slack", "SLACK_SURFACE_ENABLED", "SLACK_BOT_TOKEN"),
        )
        flag_only_surfaces = (
            ("email", "EMAIL_SURFACE_ENABLED"),
            ("signal", "SIGNAL_SURFACE_ENABLED"),
            ("x-dm", "X_SURFACE_ENABLED"),
            ("whatsapp", "WHATSAPP_SURFACE_ENABLED"),
        )
        configured: list[str] = []
        for name, flag_key, token_key in token_surfaces:
            flag_on = _flag_on(env.get(flag_key))
            has_token = bool((env.get(token_key) or "").strip())
            if flag_on and has_token:
                configured.append(name)
            elif flag_on:
                configured.append(f"{name} (enabled, token missing)")
            elif has_token:
                configured.append(f"{name} (token only — set {flag_key} to run via gateway)")
        for name, flag_key in flag_only_surfaces:
            if _flag_on(env.get(flag_key)):
                configured.append(name)
        if configured:
            out.append(f"surfaces: {', '.join(configured)}")
        else:
            out.append("surfaces: none configured (optional — see `polyrob gateway --help`)")
    except Exception:
        out.append("surfaces: unknown")

    # SOUL / identity docs
    try:
        base = Path(data_home) / "identity"
        present = [n for n in ("identity.md", "operating.md")
                   if (base / n).is_file() and (base / n).read_text().strip()]
        if present:
            out.append(f"identity docs: authored ({', '.join(present)})")
        else:
            out.append("identity docs: default (optional — author with `polyrob soul init`)")
    except Exception:
        out.append("identity docs: unknown")
    return out


def doctor_report(env: dict, local_absent_means_on: bool = True) -> list[str]:
    """Build the doctor report lines from an env mapping (pure, testable).

    ``local_absent_means_on`` — see :func:`local_flag_on`; the webview's System
    page passes False so its report reflects the server process, not the CLI.
    """
    resolved_env = env.get("CONFIG_ENV") or env.get("ENV") or "development"
    lines: list[str] = [f"POLYROB doctor — resolved env: {resolved_env}", "provider keys:"]
    present_providers = set(providers_with_keys(env))          # key present (any value)
    usable_providers = set(usable_providers_with_keys(env))    # value passes looks_like_real_key
    # The resolver gets only USABLE keys (env-var NAMES) so the reported provider/model
    # matches what `polyrob run` will actually accept — a malformed key must not resolve
    # a provider the LLM manager then rejects with a misleading "No API key found".
    usable_keys = {p.env_key for p in PROFILES.values() if p.name in usable_providers}
    for prof in PROFILES.values():
        if prof.name in usable_providers:
            status = "present"
        elif prof.name in present_providers:
            status = "present but malformed (too short/placeholder — will be rejected)"
        else:
            status = "missing"
        note = ""
        if not prof.initializable and prof.name in present_providers:
            note = " (not directly initializable — use OPENROUTER_API_KEY + deepseek/deepseek-chat)"
        lines.append(f"  {prof.name}: {status}{note}")
    if not present_providers:
        lines.append("  ! no provider API key found — run `polyrob init` or `polyrob config set`")
    elif not usable_providers:
        lines.append("  ! provider key(s) present but malformed — run `polyrob config set <KEY> <value>`")

    provider, model = resolve_provider_model(None, None, available_keys=usable_keys)
    lines.append(f"resolved provider/model: {provider} / {model or '(registry default)'}")

    # Owner/instance pairing (complements `polyrob init`): show who this instance
    # answers to and its instance id, plus the session-registry backend posture.
    try:
        from core.instance import resolve_instance_id, resolve_owner_principal
        _instance = resolve_instance_id(env)
        _owner = resolve_owner_principal(env, default_to_instance=False)
        lines.append(f"instance id: {_instance}")
        lines.append(f"owner: {_owner or '(unpaired — set POLYROB_OWNER_USER_ID or run `polyrob init`)'}")
    except Exception:
        pass
    _reg = (env.get("SESSION_REGISTRY_BACKEND") or "memory").strip().lower()
    lines.append(f"session registry backend: {_reg}"
                 + ("  (workers>1 needs sqlite + sticky routing)" if _reg == "memory" else ""))

    # T10: autonomy mode visibility (supervised/autonomous, and whether an
    # autonomous request actually took effect or clamped) — reads the LIVE
    # process env (autonomy_mode()/full_autonomy_enabled() are os.getenv-based,
    # not pure over `env`), same as every other autonomy-gated resolver in the
    # codebase; doctor is always invoked against the real process env.
    try:
        from agents.task.constants import autonomy_mode_display
        lines.append(f"autonomy mode: {autonomy_mode_display()}")
    except Exception:
        lines.append("autonomy mode: unknown")

    # The CLI (build_cli_container) does os.environ.setdefault("POLYROB_LOCAL", "1"),
    # so for run/chat an ABSENT value means ON — report that honestly (surfacing this
    # footgun is doctor's job). An explicitly-falsey value still reads off.
    rob_local = local_flag_on(env, absent_means_on=local_absent_means_on)
    lines.append(f"POLYROB_LOCAL: {'ON' if rob_local else 'off'}")
    if rob_local:
        lines.append("  ! POLYROB_LOCAL ON flips safe autonomy flags (self-wake/goals/"
                     "curator/skills-writable…) ON by default — intended for the "
                     "single-user CLI, NOT a multi-tenant server.")

    # Workspace-isolation invariant: the agent's writable workspace must NOT live
    # under the install/code tree (which also holds config/.env.* secrets). The
    # CLI local mode is the documented CWD-as-workspace exception (informational,
    # consistent with the POLYROB_LOCAL footgun note above) — not a failure.
    try:
        paths = resolve_runtime_paths(local=rob_local)
        if rob_local:
            lines.append(
                "workspace isolation: local CWD-as-workspace (consented Claude-Code-"
                "style behavior — NOT a confinement bug)"
            )
        else:
            ws_under_code = is_within_root(str(paths.workspace_root), str(paths.code_root))
            config_under_ws = is_within_root(str(paths.config_dir), str(paths.workspace_root))
            if not ws_under_code and not config_under_ws:
                lines.append("workspace isolation: OK")
            else:
                lines.append("! WORKSPACE UNDER CODE ROOT — secrets reachable")
    except Exception:
        lines.append("workspace isolation: unknown (could not resolve path roots)")

    # Sourced from the passed-in `env` dict, not os.environ, to keep
    # doctor_report pure/testable.
    lines.append(f"memory backend: {resolve_memory_backend(env, rob_local)}")

    # Wallet posture (setup section, part 1): off / on / MISCONFIGURED.
    _wallet_on = str(env.get("AGENT_WALLET_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")
    _seed_ok = len((env.get("AGENT_WALLET_MASTER_SEED") or "").strip()) >= 32
    if not _wallet_on:
        lines.append("wallet: off (optional — create one with `polyrob wallet init`)")
    elif _seed_ok:
        # H14c: don't report green "on" for a wallet that crashes on use. A >=32-char
        # junk seed passes the length check but fails bip44 derivation — actually
        # resolve the scheme and derive the treasury key, and surface network +
        # derivation so "on" is informative. resolve_scheme also raises on a corrupt
        # meta.json (H2), which is correctly reported as MISCONFIGURED here.
        _net = (env.get("AGENT_WALLET_NETWORK") or "testnet").strip().lower() or "testnet"
        try:
            from core.wallet import derivation as _deriv
            _scheme = _deriv.resolve_scheme(env=env)
            _deriv.derive_key((env.get("AGENT_WALLET_MASTER_SEED") or "").strip(), "treasury", _scheme)
            # H14c: report caps too, so "on" is informative — and flag "no daily
            # cap" as the real (unlimited) posture, mirroring the wallet view (M13).
            _max_tx = (env.get("AGENT_WALLET_MAX_PER_TX_USD") or "1000").strip() or "1000"
            _daily = (env.get("WALLET_DAILY_CAP_USD") or "").strip()
            _caps = f"caps max ${_max_tx}/tx · daily {('$' + _daily) if _daily else 'UNLIMITED'}"
            lines.append(f"wallet: on (network={_net}, derivation={_scheme}, {_caps}; "
                         "addresses: `polyrob wallet`, backup: `polyrob wallet export`)")
            if not _daily:
                lines.append("  ⚠ wallet has NO daily cap (unlimited sub-ceiling spend) — "
                             "set one with `polyrob wallet set-cap daily <usd>`")
        except Exception as e:
            lines.append(f"! wallet ENABLED but MISCONFIGURED: {e}")
    else:
        lines.append("! wallet ENABLED but AGENT_WALLET_MASTER_SEED missing/short — run `polyrob wallet init`")

    # sqlite-vec probe — never crash doctor on import/connection failure.
    try:
        from modules.memory.local_vector_memory_provider import _vec_available, vec_connect

        if _vec_available():
            try:
                con = vec_connect(":memory:")
                con.close()
                lines.append("sqlite-vec: loadable")
            except Exception:
                lines.append(
                    "sqlite-vec: NOT loadable (local_vector degrades to FTS5 — install apsw + sqlite-vec)"
                )
        else:
            lines.append(
                "sqlite-vec: NOT loadable (local_vector degrades to FTS5 — install apsw + sqlite-vec)"
            )
    except Exception:
        lines.append(
            "sqlite-vec: NOT loadable (local_vector degrades to FTS5 — install apsw + sqlite-vec)"
        )

    # Embedder presence — importlib probe, no model instantiated.
    has_embedder = importlib.util.find_spec("sentence_transformers") is not None
    lines.append(f"embedder: {'present' if has_embedder else 'absent'}")

    # Environment checks (3.3, 2026-07-14 review): python floor, [server] extra,
    # playwright browser, and DB-schema-vs-code — each a single honest line.
    lines.append(python_version_line())
    lines.append(server_extra_line())
    lines.append(playwright_line(env))
    lines.append(schema_status_line(env))
    lines.append(live_activity_line())

    # Skill-library compliance (Task 4) — strict agentskills.io frontmatter check,
    # same validator `polyrob skills validate` and CI's library-invariant test use.
    # Warn-only: a skill-authoring defect (or the check itself failing) must never
    # fail `doctor`.
    try:
        from agents.task.agent.skill_manager import get_skill_manager

        mgr = get_skill_manager()
        bad = mgr.validate_all_authored()
        total = mgr.count_authored_skills()
        compliant = total - len(bad)
        lines.append(f"skills: {compliant} compliant / {len(bad)} with issues (of {total})")
    except Exception:
        lines.append("skills: unknown (compliance check failed)")

    # Setup completeness (avatar / surfaces / SOUL) — informational, never-failing.
    lines.extend(setup_lines(env))

    return lines


def flags_report(env: dict, local_absent_means_on: bool = True) -> list[str]:
    """Resolved env-flag registry dump (SA-05), grouped, secrets masked.

    Pure over ``env`` for explicit values; posture/local-derived DEFAULTS come
    from the process env via ``core.config_policy.flag_defaults`` (the same resolvers
    the runtime uses). ``local_absent_means_on`` mirrors :func:`doctor_report`'s
    parameter so BOTH halves of one ``doctor`` invocation tell the same story:
    the CLI setdefaults ``POLYROB_LOCAL=1`` at container build, so for the CLI
    context an ABSENT value resolves the local-derived defaults as ON. A server
    caller (e.g. the webview System page) passes False.
    """
    from core.config_policy.flag_defaults import dynamic_flag_default
    from core.flags import resolve_all

    resolve_as_local = (
        local_absent_means_on
        and env.get("POLYROB_LOCAL") is None
        and env.get("ROB_LOCAL") is None
    )
    lines: list[str] = [f"POLYROB doctor — resolved flags ({len(env)} env vars set)"]
    if resolve_as_local:
        lines.append("(CLI context: POLYROB_LOCAL absent resolves as ON — "
                     "build_cli_container setdefaults it; see the POLYROB_LOCAL footgun)")
        # flag_defaults reads the PROCESS env; align it with the CLI resolution
        # for the duration of this report, then restore.
        os.environ["POLYROB_LOCAL"] = "1"
    try:
        current_group = None
        for r in resolve_all(env, dynamic_default=dynamic_flag_default):
            if r.group != current_group:
                current_group = r.group
                lines.append(f"## {current_group}")
            lines.append(f"  {r.name} = {r.value}  [{r.source}]")
    finally:
        if resolve_as_local:
            os.environ.pop("POLYROB_LOCAL", None)
    return lines


@click.command("doctor")
@click.option("--flags", "show_flags", is_flag=True,
              help="Dump every registered env flag with its resolved value and source.")
def doctor(show_flags: bool):
    """Show resolved providers/model, memory backend, and config footguns."""
    # Load env the same way the REPL does (./.polyrob, ~/.polyrob, root .env, config/.env.*
    # + the local-mode key backfill) so doctor reports the keys `rob` actually sees,
    # not just the bare process environment.
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)
    report = flags_report(dict(os.environ)) if show_flags else doctor_report(dict(os.environ))
    for line in report:
        click.echo(line)
