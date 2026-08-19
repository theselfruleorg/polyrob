"""One feature-off warning for CLI verbs whose effect needs a gated loop (026 P0.4).

`polyrob goals create` used to print a green "Created goal" with GOALS_ENABLED
off — the row was durable but the dispatcher would never pick it up, and
nothing said so. Same class: `owner sub *` with SUBSCRIPTIONS_ENABLED off,
`owner invoices` with X402_INVOICE_ENABLED off, `cron schedule` with
CRON_ENABLED off (which had its own private copy of this warning — now
delegated here).

Contract: WARN, never block — the write is still durable state the loop can
use the moment the flag turns on. The note goes to STDERR so `--json` output
stays machine-readable. The remedy line follows the house grammar
(`polyrob config set KEY true --global`, restart to apply).
"""
from typing import Callable, Optional

import click


def warn_if_flag_off(key: str, consequence: str, *,
                     enabled_fn: Callable[[], bool],
                     remedy: Optional[str] = None) -> bool:
    """Echo a yellow note to stderr when *key* resolves OFF.

    ``enabled_fn`` is the flag's OWN runtime resolver (so group/posture/mode
    defaults are honored, not just the raw env var); a resolver error counts
    as OFF (matches the legacy cron warning's fail-to-warn). Returns True when
    the warning fired, for tests.
    """
    try:
        if bool(enabled_fn()):
            return False
    except Exception:
        pass
    remedy = remedy or f"polyrob config set {key} true --global"
    click.echo(click.style(
        f"note: {key} is off — {consequence} "
        f"Enable: `{remedy}` (takes effect: restart).", fg="yellow"), err=True)
    return True
