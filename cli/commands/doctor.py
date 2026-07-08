"""`polyrob doctor` — read-only health + config legibility check.

Surfaces which provider keys are present (and whether they're actually usable), the
resolved provider/model, the active memory backend, and the POLYROB_LOCAL footgun (it
flips a group of safe autonomy flags ON). Pure ``doctor_report`` does the work so it
is testable without a live container.
"""
import importlib.util
import os

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

    return lines


def flags_report(env: dict, local_absent_means_on: bool = True) -> list[str]:
    """Resolved env-flag registry dump (SA-05), grouped, secrets masked.

    Pure over ``env`` for explicit values; posture/local-derived DEFAULTS come
    from the process env via ``agents.task.flag_defaults`` (the same resolvers
    the runtime uses). ``local_absent_means_on`` mirrors :func:`doctor_report`'s
    parameter so BOTH halves of one ``doctor`` invocation tell the same story:
    the CLI setdefaults ``POLYROB_LOCAL=1`` at container build, so for the CLI
    context an ABSENT value resolves the local-derived defaults as ON. A server
    caller (e.g. the webview System page) passes False.
    """
    from agents.task.flag_defaults import dynamic_flag_default
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
