"""Shared command options: root defaults, and the one spelling of the output and
confirm switches (113 §2 item 10, T-09)."""
import click


def json_option(f=None, *, help="Print machine-readable JSON."):
    """``--json``: print machine-readable JSON (the value lands in ``as_json``).
    Use bare (``@json_option``) or with its own help (``@json_option(help=...)``)."""
    def deco(fn):
        return click.option("--json", "as_json", is_flag=True, help=help)(fn)
    return deco(f) if f is not None else deco


def yes_option(f=None, *, help="Skip the confirmation prompt.", dest="yes"):
    """``--yes`` / ``-y``: answer yes to the confirmation (the value lands in *dest*).
    Use bare (``@yes_option``) or with arguments (``@yes_option(help=...)``)."""
    def deco(fn):
        return click.option("--yes", "-y", dest, is_flag=True, help=help)(fn)
    return deco(f) if f is not None else deco


def inherit_options(**values):
    ctx = click.get_current_context(silent=True)
    defaults = ((ctx.find_root().obj or {}).get("cli_defaults", {}) if ctx else {})
    for key, value in values.items():
        if value is None or (key == "plain" and not value):
            values[key] = defaults.get(key, value)
    return values
