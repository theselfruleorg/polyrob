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
from modules.llm.profiles import PROFILES, credential_status


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
    """One default with backend_factory via the policy SSOT (027 rider).
    An explicit MEMORY_BACKEND always wins."""
    from core.config_policy.policy import memory_backend_default
    default = memory_backend_default(rob_local)
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


def a2a_v1_line(env: dict) -> str:
    """A2A agent-card + OpenAI-compat `/v1` reachability (043 A31).

    Both surfaces live ONLY on the API server (`polyrob serve` / `python main.py`),
    never this CLI/telegram process — so doctor reports the CONFIGURED state over
    ``env``, not a live network probe, and never claims a bare "reachable". The A2A
    agent card mounts unconditionally when the API runs (no flag); the OpenAI-compat
    `/v1` router is gated by ``OPENAI_COMPAT_API_ENABLED`` — flag-off means it is NOT
    served. Without the ``[server]`` extra the API app cannot build at all, so both
    surfaces are honestly "absent". The path names are always shown either way.
    """
    v1_on = str(env.get("OPENAI_COMPAT_API_ENABLED", "")).strip().lower() \
        in ("1", "true", "yes", "on")
    server_missing = [m for m in ("fastapi", "uvicorn")
                      if importlib.util.find_spec(m) is None]
    if server_missing:
        gone = ("absent (needs the [server] extra: " + ", ".join(server_missing)
                + " missing — `pip install 'polyrob[server]'`)")
        a2a_state = v1_state = gone
    else:
        a2a_state = "mounted when the API server runs"
        # Honest: flag-off is reported as off, never as reachable/enabled.
        v1_state = ("enabled when serving" if v1_on
                    else "off (OPENAI_COMPAT_API_ENABLED)")
    return ("machine API surfaces (served by the API server, not this process): "
            f"A2A agent card /.well-known/agent.json — {a2a_state}; "
            f"/v1 OpenAI-compat — {v1_state}")


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

    # persona / character (F13) — the active <identity> persona source. Nothing
    # reported this before, so a wrong pref, a dropped field or an absent
    # character file were all invisible at once.
    try:
        from cli.persona import active_persona_line, cli_gate_on
        out.append(active_persona_line("local", data_home, gate=cli_gate_on(env)))
    except Exception:
        out.append("persona: unknown")

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


# --- credential source provenance (W1.3) --------------------------------------
# HANDOFF-env-system-and-key-subscriptions-2026-08-14 §1.2.3: a malformed
# placeholder in a higher env-file tier masked a real key one tier below, and
# nothing on the doctor line said WHERE either value lived. Each env-backed
# credential line now names its source file tier. Values are never printed.


def _display_env_path(path) -> str:
    """Shorten an env-file path for display: home → `~`, cwd → `.`."""
    s = str(path)
    for prefix, short in ((str(Path.home()), "~"), (str(Path.cwd()), ".")):
        if s.startswith(prefix + os.sep):
            return short + s[len(prefix):]
    return s


def env_file_layers(env: dict, local_mode: bool = True) -> list:
    """``(display_path, parsed_values)`` per EXISTING env file, precedence order.

    Parsed with dotenv for parity with ``core.bootstrap.load_env`` (quote
    handling — the naive ``read_env_file`` parser would misreport a quoted
    value as a process-env override). Read once per report.
    """
    from dotenv import dotenv_values

    from core.paths import env_file_candidates

    resolved = env.get("CONFIG_ENV") or env.get("ENV") or "development"
    out = []
    for cand in env_file_candidates(resolved, local_mode=local_mode):
        try:
            if cand.path.exists():
                out.append((_display_env_path(cand.path), dotenv_values(str(cand.path))))
        except Exception:
            continue
    return out


def credential_source_tier(var_name, env: dict, layers: list):
    """Name the tier that supplies *var_name*'s effective value (None if unset).

    load_env loads the layers highest-precedence-first with ``override=False``,
    so the FIRST layer defining the var is the file source — unless its value
    differs from the effective one, which means the process env preempted it.
    """
    if not var_name:
        return None
    value = str(env.get(var_name) or "").strip()
    if not value:
        return None
    for display, values in layers:
        raw = values.get(var_name)
        if raw is None or not str(raw).strip():
            continue
        if str(raw).strip() == value:
            return display
        return f"process env (overrides {display})"
    return "process env"


def _fmt_expiry(epoch: float) -> str:
    """Relative expiry for a store-backed credential ("in 4h", "EXPIRED").

    Relative, not absolute: what an owner needs from this line is whether a
    reconnect is imminent, and a wall-clock timestamp makes them do the
    subtraction (in whatever timezone the box happens to be in).
    """
    import time
    delta = float(epoch) - time.time()
    if delta <= 0:
        return "EXPIRED"
    if delta < 3600:
        return f"in {int(delta // 60)}m"
    if delta < 86400:
        return f"in {int(delta // 3600)}h"
    return f"in {int(delta // 86400)}d"


def _credential_alert_lines_from(status_by_provider: dict, present_providers: set,
                                 usable_providers: set) -> list[str]:
    """The `!` provider-credential remedy line(s) — pure formatting over
    already-computed :func:`credential_status` sets (043 A13 fix round 1).
    Extracted so ``doctor_report``'s transcript and the default leading-
    snapshot view (:func:`credential_alert_lines`) print the EXACT same
    words rather than two independently-maintained copies. Empty when at
    least one provider credential is usable."""
    if not present_providers and not usable_providers:
        # Keep the words people actually search for ("no provider API key") in
        # the line, even though a credential need not be an API key any more.
        return ["  ! no provider credential found (no API key, no connected "
                "account) — run `polyrob init` or `polyrob config set <KEY>`"]
    if not usable_providers:
        # Say WHICH problem: "malformed" and "expired" and "quota exhausted" send
        # the owner to three different fixes.
        why = "; ".join(sorted({s.reason for s in status_by_provider.values()
                                if s.present and s.reason}))
        return [f"  ! no usable provider credential — {why}"]
    return []


def credential_alert_lines(env: dict) -> list[str]:
    """:func:`_credential_alert_lines_from`, computing its inputs fresh via
    :func:`credential_status` (043 A13 fix round 1).

    The default (non ``--full``) ``doctor`` view and the REPL ``/doctor``
    verb call this to surface the SAME provider-credential remedy the full
    transcript prints, for a case the leading status snapshot's own health
    block cannot see: ``core/status_snapshot.py``'s ``_providers_section``
    only raises a CRIT when ``live is None and usable`` (usable credentials
    exist but none is live — all credit-dead); a box with ZERO provider
    credentials at all has an empty ``usable`` set, so that branch never
    fires and the snapshot alone reads a clean "Health: OK" lie."""
    status_by_provider = credential_status(env)
    present_providers = {n for n, s in status_by_provider.items() if s.present}
    usable_providers = {n for n, s in status_by_provider.items() if s.usable}
    return _credential_alert_lines_from(status_by_provider, present_providers, usable_providers)


def doctor_report(env: dict, local_absent_means_on: bool = True) -> list[str]:
    """Build the doctor report lines from an env mapping (pure, testable).

    ``local_absent_means_on`` — see :func:`local_flag_on`; the webview's System
    page passes False so its report reflects the server process, not the CLI.
    """
    resolved_env = env.get("CONFIG_ENV") or env.get("ENV") or "development"
    lines: list[str] = [f"POLYROB doctor — resolved env: {resolved_env}",
                        "provider credentials:"]
    # The CLI (build_cli_container) does os.environ.setdefault("POLYROB_LOCAL", "1"),
    # so for run/chat an ABSENT value means ON. Resolved once here — the credential
    # provenance walk, the config-file line, and the footgun note all read it.
    rob_local = local_flag_on(env, absent_means_on=local_absent_means_on)
    try:
        _env_layers = env_file_layers(env, local_mode=rob_local)
    except Exception:
        _env_layers = []
    # 024 L1.5: one pass over the credential oracle instead of two key-only set
    # lookups, so a store-backed credential (an OAuth seat, a borrowed login) is
    # VISIBLE here rather than reported as "missing" while it serves requests.
    # With the store off this renders exactly what the key oracles rendered.
    status_by_provider = credential_status(env)
    present_providers = {n for n, s in status_by_provider.items() if s.present}
    usable_providers = {n for n, s in status_by_provider.items() if s.usable}
    # The resolver gets only USABLE keys (env-var NAMES) so the reported provider/model
    # matches what `polyrob run` will actually accept — a malformed key must not resolve
    # a provider the LLM manager then rejects with a misleading "No API key found".
    usable_keys = {p.env_key for p in PROFILES.values()
                   if p.name in usable_providers and p.env_key}
    # Only providers you have SOMETHING for get a line. With 30+ rows in the
    # table, printing one line each buried the two that mattered under thirty
    # "missing" — the unconfigured ones are summarised on a single line below.
    unconfigured = []
    for prof in PROFILES.values():
        st = status_by_provider[prof.name]
        if not st.present and not (not prof.env_key and prof.auth_type == "none"):
            unconfigured.append(prof.name)
            continue
        if not prof.env_key and prof.auth_type == "none":
            # keyless-by-design (a providers.yaml auth_type:none row) — nothing
            # is "missing"; the endpoint needs no credential at all.
            lines.append(f"  {prof.name}: no key needed (keyless local endpoint)")
            continue
        # Which var actually supplies the value (a chain can have aliases), and
        # which file tier that value came from (W1.3 provenance).
        var_name = next((n for n in prof.env_key_chain()
                         if str(env.get(n) or "").strip()), None)
        tier = (credential_source_tier(var_name, env, _env_layers)
                if st.source == "env" else None)
        if st.usable:
            status = "present"
            if tier:
                # Name the file tier the value comes from — the §1.2.3 shadowing
                # class is invisible without it.
                status += f" ({tier})"
            # Name the SOURCE whenever it isn't the plain env key: "present" for
            # a connected account and for an env var are different facts, and
            # the difference is what you need when one of them stops working.
            if st.source not in ("env", "none"):
                status += f" ({st.source})"
            if st.expires_at:
                status += f", expires {_fmt_expiry(st.expires_at)}"
        elif st.present:
            where = f" ({tier})" if tier else ""
            status = f"present but unusable{where} — {st.reason}"
            if "malformed" in st.reason and st.source == "env" and prof.env_key:
                # Diagnosis without a verb strands the user in hand-editing the
                # env file — name the fix on the line itself, with the scope
                # that will actually hit the file the value lives in.
                target = var_name or prof.env_key
                if tier == "~/.polyrob/.env":
                    status += f"; clear it: polyrob config unset {target} --global"
                elif tier is None or tier == "./.polyrob/.env" \
                        or tier.startswith("process env"):
                    status += f"; clear it: polyrob config unset {target}"
                else:
                    # A tier `config unset` does not manage (root .env,
                    # config/.env.*) — the honest remedy is the file itself.
                    status += f"; remove it from {tier}"
        else:
            status = "missing"
        note = ""
        if not prof.initializable and st.usable:
            # Only when the reason line ISN'T already saying it — with a key
            # present, `st.reason` carries this same remedy and printing both
            # reads like two different problems.
            note = " (not directly initializable — use OPENROUTER_API_KEY + deepseek/deepseek-chat)"
        if prof.subscription and st.present:
            # A flat-rate tag on a provider you haven't configured is noise;
            # on one you HAVE, it explains why its cost shows as $0.
            note += " [subscription — not metered per token]"
        lines.append(f"  {prof.name}: {status}{note}")
    if unconfigured:
        shown = ", ".join(unconfigured[:6])
        rest = f" (+{len(unconfigured) - 6} more)" if len(unconfigured) > 6 else ""
        lines.append(f"  not configured: {shown}{rest} — connect: "
                     "`polyrob auth add <name>` · list: `polyrob model list`")
    lines.extend(_credential_alert_lines_from(status_by_provider, present_providers, usable_providers))

    # providers.yaml load state (queryable, not just a transient load-time log
    # line — UX assessment 2026-08-07, Q7). Shown only when a file exists.
    try:
        from modules.llm.provider_spec import user_providers_report
        rep = user_providers_report()
        if rep:
            loaded = rep.get("loaded") or []
            rejected = rep.get("rejected") or []
            lines.append(
                f"providers file: {rep.get('path')} — "
                f"{len(loaded)} loaded, {len(rejected)} rejected"
            )
            for row_name, reason in rejected:
                lines.append(f"  ! {row_name}: {reason} — row skipped")
            if rep.get("file_error"):
                lines.append(f"  ! file: {rep['file_error']}")
    except Exception as e:
        # A read we could not perform is not the same as "no providers.yaml".
        lines.append(f"providers file: unavailable ({type(e).__name__}: {e})")

    if usable_providers:
        provider, model = resolve_provider_model(None, None, available_keys=usable_keys)
        lines.append(f"resolved provider/model: {provider} / {model or '(registry default)'}")
    else:
        # No usable credential → don't display the last-resort provider as if it
        # would serve (the old line said "gemini" on a zero-key box).
        lines.append("resolved provider/model: (none — no usable provider yet)")

    # Owner/instance pairing (complements `polyrob init`): show who this instance
    # answers to and its instance id, plus the session-registry backend posture.
    _owner = None
    try:
        from core.instance import resolve_instance_id, resolve_owner_principal
        _instance = resolve_instance_id(env)
        _owner = resolve_owner_principal(env, default_to_instance=False)
        lines.append(f"instance id: {_instance}")
        lines.append(f"owner: {_owner or '(unpaired — set POLYROB_OWNER_USER_ID or run `polyrob init`)'}")
    except Exception as e:
        # Losing BOTH lines silently reads as an instance with no identity; say so.
        lines.append(f"instance id / owner: unavailable ({type(e).__name__}: {e})")

    # 2026-08-28 status SSOT: the SAME health block Telegram /status, the
    # webview /system page and the agent's agent_status render — credit
    # sentinel, live provider, open asks, blocked goals, suppressed owner
    # notices, dead loops. A section the snapshot cannot read renders as
    # `unavailable (<reason>)`; a builder failure renders as its own line —
    # never silence. Tenant = the bound owner (the instance's one principal).
    lines.extend(health_lines(_owner))
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

    # 030 WS-E5: the effective-posture card — the SAME card /autonomy, the
    # webview /system page and Telegram /status render, so every seat gives one
    # answer to "what is this instance allowed to do right now, and why".
    try:
        from core.config_policy.posture_card import render_posture_card
        lines.append("posture:")
        lines.extend(render_posture_card(prefix="  "))
    except Exception:
        lines.append("posture: unavailable")

    # 027 rider: an env/file value for an import-frozen policy flag that the
    # process never re-read is INERT — this used to be visible only in
    # `doctor --flags`, so the plain report silently implied it works.
    try:
        _frozen, _frozen_errs = _frozen_flag_truth()
        for name, frozen in _frozen.items():
            raw = env.get(name)
            if raw is not None and not _frozen_values_agree(raw, frozen):
                lines.append(
                    f"! {name}: env value {raw!r} is INERT — frozen at import "
                    f"as {frozen!r}; restart to apply")
        for _err in _frozen_errs:
            lines.append(f"! frozen-flag check: unavailable ({_err}) — an INERT "
                         "env value for those flags cannot be detected")
    except Exception as e:
        lines.append(f"! frozen-flag check: unavailable ({type(e).__name__}: {e}) "
                     "— an INERT env value cannot be detected")

    # 0.9.0: the AUTONOMY_ENABLED master switch — OFF by default for new local
    # installs. Say so plainly + what autonomy would add, so a new user knows the
    # agent is interactive-only until they opt in (live process env, like the mode).
    try:
        from agents.task.constants import autonomy_enabled as _auton
        _on = _auton()
        lines.append(f"autonomy: {'ON' if _on else 'OFF'} (AUTONOMY_ENABLED)")
        if not _on:
            lines.append("  self-directed loops (self-wake / goals + planner / curator / "
                         "background-review / self-editing) are OFF — the agent acts only "
                         "on your messages. Enable: AUTONOMY_ENABLED=true (or `polyrob init`).")
    except Exception:
        lines.append("autonomy: unknown (AUTONOMY_ENABLED)")

    # Active profile (multi-instance W4) — the first thing to check when the
    # agent's identity looks wrong.
    try:
        from core.profiles import resolve_active_profile
        _sel = resolve_active_profile()
        if _sel is not None:
            lines.append(f"profile: {_sel.name} (via {_sel.source}) at {_sel.home}")
        else:
            lines.append("profile: none (legacy/project mode)")
    except Exception:
        lines.append("profile: unknown")

    # Where this instance's data + config live — new-user orientation ("what's
    # running and where"). Data home mirrors schema_status_line's resolution.
    _data_home = (env.get("POLYROB_DATA_DIR") or "").strip()
    if not _data_home:
        try:
            from core.runtime_paths import resolve_data_home
            _data_home = str(resolve_data_home())
        except Exception:
            _data_home = "data"
    lines.append(f"data dir: {_data_home}")
    try:
        from core.paths import env_file_candidates
        _cfg = next((str(c.path) for c in env_file_candidates(local_mode=rob_local)
                     if c.path.exists()), None)
        lines.append(f"config file: {_cfg or '(none found — using process env / defaults)'}")
    except Exception as e:
        lines.append(f"config file: unavailable ({type(e).__name__}: {e})")

    # An ABSENT POLYROB_LOCAL means ON for run/chat (see rob_local above) —
    # report that honestly (surfacing this footgun is doctor's job). An
    # explicitly-falsey value still reads off.
    lines.append(f"POLYROB_LOCAL: {'ON' if rob_local else 'off'}")
    if rob_local:
        lines.append("  ! POLYROB_LOCAL ON flips the INTERACTIVE tools (coding/git/KB/"
                     "RAG/project-context…) ON by default — intended for the single-user "
                     "CLI, NOT a multi-tenant server. The self-directed autonomy loops are "
                     "separate: they need AUTONOMY_ENABLED (see the `autonomy:` line above).")

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
            # H3 (2026-08-22): delegate to the SAME parsers load_wallet_config()
            # uses (never a second, divergent "1000"/blank-means-unlimited
            # display parser) — this also means a malformed cap correctly falls
            # into the `except Exception` MISCONFIGURED branch below, matching
            # what actually happens when the wallet is used.
            from core.wallet.config import _cap_float, _req_float, DEFAULT_DAILY_CAP_USD, DEFAULT_MAX_PER_TX_USD
            _max_tx_val = _req_float(env, "AGENT_WALLET_MAX_PER_TX_USD", DEFAULT_MAX_PER_TX_USD)
            _daily_val = _cap_float(env, "WALLET_DAILY_CAP_USD", DEFAULT_DAILY_CAP_USD)
            _daily = f"${_daily_val:.2f}" if _daily_val is not None else ""
            _caps = f"caps max ${_max_tx_val:.2f}/tx · daily {_daily if _daily else 'UNLIMITED'}"
            lines.append(f"wallet: on (network={_net}, derivation={_scheme}, {_caps}; "
                         "addresses: `polyrob wallet`, backup: `polyrob wallet export`)")
            if not _daily:
                lines.append("  ⚠ wallet has NO daily cap (unlimited sub-ceiling spend) — "
                             "set one with `polyrob wallet set-cap daily <usd>`")
        except Exception as e:
            lines.append(f"! wallet ENABLED but MISCONFIGURED: {e}")
    else:
        # S1 (2026-09-14): the seed is split into `wallet.env`, loaded ONLY by
        # the agent unit. A console/email process legitimately has NO seed and
        # reads the public addresses from `wallet/public_identity.json` — that
        # is the intended posture, not a misconfiguration. Fail-open: any read
        # error falls through to the legacy remedy line.
        _pub = None
        try:
            from core.wallet.public_identity import read_public_identity
            _pub = read_public_identity()
        except Exception:
            _pub = None
        if _pub:
            lines.append("wallet: on, PUBLIC-ONLY in this process (no seed here; addresses from "
                         "wallet/public_identity.json; signing lives in the agent unit)")
        else:
            lines.append("! wallet ENABLED but AGENT_WALLET_MASTER_SEED missing/short — run `polyrob wallet init`")

    # x402 treasury source (W1.1, 2026-08-21): where invoice money lands and
    # WHY — explicit env, the wallet auto-fill, or nothing (with the remedy).
    # Pure over `env` (never touches the live wallet), mirroring the
    # resolve_treasury_address() precedence in modules/x402/x402_integration.py.
    _treasury_env = (env.get("X402_PAYMENT_RECIPIENT") or "").strip()
    # Parse EXACTLY as bool_env does (blank -> the default, not false), or a
    # present-but-empty X402_TREASURY_FROM_WALLET makes doctor report "none"
    # while invoicing happily resolves the wallet address.
    _raw_from_wallet = env.get("X402_TREASURY_FROM_WALLET")
    _from_wallet = (True if _raw_from_wallet is None or str(_raw_from_wallet).strip() == ""
                    else str(_raw_from_wallet).strip().lower() not in _FALSEY)
    if _treasury_env:
        lines.append(f"x402 treasury: {_treasury_env} (env X402_PAYMENT_RECIPIENT)")
    elif _wallet_on and _seed_ok and _from_wallet:
        lines.append("x402 treasury: agent wallet address (auto — "
                     "X402_TREASURY_FROM_WALLET; addresses: `polyrob wallet`)")
    else:
        lines.append("x402 treasury: none (invoicing refuses until set — set "
                     "X402_PAYMENT_RECIPIENT or enable the agent wallet)")

    # x402 Tier-2 endpoint state (§5.3, 2026-08-21): a machine-callable 402 URL
    # needs a domain + cert + the api service, which an agent can never
    # self-provision — so the absent case names the owner runbook instead of
    # leaving "we need an endpoint" as an unclosable ask. Tier-1 receive
    # (invoice + on-chain detect) needs none of this; say so in the same line.
    _x402_http_on = str(env.get("X402_ENABLED", "")).strip().lower() == "true"
    _base_url = (env.get("A2A_BASE_URL") or "").strip().rstrip("/")
    if _x402_http_on and _base_url:
        lines.append(f"x402 endpoint: {_base_url} (machine-callable 402 surface)")
    elif _x402_http_on:
        lines.append("x402 endpoint: enabled but no A2A_BASE_URL — the agent card "
                     "advertises a guessed host; set A2A_BASE_URL to the public URL")
    else:
        lines.append("x402 endpoint: not configured (optional — invoices are "
                     "unaffected; for a machine-callable 402 URL run "
                     "`X402_HOST=<host> bash scripts/setup_x402_endpoint.sh` as "
                     "root from a full repo checkout — the install tree has no "
                     "deployment/ templates)")

    # A2A agent-card + OpenAI-compat /v1 reachability (043 A31): both live on the
    # API server, never this process — report the configured state honestly (a
    # flag-off /v1 is never rendered as reachable).
    lines.append(a2a_v1_line(env))

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


def health_lines(owner: "str | None", *, prefix: str = "  ") -> list[str]:
    """The status-snapshot health block for the doctor report (pure over the
    resolved owner; the snapshot reads the data home read-only)."""
    try:
        from core.status_snapshot import build_status_snapshot
        from core.status_render import pause_headline, render_health_lines
        if not owner:
            return ["health: unavailable (no owner principal bound — the snapshot is "
                    "tenant-scoped; set POLYROB_OWNER_USER_ID)"]
        snap = build_status_snapshot(str(owner), include_money=False)
        # 031: the pause state leads every seat, before health (CLI verbs here).
        out = ([pause_headline(snap, resume_hint="`polyrob autonomy resume`",
                               pause_hint="`polyrob autonomy pause`")]
               + render_health_lines(snap, prefix=prefix))
        # the doctor report is lower-case; the health line is index 1 (the pause
        # line leads since 031)
        for _i, _ln in enumerate(out):
            if _ln.startswith("Health:"):
                out[_i] = _ln.replace("Health:", "health:", 1)
                break
        return out
    except Exception as e:
        return [f"health: unavailable ({type(e).__name__}: {str(e)[:120]})"]


#: The pointer line the default (non ``--full``) doctor view ends on — a
#: shared constant so `polyrob doctor` and the REPL `/doctor` verb
#: (``cli/ui/commands/h_diag.py::h_doctor``) print the identical words
#: (043 A13 fix round 1, Important 3).
DOCTOR_FULL_POINTER = "run `polyrob doctor --full` for every check"


def build_doctor_snapshot():
    """Build the ``StatusSnapshot`` that leads `polyrob doctor` / REPL
    `/doctor` output (043 A13) — the ONE build point, so rendering AND the
    credential-alert gate (:func:`status_snapshot_lines`) read the SAME
    snapshot rather than paying for (and risking disagreement between) two
    separate builds per invocation.

    Owner resolution mirrors ``autonomy status``'s ``resolve_identity()``
    (never None — an explicitly-bound owner, else the single-user local
    tenant ``"local"``) rather than the STRICT ``resolve_owner_principal``
    used for the transcript's separate "owner: (unpaired...)" diagnostic
    line, so a fresh, unpaired install still gets a real snapshot instead of
    an "unavailable" stub.

    Returns ``(snapshot, error_line)``: on success ``error_line`` is
    ``None``; on any failure ``snapshot`` is ``None`` and ``error_line`` is a
    ready-to-print ``"status: unavailable (...)"`` string. Fail-open by
    construction — `doctor` must never crash because the status SSOT
    couldn't be read.
    """
    try:
        from core.admin_data_home import AmbiguousDataHome, admin_data_home
        from core.identity import resolve_identity
        from core.status_snapshot import build_status_snapshot

        owner = resolve_identity()
        try:
            data_home = admin_data_home(
                echo=lambda m: click.echo(click.style(m, fg="yellow"), err=True))
        except AmbiguousDataHome as exc:
            return None, f"status: unavailable ({exc})"
        snap = build_status_snapshot(str(owner), data_dir=data_home, include_money=True)
        return snap, None
    except Exception as e:
        return None, f"status: unavailable ({type(e).__name__}: {str(e)[:120]})"


def status_snapshot_lines() -> list:
    """The full status-snapshot view (043 A13) that leads plain `polyrob doctor`
    output — the SAME builder/renderer `polyrob autonomy status` uses
    (``build_status_snapshot`` / ``render_status_lines``, see
    ``cli/commands/autonomy.py::status_cmd``), but with every section
    (``include_money=True``, which `autonomy status` deliberately omits) since
    `doctor` is the one seat meant to show the whole picture at a glance.

    043 A13 fix round 1 (Important 1): ``core/status_snapshot.py``'s own CRIT
    (``_providers_section``, ``if live is None and usable``) only fires when
    usable credentials exist but none is live (all credit-dead) — a box with
    ZERO provider credentials at all has an empty ``usable`` set, so that
    branch never fires and the snapshot alone renders a clean "Health: OK"
    lie. When the snapshot's own providers section shows no live AND no
    usable provider, this appends :func:`credential_alert_lines`' remedy —
    the exact words `doctor --full`'s transcript prints — so the default
    view never claims a clean bill of health over a box that cannot run at
    all. (The "some credential, none usable" and "credit-dead" cases are
    already surfaced by the snapshot's own health block, so this never
    duplicates those.)

    Both this function and the CLI/REPL callers using
    :data:`DOCTOR_FULL_POINTER` are the ONE shared view both `polyrob doctor`
    and the REPL `/doctor` verb render (Important 3) — imported, not copied.
    """
    snap, error_line = build_doctor_snapshot()
    if snap is None:
        return [error_line]
    from core.status_render import render_status_lines
    lines = render_status_lines(
        snap,
        resume_hint="`polyrob autonomy resume`",
        pause_hint="`polyrob autonomy pause`",
    )
    try:
        prov = snap.sections.get("providers")
        data = prov.data if (prov is not None and prov.available) else {}
    except Exception:
        data = {}
    if not data.get("live") and not data.get("usable"):
        lines.extend(credential_alert_lines(dict(os.environ)))
    return lines


def _frozen_flag_truth() -> tuple:
    """``(values, errors)`` for the import-frozen flags, from the SAME frozen
    accessors the runtime consults (026 P0.2). The report must print THESE as
    effective — an env/file value the process never re-read is inert, and saying
    `[env]` for it is a lie about the most security-critical flags.

    Fail-open per seam, but NEVER silent (2026-08-28 status SSOT): a seam that
    could not be probed comes back in *errors* so the caller can say the INERT
    check is blind for those flags, rather than reporting a clean bill of health
    it never actually checked."""
    out: dict = {}
    errors: list = []
    try:
        from core.config_policy.policy import (approval_grant_ttl_hours,
                                               compute_posture,
                                               payment_approval_mode,
                                               payment_approval_timeout_sec)
        out["AGENT_COMPUTE_POSTURE"] = compute_posture()
        out["PAYMENT_APPROVAL_MODE"] = payment_approval_mode()
        out["APPROVAL_TIMEOUT_SEC"] = payment_approval_timeout_sec()
        out["APPROVAL_GRANT_TTL_HOURS"] = approval_grant_ttl_hours()
    except Exception as e:
        errors.append(f"compute/payment posture: {type(e).__name__}: {e}")
    try:
        from tools.controller.approval import (frozen_approval_provider,
                                               frozen_approval_required_tools)
        out["APPROVAL_PROVIDER"] = frozen_approval_provider()
        out["APPROVAL_REQUIRED_TOOLS"] = ",".join(sorted(frozen_approval_required_tools()))
    except Exception as e:
        errors.append(f"approval provider/tools: {type(e).__name__}: {e}")
    return out, errors


def _frozen_values_agree(reported, frozen) -> bool:
    """Loose equality across the shapes the frozen flags take (int/float/str/
    comma-set) so `300` vs `300.0` or a reordered tool list never reads INERT."""
    a, b = str(reported).strip(), str(frozen).strip()
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    set_a = {p.strip() for p in a.split(",") if p.strip()}
    set_b = {p.strip() for p in b.split(",") if p.strip()}
    return set_a == set_b


_NO_FLAGS_MATCH = "no flags match the given --group/--search/--changed filters"


def _changed_from_default(r, dynamic_default) -> bool:
    """Whether a resolved flag's effective value differs from what it would
    resolve to with NO explicit env value (documented or dynamic default).
    Only an env-sourced row can be a change; a resolution error keeps the row
    visible rather than hiding it (fail-open toward showing)."""
    if r.source != "env":
        return False
    from core.flags import resolve_flag
    try:
        baseline = resolve_flag(r.name, {}, dynamic_default)
    except Exception:
        return True
    return r.value != baseline.value and str(r.value) != str(baseline.value)


def flags_report(env: dict, local_absent_means_on: bool = True, *,
                 group: str | None = None, search: str | None = None,
                 changed: bool = False) -> list[str]:
    """Resolved env-flag registry dump (SA-05), grouped, secrets masked.

    Pure over ``env`` for explicit values; posture/local-derived DEFAULTS come
    from the process env via ``core.config_policy.flag_defaults`` (the same resolvers
    the runtime uses). ``local_absent_means_on`` mirrors :func:`doctor_report`'s
    parameter so BOTH halves of one ``doctor`` invocation tell the same story:
    the CLI setdefaults ``POLYROB_LOCAL=1`` at container build, so for the CLI
    context an ABSENT value resolves the local-derived defaults as ON. A server
    caller (e.g. the webview System page) passes False.

    026 P0.2/P0.3: an import-frozen flag whose env value differs from the frozen
    effective value is printed as the FROZEN value with the env value marked
    INERT; the resolved autonomy mode (incl. clamp state) heads the report.

    030 C5 filters (combine as AND; group headers survive for matching rows,
    empty groups are skipped): ``group`` keeps only groups whose name contains
    the substring (case-insensitive); ``search`` keeps only flags whose NAME
    contains the substring (case-insensitive); ``changed`` keeps only flags
    whose effective value differs from their default, plus the frozen/INERT
    rows (always interesting). A filtered report with zero matching flags
    collapses to the one-line "no flags match" message.
    """
    from core.config_policy.flag_defaults import dynamic_flag_default
    from core.flags import resolve_all

    group_needle = group.lower() if group else None
    search_needle = search.lower() if search else None
    filtering = bool(group_needle or search_needle or changed)

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
        # P0.3: clamp visibility — `--flags` and plain doctor are mutually
        # exclusive views, so the mode/clamp line must live in both.
        try:
            from core.config_policy.policy import autonomy_mode_display
            lines.append(f"autonomy mode: {autonomy_mode_display()}")
        except Exception:
            # Matches the plain report's wording — the line never just vanishes.
            lines.append("autonomy mode: unknown")
        frozen_truth, frozen_errs = _frozen_flag_truth()
        for _err in frozen_errs:
            lines.append(f"! frozen-flag check: unavailable ({_err}) — an INERT "
                         "env value for those flags cannot be detected")
        emitted_group = None
        matched = 0
        for r in resolve_all(env, dynamic_default=dynamic_flag_default):
            if group_needle is not None and group_needle not in r.group.lower():
                continue
            if search_needle is not None and search_needle not in r.name.lower():
                continue
            frozen_inert = (r.name in frozen_truth and r.source == "env"
                            and not _frozen_values_agree(r.value, frozen_truth[r.name]))
            if (changed and not frozen_inert
                    and not _changed_from_default(r, dynamic_flag_default)):
                continue
            if r.group != emitted_group:
                emitted_group = r.group
                lines.append(f"## {r.group}")
            if frozen_inert:
                lines.append(
                    f"  {r.name} = {frozen_truth[r.name]}  "
                    f"[frozen at import — env value {r.value} INERT]")
            else:
                lines.append(f"  {r.name} = {r.value}  [{r.source}]")
            matched += 1
    finally:
        if resolve_as_local:
            os.environ.pop("POLYROB_LOCAL", None)
    if filtering and matched == 0:
        return [_NO_FLAGS_MATCH]
    return lines


@click.command("doctor")
@click.option("--flags", "show_flags", is_flag=True,
              help="Dump every registered env flag with its resolved value and source.")
@click.option("--group", "flag_group", metavar="SUBSTRING", default=None,
              help="Only flag groups whose name contains SUBSTRING "
                   "(case-insensitive). Implies --flags.")
@click.option("--search", "flag_search", metavar="SUBSTRING", default=None,
              help="Only flags whose name contains SUBSTRING "
                   "(case-insensitive). Implies --flags.")
@click.option("--changed", "flag_changed", is_flag=True,
              help="Only flags set away from their default, plus the "
                   "frozen/INERT ones. Implies --flags.")
@click.option("--full", "show_full", is_flag=True,
              help="Print the full check transcript after the status snapshot "
                   "(the whole pre-043 `doctor` report). No effect with --flags.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit as JSON ({\"report\": [lines], \"status\": [lines]}; "
                   "--flags mode emits {\"report\": [lines]} only).")
def doctor(show_flags: bool, flag_group: str | None, flag_search: str | None,
           flag_changed: bool, show_full: bool, as_json: bool):
    """Health snapshot first (pause state, ranked issues, every status
    section) — the same view `polyrob autonomy status` renders. The full
    check transcript (resolved providers/model, memory backend, config
    footguns) prints under --full."""
    # Load env the same way the REPL does (./.polyrob, ~/.polyrob, root .env, config/.env.*
    # + the local-mode key backfill) so doctor reports the keys `rob` actually sees,
    # not just the bare process environment.
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)
    show_flags = show_flags or bool(flag_group) or bool(flag_search) or flag_changed

    if show_flags:
        report = flags_report(dict(os.environ), group=flag_group,
                              search=flag_search, changed=flag_changed)
        if as_json:
            import json as _json
            click.echo(_json.dumps({"report": report}, indent=2))
            return
        for line in report:
            click.echo(line)
        return

    # 043 A13: plain `doctor` leads with the SAME status snapshot every other
    # seat renders (`polyrob autonomy status`, Telegram /status, the webview
    # /system page) — the full check transcript (`doctor_report`, its own
    # provider/DB/skill probes) is detail, not the headline, computed only
    # when actually needed: under --full, or folded into --json.
    status = status_snapshot_lines()
    if as_json:
        import json as _json
        report = doctor_report(dict(os.environ))
        click.echo(_json.dumps({"report": report, "status": status}, indent=2))
        return
    for line in status:
        click.echo(line)
    click.echo("")
    if show_full:
        for line in doctor_report(dict(os.environ)):
            click.echo(line)
    else:
        click.echo(DOCTOR_FULL_POINTER)
