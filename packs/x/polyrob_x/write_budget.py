"""One durable X write budget shared by API/browser clients and tool instances."""
import logging

from core.env import int_env
from core.rate_limit import PersistentWindowLimiter
from core.runtime_paths import data_home_db_path

logger = logging.getLogger(__name__)


def _db_path():
    return data_home_db_path("x_write_attempts.db")


def reserve_write(*, is_dm: bool = False, units: int = 1) -> bool:
    """Reserve before the wire. Unknown/failed attempts retain their reservation."""
    limits = {"x:writes": int_env("TWITTER_WRITE_MAX_PER_HOUR", 15)}
    if is_dm:
        limits["x:dm"] = int_env("TWITTER_DM_MAX_PER_HOUR", 5)
    if any(limit <= 0 for limit in limits.values()):
        return False
    try:
        return PersistentWindowLimiter(_db_path(), window_seconds=3600).check_many(limits, units=units)
    except Exception:
        logger.warning("X write budget is unreadable; refusing the write")
        return False


#: 109: the part of the hour's X writes that CODE may use (the storefront's thread
#: replies); the rest stays for the owner's and the model's own posts.
COMMERCE_SHARE = 2 / 3


def share_available(share: float = COMMERCE_SHARE, *, units: int = 1) -> bool:
    """True when *units* more writes stay inside *share* of the hourly cap. A read:
    the write itself still reserves through :func:`reserve_write`. Fail-closed."""
    cap = int_env("TWITTER_WRITE_MAX_PER_HOUR", 15)
    if cap <= 0:
        return False
    try:
        used = PersistentWindowLimiter(_db_path(), window_seconds=3600).used("x:writes")
    except Exception:
        logger.warning("X write budget is unreadable; no code write")
        return False
    return used + units <= int(cap * share)
