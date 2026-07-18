"""`polyrob init` — file-first onboarding for single-user mode (R5 + Task-11).

Writes the global ~/.polyrob/.env (chmod 600), creates the project ./.polyrob/sessions/,
appends .polyrob/ to .gitignore, and folds any legacy ~/.polyrob/cli.json into the env
file first.  No database, no TOML — files only.

Wizard sections (interactive mode):
  (a) Provider keys (Anthropic + OpenAI)
  (b) Default model
  (c) Toolset pick (from TOOLSETS)
  (d) Template / persona pick (from TEMPLATES)
  (e) Owner pairing (instance id + owner user id)
  (f) Autonomy & guardrails ("6/6") — local mode, approval preset, and daily
      digest channel; all blank-to-skip
  (g) Optional agent crypto wallet opt-in (default No; non-quick interactive only)

Flags:
  --quick           Only sections (a)+(b) — keys + model, skip toolset/template
                    (and therefore (e)/(f)/(g), which are nested inside (d)'s block).
  --non-interactive Alias of --no-prompt; byte-identical behaviour.
  --template NAME   Pre-fill toolset + persona from a built-in template.
  --skip-keys       (hidden) Skip section (a) — used by the run_quick_key_setup
                    bridge so a just-collected key isn't prompted for again.
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
        # O4 (2026-07-14 review): a non-initializable provider (e.g. DeepSeek)
        # can't bootstrap the agent alone — say so AT the prompt, not after the
        # user hits "No API key found" on their first run.
        if not profile.initializable:
            hint += " — can't bootstrap alone; pair with another provider (e.g. OpenRouter)"
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

    ok = bool(usable_providers_with_keys(dict(_os.environ)))
    if ok and collected:
        from cli.keys import _can_prompt
        if _can_prompt() and click.confirm(
                "Key saved. Finish full setup now (model, persona, autonomy — ~1 min)?",
                default=False):
            try:
                init_cmd.main(args=["--skip-keys"], standalone_mode=False)
            except click.Abort:
                # Ctrl-C / EOF inside the bridged wizard is a cancellation, not a
                # failure — don't scare the user with "failed (Aborted!)" (Finding 4,
                # 2026-07-14 final review).
                click.echo("full setup cancelled — run `polyrob init` anytime.")
            except Exception as exc:
                click.echo(f"full setup failed ({exc}) — run `polyrob init` anytime.", err=True)
        else:
            click.echo("Tip: run `polyrob init` anytime for full setup "
                       "(model, persona, autonomy).")
    return ok


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
@click.option("--owner", "owner_user_id", default=None,
              help="Owner user id to pair this instance to (defaults to the instance id).")
@click.option("--instance-id", "instance_id", default=None,
              help="Instance id for this deployment (default 'rob').")
@click.option("--skip-keys", is_flag=True, default=False, hidden=True,
              help="Skip the provider-key section (used by the inline key wizard bridge).")
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
    owner_user_id,
    instance_id,
    skip_keys,
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

    # Populated by Section 6/6 (Autonomy & guardrails), below; skipped entirely
    # (stays empty) under --quick / --no-prompt / --non-interactive.
    guardrail_updates: dict[str, str] = {}

    if not no_prompt:
        # ── Section (a): Provider keys (OpenRouter-first) ───────────────────
        # --quick is flag-driven: if key(s) were already supplied via flags, skip the
        # interactive provider sweep (quick = fast, non-nagging). The full interactive
        # wizard (non-quick) still prompts every provider.
        if not (quick and collected_keys) and not skip_keys:
            click.echo("\n=== Section 1/6: LLM provider keys ===")
            click.echo("Recommended: OpenRouter — one key, access to every model, auto-failover.")
            _prompt_provider_keys(collected_keys)

        # ── Section (b): Default model ───────────────────────────────────────
        click.echo("\n=== Section 2/6: Default model ===")
        default_model = default_model or click.prompt(
            "Default model (blank to skip)", default="", show_default=False)

        if not quick:
            # ── Section (c): Toolset ─────────────────────────────────────────
            click.echo("\n=== Section 3/6: Toolset ===")
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
            click.echo("\n=== Section 4/6: Template / persona ===")
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

            # ── Section (e): Owner pairing ────────────────────────────────────
            # Pair this instance to an owner id so autonomy/self-evolution surfaces
            # know who to answer to. Single-user local: the owner id and instance id
            # are typically the same (both default "rob"). Explicit flags win.
            click.echo("\n=== Section 5/6: Owner pairing ===")
            if instance_id is None:
                instance_id = click.prompt(
                    "Instance id", default="rob", show_default=True) or None
            if owner_user_id is None:
                owner_user_id = click.prompt(
                    "Owner user id (blank = same as instance id)",
                    default=(instance_id or "rob"), show_default=True) or None

            # ── Section 6/6: Autonomy & guardrails ───────────────────────────
            # All prompts blank-to-skip. Env keys land in ``guardrail_updates``
            # (merged into the ~/.polyrob/.env upsert below); the digest choice
            # writes a typed preference instead when an owner uid is known
            # (which it always is here — Owner pairing above just defaulted
            # one), falling back to an env note only if it somehow isn't.
            click.echo("\n=== Section 6/6: Autonomy & guardrails ===")
            if click.confirm(
                "Enable local mode (autonomy safe-set ON)?", default=False):
                guardrail_updates["POLYROB_LOCAL"] = "1"

            if click.confirm(
                "Apply recommended approval preset (git push, PRs, installs "
                "need your OK)?", default=False):
                from tools.controller.approval import DEFAULT_APPROVAL_REQUIRED_TOOLS
                guardrail_updates["APPROVAL_REQUIRED_TOOLS"] = ",".join(
                    DEFAULT_APPROVAL_REQUIRED_TOOLS)
                guardrail_updates["APPROVAL_PROVIDER"] = "interactive_cli"

            digest_channel = click.prompt(
                "Daily digest channel (telegram/email, blank=off)",
                default="", show_default=False)
            if digest_channel:
                if owner_user_id:
                    from core.prefs import write_preference
                    from core.runtime_paths import resolve_runtime_paths
                    prefs_home = resolve_runtime_paths(local=True).data_home
                    ok, err = write_preference(
                        prefs_home, owner_user_id, "digest.channel",
                        digest_channel, instance_id or "rob")
                    if ok:
                        write_preference(prefs_home, owner_user_id, "digest.enabled",
                                         True, instance_id or "rob")
                    else:
                        click.echo(f"Warning: digest preference not saved: {err}", err=True)
                else:
                    guardrail_updates["OWNER_DIGEST_ENABLED"] = "true"
                    click.echo(
                        "No owner id known yet — set OWNER_DIGEST_ENABLED=true "
                        "in ~/.polyrob/.env; pair an owner (`polyrob owner "
                        "invite`) to get per-owner digest preferences instead."
                    )

            # ── Optional: agent crypto wallet (fully optional; default No) ────
            # M17 (2026-07-15): don't promise invoicing unconditionally — the
            # wallet can PAY x402 paywalls immediately, but INVOICING/getting-paid
            # needs X402_INVOICE_ENABLED (OFF by default). State that at setup so
            # the promise isn't broken.
            click.echo("\n=== Optional: agent crypto wallet ===")
            if click.confirm("Give the agent a crypto wallet (pay x402 paywalls now; "
                             "invoicing needs one more flag — all approval-gated)?",
                             default=False):
                try:
                    from cli.commands.wallet import run_wallet_init_flow
                    run_wallet_init_flow(mnemonic=None, raw_seed=None,
                                         home=_core_paths.polyrob_home(),
                                         assume_yes=False, data_dir=None)
                    click.echo("To let the agent INVOICE and get paid, enable it: "
                               "`polyrob config set X402_INVOICE_ENABLED true --global` "
                               "(off by default).")
                except click.ClickException as exc:
                    click.echo(f"wallet setup skipped: {exc.message}", err=True)
                except Exception as exc:
                    click.echo(f"wallet setup failed (you can retry anytime with "
                               f"`polyrob wallet init`): {exc}", err=True)
            else:
                click.echo("Later: `polyrob wallet init` (one command).")

    # Owner pairing defaults: under --no-prompt (or --quick), honor explicit flags
    # and fall back owner→instance so `--owner` alone or `--instance-id` alone works.
    if owner_user_id and instance_id is None:
        instance_id = owner_user_id
    if instance_id and owner_user_id is None:
        owner_user_id = instance_id

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
            "POLYROB_INSTANCE_ID": instance_id,
            "POLYROB_OWNER_USER_ID": owner_user_id,
        }
        updates.update(collected_keys)
        updates.update(guardrail_updates)
        _write_env(home_env, updates)
    except OSError as exc:
        click.echo(f"Warning: could not write {home_env}: {exc}", err=True)

    sessions = Path.cwd() / ".polyrob" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)

    from cli.gitignore import ensure_polyrob_gitignored
    ensure_polyrob_gitignored(Path.cwd(), require_git_repo=False)

    click.echo(f"Initialized POLYROB. Global config: {home_env}")
    click.echo(f"Project sessions: {sessions}")
    # O3 (2026-07-14 review): the closing key-check must see every layer
    # `polyrob run` honors (shell env, ./.polyrob/.env, ~/.polyrob/.env,
    # config/.env.*) — checking only the file just written false-alarmed users
    # whose key lives in the shell env or a project-level file.
    import os as _os
    try:
        from core.bootstrap import load_env
        load_env(local_mode=True)
        merged_env = dict(_os.environ)
    except Exception:
        # Degrade to the just-written file + process env, never crash init.
        merged_env = dict(_os.environ)
        try:
            for line in home_env.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    merged_env.setdefault(k.strip(), v.strip())
        except OSError:
            pass
    if not usable_providers_with_keys(env=merged_env):
        click.echo("⚠ No usable LLM API key set — add one with "
                   "`polyrob config set OPENROUTER_API_KEY <key> --global` "
                   "(for DeepSeek models use OPENROUTER_API_KEY + model deepseek/deepseek-chat)")
    click.echo("\nNext steps (all optional):")
    click.echo("  • agent wallet:   polyrob wallet init")
    click.echo("  • avatar:         polyrob pfp generate   (or /pfp in the chat)")
    click.echo("  • surfaces:       polyrob gateway --help  (telegram, email, …)")
    click.echo("  • identity:       polyrob soul init      (author who this instance is)")
    click.echo("  • health check:   polyrob doctor")
    click.echo('\nAsk me anything about myself — try "what can you do?"')
