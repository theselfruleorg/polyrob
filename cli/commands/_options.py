"""Explicit command options override root defaults."""
import click


def inherit_options(**values):
    ctx = click.get_current_context(silent=True)
    defaults = ((ctx.find_root().obj or {}).get("cli_defaults", {}) if ctx else {})
    for key, value in values.items():
        if value is None or (key == "plain" and not value):
            values[key] = defaults.get(key, value)
    return values
