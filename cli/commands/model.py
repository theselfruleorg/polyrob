"""polyrob model commands (P3 cli/commands split)."""
import asyncio

import click


@click.group()
def model():
    """Manage LLM models and providers."""
    pass


@model.command("list")
@click.option("--json", "as_json", is_flag=True,
              help="Emit providers + usable models as JSON.")
def model_list(as_json: bool):
    """List available models and provider API key status."""
    asyncio.run(_model_list(as_json=as_json))


async def _model_list(as_json: bool = False):
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    # local_mode=True so `rob model list` sees keys wherever the CLI does (./.polyrob,
    # ~/.polyrob, and the config/.env.* backfill) — not just a bare server-mode load.
    load_env(local_mode=True)

    import os

    from modules.llm.profiles import (  # P8: profiles are the declarative source
        all_profiles,
        providers_with_keys,
        usable_providers_with_credentials,
    )
    from modules.llm.available_models import available_models, steer_notes

    # Same oracles + vocabulary as `polyrob doctor` (UX assessment 2026-08-07,
    # Q5): the old AgentConfig.available_providers() read pydantic fields that
    # only exist for the six built-ins (user rows were permanently "no key"),
    # and its presence-only semantics rendered a placeholder key as "ready" on
    # the same screen whose footer said no usable key exists.
    env = dict(os.environ)
    present = set(providers_with_keys(env))
    usable = set(usable_providers_with_credentials(env))

    if as_json:
        import json as _json
        providers = []
        for prof in all_profiles():
            if not prof.env_key and prof.auth_type == "none":
                status_txt = "no key needed"
            elif prof.name in usable:
                status_txt = "present"
            elif prof.name in present:
                status_txt = "malformed"
            else:
                status_txt = "missing"
            providers.append({
                "name": prof.name,
                "status": status_txt,
                "default_model": prof.default_model,
                "native_tools": bool(prof.supports_native_tools),
            })
        models = [
            {"provider": c.provider, "model": c.model,
             "display_name": c.display_name, "is_default": bool(c.is_default)}
            for c in available_models()
        ]
        click.echo(_json.dumps({"providers": providers, "models": models}, indent=2))
        return

    click.echo(f"{'Provider':<16} {'Status':<14} {'Default Model':<24} Native")
    click.echo("-" * 68)

    # Drive the listing from ProviderProfile (single declarative source), not a
    # hardcoded dict — each profile carries default_model + capability flags (P8).
    for prof in all_profiles():
        if not prof.env_key and prof.auth_type == "none":
            status_txt, color = "no key needed", "green"
        elif prof.name in usable:
            status_txt, color = "present", "green"
        elif prof.name in present:
            status_txt, color = "malformed", "yellow"
        else:
            status_txt, color = "missing", "red"
        native = "yes" if prof.supports_native_tools else "no"
        # Pad BEFORE styling — ANSI bytes must not count toward the column width
        # (piped output previously shifted by ~9 chars).
        status = click.style(f"{status_txt:<14}", fg=color)
        click.echo(f"{prof.name:<16} {status} {prof.default_model:<24} {native}")

    # AFTER the status table: the actual model NAMES for usable-key providers. The old
    # "(N models)" count told you how many models a provider has but never which ones —
    # that was the reported bug (no way to see a pickable name without leaving the
    # table). available_models() is the single provider-key x model-registry join used
    # by the picker too, so this list and `model set-default` (no args) always agree.
    click.echo("")
    choices = available_models()
    if not choices:
        if usable:
            # A usable provider exists (e.g. a keyless providers.yaml row) but
            # the model join doesn't list user providers yet — don't deny it.
            click.echo(
                "No listable models yet for: " + ", ".join(sorted(usable))
                + " — pass -m <model> explicitly (its declared models still route)."
            )
        else:
            click.echo("No usable provider key found — run `polyrob init` to add one.")
    else:
        click.echo("Models you can use now:")
        # Column widths sized to THIS invocation's choices (model ids vary wildly in
        # length across providers) rather than a fixed guess that misaligns for any
        # outlier.
        name_w = max((len(c.display_name) for c in choices), default=0)
        id_w = max((len(c.model) for c in choices), default=0)
        last_provider = None
        for c in choices:
            if c.provider != last_provider:
                click.echo(f"  {c.provider}")
                last_provider = c.provider
            star = "★" if c.is_default else " "
            click.echo(f"    {star} {c.display_name:<{name_w}}  {c.model:<{id_w}}  {c.pricing_hint}")
    for note in steer_notes():
        click.echo(click.style("  " + note, fg="yellow"))


@model.command("set-default")
@click.argument("provider", required=False)
@click.argument("model_name", required=False)
def model_set_default(provider: str = None, model_name: str = None):
    """Persist a default provider+model for `polyrob run` (stored in ~/.polyrob/cli.json).

    Called with no arguments, launches an interactive picker instead.
    """
    from cli.config_store import set_default_model, check_provider_model
    from modules.llm.profiles import all_profiles

    if not provider and not model_name:
        import os

        from cli.ui import model_selector
        picked = model_selector.run_standalone()
        if not picked:
            # 027 WP4: with zero usable keys the picker has nothing to pick —
            # bare "Cancelled." left the user with no next step.
            from modules.llm.profiles import usable_providers_with_credentials
            if not usable_providers_with_credentials(dict(os.environ)):
                click.echo(
                    "No usable provider key found — connect one: "
                    "`polyrob auth add <provider>` (or `polyrob init` for "
                    "guided setup).")
            else:
                click.echo("Cancelled.")
            return
        provider, model_name = picked

    known, warning = check_provider_model(provider, model_name)
    if not known:
        profiles = ", ".join(p.name for p in all_profiles())
        click.echo(click.style(f"Unknown provider '{provider}'. Known: {profiles}", fg="red"))
        raise SystemExit(1)
    if warning:
        click.echo(click.style(f"Warning: {warning}", fg="yellow"))

    set_default_model(provider, model_name)
    click.echo(f"Default model set: {provider} / {model_name}")
    # An env pin (CHAT_/DEFAULT_PROVIDER) outranks cli.json for new sessions — say so.
    from cli.config_store import env_default_override_note
    note = env_default_override_note(provider)
    if note:
        click.echo(click.style(note, fg="yellow"))

