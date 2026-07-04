"""polyrob model commands (P3 cli/commands split)."""
import asyncio

import click


@click.group()
def model():
    """Manage LLM models and providers."""
    pass


@model.command("list")
def model_list():
    """List available models and provider API key status."""
    asyncio.run(_model_list())


async def _model_list():
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    # local_mode=True so `rob model list` sees keys wherever the CLI does (./.polyrob,
    # ~/.polyrob, and the config/.env.* backfill) — not just a bare server-mode load.
    load_env(local_mode=True)

    from core.config import AgentConfig
    from modules.llm.profiles import all_profiles  # P8: profiles are the declarative source
    from modules.llm.available_models import available_models, steer_notes

    config = AgentConfig()
    # One key-presence oracle (Seam 1) instead of a local key_attr dict.
    ready_providers = set(config.available_providers())

    click.echo("Provider         Status     Default Model            Native")
    click.echo("-" * 68)

    # Drive the listing from ProviderProfile (single declarative source), not a
    # hardcoded dict — each profile carries default_model + capability flags (P8).
    for prof in all_profiles():
        api_key = prof.name in ready_providers
        status = click.style("ready", fg="green") if api_key else click.style("no key", fg="red")
        native = "yes" if prof.supports_native_tools else "no"
        click.echo(f"{prof.name:<16} {status:<19} {prof.default_model:<24} {native}")

    # AFTER the status table: the actual model NAMES for usable-key providers. The old
    # "(N models)" count told you how many models a provider has but never which ones —
    # that was the reported bug (no way to see a pickable name without leaving the
    # table). available_models() is the single provider-key x model-registry join used
    # by the picker too, so this list and `model set-default` (no args) always agree.
    click.echo("")
    choices = available_models()
    if not choices:
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
        from cli.ui.model_selector import run_standalone
        picked = run_standalone()
        if not picked:
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

