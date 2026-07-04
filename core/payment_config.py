"""Single source of truth for resolving the payment/deposit master seed.

Two env var names have existed historically: PAYMENT_MASTER_SEED (the name used
in every deployment doc/template — DEPLOYMENT.md, ENV_FILE_README.md, the .env
templates) and the legacy pydantic-settings alias MASTER_SEED (still set in the
live production .env). E6 (2026-07-02 security audit) found the two entry points
(core/initialization.py's container-built wallet_generator, and webview/server.py's
own startup-time wallet_generator) each read only ONE of the two names — an
operator following the documented PAYMENT_MASTER_SEED convention silently got a
disabled wallet generator on the container path, and a future divergent value
between the two names would derive DIFFERENT deposit addresses for the same
user_id depending which code path served the request.
"""
import os
from typing import Optional


def resolve_master_seed() -> Optional[str]:
    """Resolve the payment master seed. PAYMENT_MASTER_SEED wins; MASTER_SEED is
    the back-compat fallback for the pre-existing production deployment."""
    return os.environ.get("PAYMENT_MASTER_SEED") or os.environ.get("MASTER_SEED") or None
