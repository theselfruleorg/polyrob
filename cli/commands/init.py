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
import shutil
import textwrap
from pathlib import Path

import click

import core.paths as _core_paths

from cli.config_store import migrate_to_dotenv


def _say(text: str, indent: str = "") -> None:
    """Echo wrapped to the terminal: an unwrapped line breaks inside a word."""
    width = max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))
    click.echo(textwrap.fill(text, width=width, initial_indent=indent,
                             subsequent_indent=indent + "  ",
                             break_long_words=False, break_on_hyphens=False))


def _env_with_home_file() -> dict:
    """The process env plus the home .env, read and not loaded: a re-run must see the key
    that the last run saved, and this question must not change the process."""
    import os as _os

    merged = dict(_os.environ)
    try:
        for line in (_core_paths.polyrob_home() / ".env").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                merged.setdefault(key.strip(), value.strip())
    except OSError:
        pass
    return merged


def _save_key_now(env_key: str, value: str) -> None:
    """A pasted key survives a Ctrl-C at any later question. Fail-open: the closing
    write reports a failure to save."""
    try:
        _write_env(_core_paths.polyrob_home() / ".env", {env_key: value})
    except (OSError, ValueError):
        pass


def _check_key(profile, entered: str) -> bool:
    """Report on a pasted key. False only when the provider REJECTED it.

    The old check looked at the shape of the key and said "present", so a dead key
    passed setup and failed on the first message.
    """
    import os as _os

    from cli.commands.auth import _stdin_isatty
    from modules.llm.profiles import credential_status
    env = dict(_os.environ)
    env[profile.env_key] = entered
    try:
        status = credential_status(env).get(profile.name)
    except Exception:
        status = None
    interactive = _stdin_isatty()
    if status is not None and not status.usable:
        _say(f"{profile.name}: present but unusable — {status.reason}", "  ")
        return not interactive  # a script has no one to answer "keep it anyway?"
    if not interactive:
        # No one to answer, and a script may have no network.
        if status is not None:
            click.echo(f"  {profile.name}: present")
        return True
    click.echo(f"  Checking the key with {profile.display_name} ...")
    try:
        from cli.commands.auth import _probe_key
        from modules.llm.provider_spec import get_spec
        verdict, _detail = _probe_key(get_spec(profile.name), entered)
    except Exception:
        verdict = None
    if verdict is False:
        click.echo(click.style(f"  {profile.display_name} rejected this key.", fg="red"))
        return False
    if verdict:
        click.echo(click.style("  The key works.", fg="green"))
    else:
        click.echo("  I could not check the key now. It is saved; the first message will test it.")
    return True


def _prompt_provider_keys(collected_keys: dict) -> None:
    """One provider choice + one validated key prompt, looping on "connect
    another" (027 WP4 — the old flow prompted for SIX keys in a row with zero
    feedback on what was pasted). Shared by ``polyrob init`` and
    ``run_quick_key_setup`` so there is ONE prompt implementation."""
    import os as _os

    from modules.llm.profiles import all_profiles, usable_providers_with_credentials

    onboardable = []
    deferred = []
    for profile in all_profiles():  # PROFILES order → OpenRouter first
        if not profile.env_key:
            # keyless-by-design (auth_type:none providers.yaml row) — there is
            # no env key to prompt for.
            continue
        if not profile.prompt_in_init:
            # Opt-in subscription plans (Ollama Cloud, z.ai GLM Coding Plan,
            # Cerebras — 024 T0) are NAMED once below instead of prompted for.
            deferred.append(profile)
            continue
        onboardable.append(profile)
    by_name = {p.name: p for p in onboardable}
    names = ", ".join(p.name for p in onboardable)

    _say(f"Providers: {names}")
    connected = sorted(usable_providers_with_credentials(_env_with_home_file()) or [])
    if connected:
        _say(f"Already connected: {', '.join(connected)}. Press Enter to keep it.")
    while True:
        choice = click.prompt(
            "Provider to connect ('skip' for none)",
            default="skip" if connected else "openrouter",
        ).strip().lower()
        if choice in ("skip", "none", "no", "-", ""):
            break
        profile = by_name.get(choice)
        if profile is None:
            _say(f"unknown provider '{choice}' — one of: {names} "
                 "(the full list: `polyrob model list`)", "  ")
            continue
        hint = f" ({profile.signup_url})" if profile.signup_url else ""
        entered = click.prompt(
            f"{profile.display_name} API key{hint} (blank to skip)",
            default="", show_default=False, hide_input=True)
        if entered:
            # Say NOW when a key cannot work, not at the first message.
            if not _check_key(profile, entered):
                if click.confirm("Keep this key anyway?", default=False):
                    collected_keys[profile.env_key] = entered
                    _save_key_now(profile.env_key, entered)
                continue
            collected_keys[profile.env_key] = entered
            _save_key_now(profile.env_key, entered)
            connected = connected or [profile.name]
        if not click.confirm("Connect another provider?", default=False):
            break

    if deferred:
        # Name a few, count the rest. Listing all of them was ~780 characters
        # of provider names in the middle of a first-run wizard.
        subs = [p for p in deferred if p.subscription][:3]
        sample = ", ".join(p.display_name for p in (subs or deferred[:3]))
        click.echo("")
        _say(f"{len(deferred)} more providers are available (subscription plans "
             f"like {sample}, gateways, regional endpoints). `polyrob model list` "
             "names them, and `polyrob auth add <provider>` connects one.")


def run_quick_key_setup() -> bool:
    """Inline OpenRouter-first key wizard used by the preflight guard.

    Prompts for provider keys, applies them to ``os.environ`` IN-PROCESS (authoritative,
    so the very next ``build_cli_container`` sees them regardless of env re-layer order),
    then persists to ``~/.polyrob/.env`` (chmod 600). Returns True iff a *usable*
    (initializable) key is now present. Never raises on a write failure.
    """
    import os as _os

    import core.paths as _core_paths
    from modules.llm.profiles import usable_providers_with_credentials

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

    ok = bool(usable_providers_with_credentials(dict(_os.environ)))
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


def _seed_identity_docs(instance_id: str) -> str:
    """Seed the SOUL docs on a fresh install; return the seed digest (or "").

    062: a reference agent writes a real default persona file at install time and POLYROB
    wrote nothing, so a fresh instance had no identity at all and `doctor` said
    "author with `polyrob soul init`" forever. Seeding is a courtesy, not a
    promotion — SOUL stays operator-authored, the agent still cannot write it,
    and the digest we record is what keeps "seeded default" honestly separate
    from "the owner wrote this".
    """
    try:
        from core.runtime_paths import resolve_data_home

        from cli.commands.soul import scaffold_soul
        data_home = Path(resolve_data_home())
        if (data_home / "identity" / "identity.md").exists():
            return ""
        _path, sha = scaffold_soul(data_home, name=instance_id or "polyrob",
                                   mission="be genuinely useful")
        return sha
    except Exception:
        return ""


def _ensure_user_skills_home() -> Path | None:
    """Create ``~/.agents/skills`` — the place a user's own skills go.

    The directory has always been READ (``core/skills/scopes``)
    and never created, so "where do I put a skill" had no visible answer.
    """
    try:
        target = Path.home() / ".agents" / "skills"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        return None


def _write_env(env_path: Path, updates: dict) -> None:
    """Upsert KEY=VALUE lines into env_path, then lock it to 0600."""
    from core.env_file import env_write_error, upsert_env_var
    for key, value in updates.items():
        if value and (error := env_write_error(key, str(value))):
            raise ValueError(error)
    for key, value in updates.items():
        if not value:
            continue
        upsert_env_var(env_path, key, str(value))


def _character_home(profile_name, home_env):
    """Where a scaffolded character belongs: the profile's home when --profile
    is given (its .env holds PERSONALITY_DEFAULT_CHARACTER), else the global
    config home. Resolved at CALL time, never bound at import."""
    if profile_name:
        from core.profiles import profile_dir
        return profile_dir(profile_name) / "characters"
    return Path(home_env).parent / "characters"


class _WizardCommand(click.Command):
    """Ctrl-C is a choice, not an error: say what is kept and how to go on."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except click.Abort:
            if ctx.params.get("skip_keys"):
                raise  # the inline key wizard reports its own cancellation
            click.echo("\nSetup stopped. Any key you entered is saved. "
                       "Run `polyrob setup` to finish.")
            ctx.exit(130)


def _section(number: int, title: str, quick: bool) -> None:
    total = 2 if quick else 7
    click.echo(f"\n=== Section {number}/{total}: {title} ===")


def _recommended_model() -> str:
    """The default model of the provider that would run now, or ""."""
    try:
        from modules.llm.profiles import usable_providers_with_credentials
        from modules.llm.provider_spec import get_spec
        for name in usable_providers_with_credentials(_env_with_home_file()) or []:
            return get_spec(name).default_model or ""
    except Exception:
        pass
    return ""


@click.command("init", cls=_WizardCommand)
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
              help="Instance id for this deployment (default 'polyrob').")
@click.option("--profile", "profile_name", default=None, metavar="NAME",
              help="Write the IDENTITY keys (persona, instance id, owner, toolset, "
                   "guardrails) into profile NAME's .env instead of the global "
                   "~/.polyrob/.env; provider keys + default model stay global. "
                   "Creates the profile if it does not exist.")
@click.option("--character", "character_slug", default=None, metavar="SLUG",
              help="Give this instance its own character: scaffold "
                   "<home>/characters/SLUG.character.json and select it with "
                   "PERSONALITY_DEFAULT_CHARACTER. See `polyrob persona init`.")
@click.option("--character-from", "character_from", default=None, metavar="NAME",
              help="Copy an existing character (e.g. writer) for --character.")
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
    profile_name,
    character_slug,
    character_from,
    skip_keys,
):
    """Set up POLYROB: your provider key, your model and optional settings."""
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

    from modules.llm.profiles import usable_providers_with_credentials

    # Pre-fill collected_keys from explicit flags (back-compat for --anthropic-key/--openai-key).
    collected_keys: dict[str, str] = {}
    if anthropic_key:
        collected_keys["ANTHROPIC_API_KEY"] = anthropic_key
    if openai_key:
        collected_keys["OPENAI_API_KEY"] = openai_key

    # Populated by section 6 (Autonomy & guardrails), below; skipped entirely
    # (stays empty) under --quick / --no-prompt / --non-interactive.
    guardrail_updates: dict[str, str] = {}

    if not no_prompt:
        # ── Section (a): Provider keys (OpenRouter-first) ───────────────────
        # --quick is flag-driven: if key(s) were already supplied via flags, skip the
        # interactive provider sweep (quick = fast, non-nagging). The full interactive
        # wizard (non-quick) still prompts every provider.
        if not (quick and collected_keys) and not skip_keys:
            _section(1, "LLM provider keys", quick)
            click.echo("Recommended: OpenRouter — one key, access to every model, auto-failover.")
            _prompt_provider_keys(collected_keys)

        # ── Section (b): Default model ───────────────────────────────────────
        _section(2, "Default model", quick)
        recommended = _recommended_model()
        _say("Press Enter to use the recommended model"
             + (f" ({recommended})" if recommended else "")
             + ". `polyrob model` changes it at any time.")
        default_model = default_model or click.prompt(
            "Default model", default="", show_default=False)
        if default_model:  # a typo reads here, not at the first message
            try:
                from cli.config_store import _provider_for_model, check_provider_model
                _pick = (default_provider or _provider_for_model(default_model)
                         or ("openrouter" if "/" in default_model else None))
                _note = check_provider_model(_pick, default_model)[1] if _pick else ""
                if _note:
                    _say(_note)
            except Exception:
                pass  # a check, never a stop

        if not quick:
            # ── Section (c): Toolset ─────────────────────────────────────────
            _section(3, "Toolset", quick)
            toolset_choices = list(TOOLSETS.keys())
            _say(f"Available toolsets: {', '.join(toolset_choices)}")
            default_ts = effective_toolset or "default"
            chosen_toolset = click.prompt(
                "Toolset", default=default_ts, show_default=True)
            if chosen_toolset in TOOLSETS:
                effective_toolset = chosen_toolset
            else:
                click.echo(f"Unknown toolset '{chosen_toolset}', using 'default'.")
                effective_toolset = "default"

            # ── Section (d): Template / persona ──────────────────────────────
            _section(4, "Template / persona", quick)
            from agents.task.templates import TEMPLATES
            template_choices = list(TEMPLATES.keys())
            click.echo(f"Available templates: {', '.join(template_choices)}")
            default_tpl = effective_persona or "general"
            chosen_template = click.prompt(
                "Template", default=default_tpl, show_default=True)
            if chosen_template not in TEMPLATES:
                click.echo(f"Unknown template '{chosen_template}', using 'general'.")
                chosen_template = "general"
            resolved = resolve_template(chosen_template)
            effective_persona = resolved.name

            # F0: the wizard already owns onboarding and already asks about the
            # persona here — but only ever wrote POLYROB_PERSONA. A template is
            # a built-in voice; a CHARACTER is this instance's own. Offer it
            # here rather than leaving seven manual steps to be inferred.
            if character_slug is None:
                from core.instance import DEFAULT_INSTANCE_ID
                if click.confirm(
                        "Give this instance its own character (its own voice)?",
                        default=False):
                    from agents.personality.persona_resolver import (
                        is_safe_character_slug,
                    )
                    suggested = profile_name or instance_id or DEFAULT_INSTANCE_ID
                    slug = click.prompt("Character slug", default=suggested,
                                        show_default=True).strip()
                    if is_safe_character_slug(slug):
                        character_slug = slug
                        character_from = character_from or (click.prompt(
                            "Copy an existing character (blank for a scaffold)",
                            default="", show_default=False).strip() or None)
                    else:
                        click.echo(f"'{slug}' is not a valid slug — skipping the "
                                   "character step (`polyrob persona init` later).")
            # If user didn't already pick a toolset explicitly, let the template drive it.
            # ── Section (e): Owner pairing ────────────────────────────────────
            # Pair this instance to an owner id so autonomy/self-evolution surfaces
            # know who to answer to. Single-user local: the owner id and instance id
            # are typically the same (both default "polyrob"). Explicit flags win.
            _section(5, "Owner pairing", quick)
            _say("These names matter only if you run several agents. Press Enter to keep them.")
            from core.instance import DEFAULT_INSTANCE_ID
            if instance_id is None:
                instance_id = click.prompt(
                    "Instance id", default=DEFAULT_INSTANCE_ID, show_default=True) or None
            if owner_user_id is None:
                owner_user_id = click.prompt(
                    "Owner user id (blank = same as instance id)",
                    default=(instance_id or DEFAULT_INSTANCE_ID), show_default=True) or None

            # ── Section 6: Autonomy & guardrails ───────────────────────────
            # All prompts blank-to-skip. Env keys land in ``guardrail_updates``
            # (merged into the ~/.polyrob/.env upsert below); the digest choice
            # writes a typed preference instead when an owner uid is known
            # (which it always is here — Owner pairing above just defaulted
            # one), falling back to an env note only if it somehow isn't.
            _section(6, "Autonomy & guardrails", quick)
            # 030 WS-F7 (C8): name the capability axes ONCE, before the prompts —
            # a new operator used to meet a 4-axis cascade with no introduction.
            _say("These questions set what the agent may do without you. "
                 "`polyrob autonomy status` shows the result at any time.")
            # 0.9.0: two honest prompts. Interactive local tools (coding/git/KB/RAG)
            # are the safe, useful default; the self-directed autonomy loops
            # (goals/self-wake/self-editing) are a separate, off-by-default opt-in.
            if click.confirm(
                "Enable interactive local tools (coding, git, knowledge base, RAG)?",
                default=True):
                guardrail_updates["POLYROB_LOCAL"] = "1"
            else:
                # The terminal sets it by default, so "no" must be written to hold.
                guardrail_updates["POLYROB_LOCAL"] = "0"
            if click.confirm(
                "Enable autonomy (agent works on its own between your messages — "
                "goals, self-wake, self-editing)?", default=False):
                guardrail_updates["AUTONOMY_ENABLED"] = "true"
                # The mode axis only matters once autonomy is on; supervised is
                # the safe default and autonomous clamps without an owner bound.
                if click.confirm(
                    "Run in AUTONOMOUS mode (capability defaults widen; requires "
                    "the owner pairing above)?", default=False):
                    guardrail_updates["AUTONOMY_MODE"] = "autonomous"

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
                    from core.instance import DEFAULT_INSTANCE_ID as _DEF_IID
                    ok, err = write_preference(
                        prefs_home, owner_user_id, "digest.channel",
                        digest_channel, instance_id or _DEF_IID)
                    if ok:
                        write_preference(prefs_home, owner_user_id, "digest.enabled",
                                         True, instance_id or _DEF_IID)
                    else:
                        click.echo(f"Warning: digest preference not saved: {err}", err=True)
                else:
                    guardrail_updates["OWNER_DIGEST_ENABLED"] = "true"
                    click.echo(
                        "No owner id known yet — the digest stays on through "
                        "OWNER_DIGEST_ENABLED (written now); pair an owner "
                        "(`polyrob owner invite`) to get per-owner digest "
                        "preferences instead."
                    )

            # ── Section 7: Reach me ────────────────────────────────────────
            # 062: the wizard configured everything EXCEPT the one thing that
            # makes the agent reachable. A chat surface needs BOTH its token
            # and its *_SURFACE_ENABLED flag — `polyrob gateway` starts a
            # surface only when the flag is on, and doctor has been explaining
            # that trap to people the wizard itself created.
            _section(7, "Reach me", quick)
            from cli.surfaces_config import flag_var, surface_choices, token_var
            click.echo("Talk to the agent from a chat app (blank = terminal only).")
            click.echo(f"Options: {', '.join(surface_choices())}")
            click.echo("Others (Signal, WhatsApp and more): `polyrob surfaces list`, then `polyrob surfaces add <name>`.")
            picked = (click.prompt("Surface", default="", show_default=False) or "").strip().lower()
            if picked and picked in surface_choices():
                tvar, fvar = token_var(picked), flag_var(picked)
                if tvar:
                    if picked == "telegram":
                        _say("Get a token from @BotFather in Telegram: send /newbot.")
                    token = click.prompt(f"{tvar} (blank to skip)", default="",
                                         show_default=False, hide_input=True).strip()
                    if token:
                        guardrail_updates[tvar] = token
                        guardrail_updates[fvar] = "true"
                        click.echo(f"{picked}: token stored and {fvar}=true")
                        click.echo("  Run it:  polyrob gateway   "
                                   "(or keep it running: polyrob service install)")
                    else:
                        click.echo(f"Skipped. Later: polyrob surfaces add {picked}")
                else:
                    # email has no token of its own; it needs IMAP/SMTP or an
                    # AgentMail key, which `polyrob doctor` names.
                    guardrail_updates[fvar] = "true"
                    click.echo(f"{fvar}=true — `polyrob doctor` names the mailbox "
                               "credentials it still needs.")
            elif picked:
                click.echo(f"Unknown surface '{picked}' — skipped.")

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
                    from core.optional_extras import require_extra
                    require_extra("crypto")
                    from cli.commands.wallet import run_wallet_init_flow
                    run_wallet_init_flow(mnemonic=None, raw_seed=None,
                                         home=_core_paths.polyrob_home(),
                                         assume_yes=False, data_dir=None)
                    click.echo("To let the agent INVOICE and get paid, enable it: "
                               "`polyrob config set X402_INVOICE_ENABLED true --global` "
                               "(off by default).")
                except click.ClickException as exc:
                    click.echo(f"wallet setup skipped: {exc.message}", err=True)
                except ImportError as exc:
                    _say(f"Wallet skipped: {exc}")
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
        if not default_provider and not no_prompt:
            # A model with no provider is dropped when the agent starts: writing it
            # would look saved and do nothing.
            _say(f"I do not know which provider serves '{default_model}', so I did not "
                 "save it. `polyrob model list` shows the names that work.")
            default_model = None

    # Persist into ~/.polyrob/.env — split by scope (W4, closes the §1.3
    # global-identity leak): IDENTITY keys go to the target profile's .env when
    # --profile is given; genuinely global things (API keys, default model)
    # always stay in the global home .env.
    home_env = _core_paths.polyrob_home() / ".env"
    identity_updates = {
        "POLYROB_AGENT_TOOLSET": effective_toolset,
        "POLYROB_PERSONA": effective_persona,
        "POLYROB_INSTANCE_ID": instance_id,
        "POLYROB_OWNER_USER_ID": owner_user_id,
    }
    if character_slug:
        identity_updates["PERSONALITY_DEFAULT_CHARACTER"] = character_slug
    identity_updates.update(guardrail_updates)
    global_updates = {
        "DEFAULT_MODEL": default_model,
        "DEFAULT_PROVIDER": default_provider,
    }
    global_updates.update(collected_keys)
    try:
        if profile_name:
            from core.profiles import ProfileError, profile_dir
            try:
                pdir = profile_dir(profile_name)
            except ProfileError as exc:
                raise click.ClickException(str(exc))
            if not pdir.is_dir():
                from cli.commands.profile import create_profile
                create_profile(profile_name)
            _write_env(pdir / ".env", identity_updates)
            _write_env(home_env, global_updates)
            click.echo(f"Identity written to profile '{profile_name}' "
                       f"({pdir / '.env'}); keys/model to {home_env}")
        else:
            updates = dict(identity_updates)
            updates.update(global_updates)
            _write_env(home_env, updates)
    except OSError as exc:
        click.echo(f"Warning: could not write {home_env}: {exc}", err=True)

    # F0: scaffold the character into the same home the identity keys went to,
    # so PERSONALITY_DEFAULT_CHARACTER and the file it names never split up.
    if character_slug:
        try:
            from cli.commands.persona import scaffold_character
            target = scaffold_character(
                character_slug, preset=character_from,
                characters_dir=_character_home(profile_name, home_env))
            click.echo(f"Character scaffolded → {target}")
            click.echo("Edit it, then `polyrob persona show` prints exactly what "
                       "the model receives.")
        except Exception as exc:
            click.echo(f"Warning: could not scaffold the character: {exc}", err=True)

    # The session tree the CLI container uses (polyrob.app.bootstrap._resolve_cli_data_home
    # -> resolve_data_home): never cwd/.polyrob, which polyrob no longer loads.
    from core.runtime_paths import resolve_data_home
    sessions = resolve_data_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)

    # 062 — seed the self, then record the install. The marker is written LAST
    # so it can only describe steps that actually happened.
    seed_sha = _seed_identity_docs(instance_id or "")
    skills_home = _ensure_user_skills_home()
    try:
        from core.bootstrap_marker import write_marker
        from core.version import get_version

        from cli.update.detect import detect_install
        from cli.update.extras import installed_extras
        ctx_install = detect_install()
        try:
            extras = installed_extras(ctx_install.repo_root)
        except Exception:
            extras = []
        # No path: the record goes to the per-user config home. (The data
        # home is no longer cwd-scoped — resolve_data_home() converges on
        # <polyrob_home>/data — but the install record is a per-user fact.)
        write_marker(version=get_version(),
                     install_method=ctx_install.method, extras=extras,
                     soul_seed_sha=seed_sha)
    except Exception:
        pass  # a missing install record must never fail an install

    # 027 WP5: only inside a git work tree — `polyrob init` in ~/Documents used
    # to leave a spurious .gitignore behind.
    from cli.gitignore import ensure_polyrob_gitignored
    ensure_polyrob_gitignored(Path.cwd(), require_git_repo=True)

    click.echo(f"\nPOLYROB is set up.\n  Config: {home_env}\n  Data:   {sessions.parent}")
    has_provider = True
    # O3 (2026-07-14 review): the closing key-check must see every layer
    # `polyrob run` honors (shell env, ./.polyrob/.env, ~/.polyrob/.env,
    # config/.env.*) — checking only the file just written false-alarmed users
    # whose key lives in the shell env or a project-level file.
    import os as _os
    try:
        from polyrob.app.bootstrap import load_env
        load_env()
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
    if not usable_providers_with_credentials(env=merged_env):
        has_provider = False
        click.echo("⚠ No usable LLM API key set — add one with "
                   "`polyrob auth add openrouter`")
    # 030 WS-F7 (C8): the scripted install path (`--no-prompt`, the README's
    # recommendation) used to end with SILENCE about the capability axes — the
    # user was never told the axes exist or where they landed. Print the same
    # effective-posture card every seat renders. Fail-open.
    # A person at the wizard just answered these questions, or took the safe defaults:
    # one line. The full card is for the scripted path, which asked nothing.
    if not no_prompt:
        try:
            from core.config_policy.posture_card import build_answers
            # This process read the env before the wizard wrote: state what was just chosen.
            import os as _os
            _os.environ.update({k: str(v) for k, v in guardrail_updates.items()
                               if k in ("POLYROB_LOCAL", "AUTONOMY_ENABLED", "AUTONOMY_MODE",
                                        "APPROVAL_REQUIRED_TOOLS", "APPROVAL_PROVIDER")})
            _say("What the agent may do:")
            for line in build_answers("terminal"):
                _say(f"  {line}")
        except Exception as e:
            _say(f"What the agent may do: unavailable({type(e).__name__}). Run `polyrob autonomy status`.")
    else:
        try:
            from core.config_policy.posture_card import render_posture_card
            click.echo("\nEffective posture (all axes; `polyrob autonomy status` re-shows this):")
            for line in render_posture_card(prefix="  "):
                click.echo(line)
        except Exception:
            pass
    if has_provider:
        click.echo("\nStart now:  polyrob     (then ask \"what can you do?\")")
    click.echo("\nLater, if you want them:")
    click.echo("  polyrob surfaces add <name>   talk to it from a chat app")
    click.echo("  polyrob service install       keep it running in the background")
    click.echo("  polyrob wallet init           give it a crypto wallet")
    click.echo("  polyrob persona init <slug>   give it its own voice")
    click.echo("  polyrob soul init --force     rewrite its generic identity docs")
    click.echo("  polyrob doctor                check the health of this install")
    if skills_home:
        click.echo(f"  Your own skills go in {skills_home}")
