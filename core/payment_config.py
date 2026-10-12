"""Single source of truth for resolving the payment/deposit master seed.

The name is PAYMENT_MASTER_SEED. The legacy pydantic-settings alias MASTER_SEED
was retired in 1.4: set alone it REFUSES (``RetiredFlagError``), because a silent
fallback to "no seed" would disable — and a later rename could change — every
derived deposit address. E6 (2026-07-02 security audit) found the two entry points
(polyrob/app/initialization.py's container-built wallet_generator, and webview/server.py's
own startup-time wallet_generator) each read only ONE of the two names — an
operator following the documented PAYMENT_MASTER_SEED convention silently got a
disabled wallet generator on the container path, and a future divergent value
between the two names would derive DIFFERENT deposit addresses for the same
user_id depending which code path served the request.
"""
import os
from typing import Optional


def resolve_master_seed() -> Optional[str]:
    """Resolve the payment master seed from PAYMENT_MASTER_SEED.

    Raises ``RetiredFlagError`` when only the retired MASTER_SEED is set."""
    # 066 P0.2: both names leave os.environ once the wallet config loads.
    from core.config_policy.retired_flags import refuse_retired_alias
    from core.security.custody_env import custody_secret
    seed = custody_secret("PAYMENT_MASTER_SEED") or None
    refuse_retired_alias("MASTER_SEED", {
        "MASTER_SEED": custody_secret("MASTER_SEED") or "",
        "PAYMENT_MASTER_SEED": seed or "",
    })
    return seed
