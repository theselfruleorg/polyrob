"""`polyrob init` — file-first onboarding for single-user mode (R5 + Task-11).

Writes the global ~/.polyrob/.env (chmod 600), creates the project ./.polyrob/sessions/,
appends .polyrob/ to .gitignore, and folds any legacy ~/.polyrob/cli.json into the env
file first.  No database, no TOML — files only.

Wizard sections (interactive mode):
  (a) Provider keys (Anthropic + OpenAI)
  (b) Default model
  (c) Toolset pick (from TOOLSETS)
  (d) Template / persona pick (from TEMPLATES)
  (e) Autonomy hint (POLYROB_LOCAL)

Flags:
  --quick           Only sections (a)+(b) — keys + model, skip toolset/template.
  --non-interactive Alias of --no-prompt; byte-identical behaviour.
  --template NAME   Pre-fill toolset + persona from a built-in template.
"""
from __future__ import annotations
from pathlib import Path

import click

import core.paths as _core_paths

from cli.config_store import migrate_to_dotenv


def _prompt_provider_keys(collected_keys: dict) -> None:
    """Prompt for each provider key in PROFILES order (OpenRouter first); populate
    ``collected_keys`` in place. Shared by ``polyrob init`` and ``run_quick_key_setup``
    so there is ONE prompt implementation (identical order/count)."""
    from modules.llm.profiles import all_profiles
    for profile in all_profiles():  # PROFILES order → OpenRouter first
        if profile.env_key in collected_keys:
            continue
        hint = f" ({profile.signup_url})" if profile.signup_url else ""
        entered = click.prompt(
            f"{profile.display_name} API key{hint} (blank to skip)",
            default="", show_default=False)
        if entered:
            collected_keys[profile.env_key] = entered


def run_quick_key_setup() -> bool:
    """Inline OpenRouter-first key wizard used by the preflight guard.

    Prompts for provider keys, applies them to ``os.environ`` IN-PROCESS (authoritative,
    so the very next ``build_cli_container`` sees them regardless of env re-layer order),
    then persists to ``~/.polyrob/.env`` (chmod 600). Returns True iff a *usable*
    (initializable) key is now present. Never raises on a write failure.
    """
    import os as _os

    import core.paths as _core_paths
    from modules.llm.profiles import usable_providers_with_keys

    click.echo("\n=== Set up an LLM provider key ===")
    click.echo("Recommended: OpenRouter — one key, access to every model, auto-failover.")
    collected: dict = {}
    _prompt_provider_keys(collected)

    # Apply in-process first (authoritative), then persist for next time.
    for key, value in collected.items():
        if value:
            _os.environ[key] = value
    if collected:
        try:
            home_env = _core_paths.polyrob_home() / ".env"
            _write_env(home_env, collected)
            click.echo(f"Saved provider key(s) to {home_env}")
        except OSError as exc:
            click.echo(f"Warning: could not write key file: {exc}", err=True)

    return bool(usable_providers_with_keys(dict(_os.environ)))


def _write_env(env_path: Path, updates: dict) -> None:
    """Upsert KEY=VALUE lines into env_path, then lock it to 0600."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    index = {ln.split("=", 1)[0]: i for i, ln in enumerate(lines) if "=" in ln}
    for key, value in updates.items():
        if not value:
            continue
        line = f"{key}={value}"
        if key in index:
            lines[index[key]] = line
        else:
            lines.append(line)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


@click.command("init")
@click.option("--anthropic-key", default=None, help="Anthropic API key")
@click.option("--openai-key", default=None, help="OpenAI API key")
@click.option("--default-model", default=None, help="Default model for `polyrob run`")
@click.option("--default-provider", default=None, help="Default provider")
@click.option("--no-prompt", is_flag=True, default=False,
              help="Skip interactive prompts (scripts/tests)")
@click.option("--non-interactive", is_flag=True, default=False,
              help="Alias of --no-prompt; byte-identical behaviour.")
@click.option("--quick", is_flag=True, default=False,
              help="Prompt only for keys + model; skip toolset/template sections.")
@click.option("--template", "template_name", default=None,
              help="Pre-fill toolset and persona from a named template (e.g. research, coding).")
@click.option("--toolset", "toolset_name", default=None,
              help="Toolset to activate (e.g. research, coding, full).")
def init_cmd(
    anthropic_key,
    openai_key,
    default_model,
    default_provider,
    no_prompt,
    non_interactive,
    quick,
    template_name,
    toolset_name,
):
    """Initialize POLYROB for this project (file-first: ~/.polyrob + ./.polyrob)."""
    # --non-interactive is a true alias of --no-prompt.
    no_prompt = no_prompt or non_interactive

    # Fold any legacy ~/.polyrob/cli.json into ~/.polyrob/.env first.
    migrate_to_dotenv()

    # Resolve template defaults (overridable by explicit flags).
    from agents.task.templates import resolve_template
    from agents.task.tool_defaults import TOOLSETS

    template = resolve_template(template_name) if template_name else None
    # toolset: explicit flag > template default > None
    effective_toolset = toolset_name or (template.toolset if template else None)
    # persona: template provides it; no explicit flag for persona
    effective_persona = template.name if template else None

    from modules.llm.profiles import usable_providers_with_keys

    # Pre-fill collected_keys from explicit flags (back-compat for --anthropic-key/--openai-key).
    collected_keys: dict[str, str] = {}
    if anthropic_key:
        collected_keys["ANTHROPIC_API_KEY"] = anthropic_key
    if openai_key:
        collected_keys["OPENAI_API_KEY"] = openai_key

    if not no_prompt:
        # ── Section (a): Provider keys (OpenRouter-first) ───────────────────
        # --quick is flag-driven: if key(s) were already supplied via flags, skip the
        # interactive provider sweep (quick = fast, non-nagging). The full interactive
        # wizard (non-quick) still prompts every provider.
        if not (quick and collected_keys):
            click.echo("\n=== Section 1/4: LLM provider keys ===")
            click.echo("Recommended: OpenRouter — one key, access to every model, auto-failover.")
            _prompt_provider_keys(collected_keys)

        # ── Section (b): Default model ───────────────────────────────────────
        click.echo("\n=== Section 2/4: Default model ===")
        default_model = default_model or click.prompt(
            "Default model (blank to skip)", default="", show_default=False)

        if not quick:
            # ── Section (c): Toolset ─────────────────────────────────────────
            click.echo("\n=== Section 3/4: Toolset ===")
            toolset_choices = list(TOOLSETS.keys())
            click.echo(f"Available toolsets: {', '.join(toolset_choices)}")
            default_ts = effective_toolset or "default"
            chosen_toolset = click.prompt(
                "Toolset", default=default_ts, show_default=True)
            if chosen_toolset in TOOLSETS:
                effective_toolset = chosen_toolset
            else:
                click.echo(f"Unknown toolset '{chosen_toolset}', using 'default'.")
                effective_toolset = "default"

            # ── Section (d): Template / persona ──────────────────────────────
            click.echo("\n=== Section 4/4: Template / persona ===")
            from agents.task.templates import TEMPLATES
            template_choices = list(TEMPLATES.keys())
            click.echo(f"Available templates: {', '.join(template_choices)}")
            default_tpl = effective_persona or "general"
            chosen_template = click.prompt(
                "Template", default=default_tpl, show_default=True)
            resolved = resolve_template(chosen_template)
            effective_persona = resolved.name
            # If user didn't already pick a toolset explicitly, let the template drive it.
            if not toolset_name and not (not quick):
                # (already set above — this branch is not reachable but kept for clarity)
                pass

            # ── Section (e): Autonomy hint ────────────────────────────────────
            click.echo(
                "\nTip: set POLYROB_LOCAL=1 in ~/.polyrob/.env to enable local autonomy features "
                "(writable skills, background review, goal board, etc.)."
            )

    # Pair the chosen model with its provider so the operator pin actually takes
    # effect. `core.runtime_config.resolve_runtime_config` returns the pin only when
    # `pinned_provider` is truthy (`if pinned_provider: return ...`), so a model-only
    # pin (DEFAULT_MODEL set, DEFAULT_PROVIDER empty) is silently DROPPED — the
    # wizard's model choice would never take effect on `polyrob run`/`chat`.
    # Prefer inference (no extra UX); fall back to a prompt only when the model's
    # provider can't be inferred (unknown/custom) and we're interactive.
    if default_model and not default_provider:
        from cli.config_store import _provider_for_model
        default_provider = _provider_for_model(default_model)
        # Unknown/custom model → can't infer. Prompt only in the full interactive
        # wizard (--quick is deliberately non-nagging; scripted/--no-prompt has no
        # stdin). Otherwise leave DEFAULT_PROVIDER empty as before — no regression.
        if not default_provider and not no_prompt and not quick:
            default_provider = click.prompt(
                f"Provider for model '{default_model}' (blank to skip)",
                default="", show_default=False) or None

    # Persist into ~/.polyrob/.env
    home_env = _core_paths.polyrob_home() / ".env"
    try:
        updates = {
            "DEFAULT_MODEL": default_model,
            "DEFAULT_PROVIDER": default_provider,
            "POLYROB_AGENT_TOOLSET": effective_toolset,
            "POLYROB_PERSONA": effective_persona,
        }
        updates.update(collected_keys)
        _write_env(home_env, updates)
    except OSError as exc:
        click.echo(f"Warning: could not write {home_env}: {exc}", err=True)

    sessions = Path.cwd() / ".polyrob" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)

    from cli.gitignore import ensure_polyrob_gitignored
    ensure_polyrob_gitignored(Path.cwd(), require_git_repo=False)

    click.echo(f"Initialized POLYROB. Global config: {home_env}")
    click.echo(f"Project sessions: {sessions}")
    try:
        env_text = home_env.read_text()
        env_from_file = {
            k.strip(): v.strip()
            for k, v in (line.split("=", 1) for line in env_text.splitlines() if "=" in line)
        }
    except OSError:
        env_from_file = {}
    if not usable_providers_with_keys(env=env_from_file):
        click.echo("⚠ No usable LLM API key set — add one with "
                   "`polyrob config set OPENROUTER_API_KEY <key> --global` "
                   "(for DeepSeek models use OPENROUTER_API_KEY + model deepseek/deepseek-chat)")
