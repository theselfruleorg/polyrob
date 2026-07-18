"""Back-compat shim (R-4, 2026-07-17): canonical home is core.security.untrusted_wrap.

Kept so existing importers keep working. New code imports from
``core.security.untrusted_wrap``.
"""
from core.security.untrusted_wrap import *  # noqa: F401,F403
from core.security.untrusted_wrap import maybe_wrap  # noqa: F401 — explicit: consumed by result_processing
