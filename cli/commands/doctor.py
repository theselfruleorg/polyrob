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


def doctor_report(env: dict) -> list[str]:
    """Build the doctor report lines from an env mapping (pure, testable)."""
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

    # The CLI (build_cli_container) does os.environ.setdefault("POLYROB_LOCAL", "1"),
    # so for run/chat an ABSENT value means ON — report that honestly (surfacing this
    # footgun is doctor's job). An explicitly-falsey value still reads off.
    raw_local = env.get("POLYROB_LOCAL")
    rob_local = True if raw_local is None else (str(raw_local).strip().lower() not in _FALSEY)
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

    # Mirror modules.memory.backend_factory.maybe_register_memory_backend's default:
    # local_vector under POLYROB_LOCAL, else sqlite (an explicit MEMORY_BACKEND always
    # wins). Sourced from the passed-in `env` dict, not os.environ, to keep
    # doctor_report pure/testable.
    default_memory_backend = "local_vector" if rob_local else "sqlite"
    backend = env.get("MEMORY_BACKEND", default_memory_backend) or default_memory_backend
    lines.append(f"memory backend: {backend}")

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


@click.command("doctor")
def doctor():
    """Show resolved providers/model, memory backend, and config footguns."""
    # Load env the same way the REPL does (./.polyrob, ~/.polyrob, root .env, config/.env.*
    # + the local-mode key backfill) so doctor reports the keys `rob` actually sees,
    # not just the bare process environment.
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)
    for line in doctor_report(dict(os.environ)):
        click.echo(line)
