"""Back-compat shim (F-1, 2026-07-17): ``TokenBucket``'s canonical home is now
``core/rate_limit.py`` alongside the other rate-limit primitives. Import from
there in new code."""
from core.rate_limit import TokenBucket

__all__ = ["TokenBucket"]
