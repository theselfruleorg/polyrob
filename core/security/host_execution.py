"""Host execution cannot share a trust domain with signing credentials."""
import os

from core.env import bool_env


def wallet_custody_enabled() -> bool:
    """Treat an enabled wallet or any supported master seed as custody.

    Never construct a wallet or return credential contents just to decide
    whether model-controlled subprocesses may run on the host.
    """
    return bool_env("AGENT_WALLET_ENABLED", False) or any(
        bool(os.environ.get(name))
        for name in ("AGENT_WALLET_MASTER_SEED", "PAYMENT_MASTER_SEED", "MASTER_SEED")
    )


def host_execution_refusal():
    if wallet_custody_enabled():
        return ("host execution refused while wallet custody is enabled; "
                "use a sandbox backend or a separate development instance without signing credentials")
    return None
