"""@handle -> numeric X user id, and the dead-handle record (prod 2026-10-09/11).

The DM sweep lists the X Chat inbox (each participant's numeric id AND @handle),
then reads each thread by @handle. A handle X no longer resolves by username
failed every run — 33 failures in 72 h ("Could not resolve participant
'RobinhoodAlphas' / 'MrGemOfficial'") — although the listing had just given its
id, and nothing recorded it. Now:

* the listing's ids are remembered process-wide (:func:`remember_handles`), and a
  listed handle resolves with no lookup;
* a handle X says does not exist is recorded in the ONE dead-target store
  (``core.surfaces.dead_targets``, surface ``x``, keyed by ``canonical_addr``)
  and later calls skip it with one log line until the mark is
  ``X_DEAD_RECHECK_SEC`` old (an account can be unsuspended, a handle
  re-registered); a successful lookup clears the mark;
* a lookup ERROR (rate limit, 5xx) is never a dead mark;
* the OAuth2 lookup failure is logged at WARNING once per handle.

Gated by ``DEAD_TARGET_REGISTRY`` like every other dead-target call site.
"""
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: canonical handle -> numeric id, from inbox listings and successful lookups.
_HANDLE_IDS: "OrderedDict[str, str]" = OrderedDict()
_HANDLE_IDS_MAX = 4096
#: handles whose OAuth2 lookup failure was already logged at WARNING.
_WARNED_LOOKUPS: set = set()
#: The dead-target surface key for an X account (the X surface's ``surface_id``).
X_DEAD_SURFACE = "x"
X_DEAD_REASON = "no_such_user"
X_DEAD_RECHECK_SEC = 7 * 86400


def canon_handle(user: str) -> str:
    from polyrob.sdk import canonical_addr
    return canonical_addr(str(user or ""))


def remember_handles(users: Optional[Iterable[Any]]) -> None:
    """Record ``{"id", "username"}`` pairs (an inbox listing's ``includes.users``)."""
    for u in users or ():
        try:
            uid, name = str(u.get("id") or ""), canon_handle(u.get("username") or "")
        except Exception:
            continue
        if uid.isdigit() and name:
            _HANDLE_IDS[name] = uid
            _HANDLE_IDS.move_to_end(name)
    while len(_HANDLE_IDS) > _HANDLE_IDS_MAX:
        _HANDLE_IDS.popitem(last=False)


def dead_store(tool: Any):
    """The dead-target store, or None (registry off / none in this process)."""
    try:
        from core.config_policy import dead_target_registry_enabled
        if not dead_target_registry_enabled():
            return None
        return tool.container.get_service("dead_targets")
    except Exception:
        return None


def dead_since(tool: Any, user: str) -> Optional[float]:
    """When *user* was recorded as not existing on X, if that mark is younger
    than ``X_DEAD_RECHECK_SEC``; else None. A numeric id is never dead here.
    Fail-open: an unreadable store answers None (the lookup runs)."""
    store = dead_store(tool)
    if store is None or str(user).isdigit():
        return None
    try:
        key = canon_handle(user)
        for row in store.list_all() or ():
            if row.get("surface") == X_DEAD_SURFACE and row.get("address") == key:
                marked = float(row.get("marked_at") or 0)
                return marked if time.time() - marked < X_DEAD_RECHECK_SEC else None
    except Exception:
        logger.debug("dead-target read failed for %s", user, exc_info=True)
    return None


def dead_handle_error(tool: Any, verb: str, user: str) -> Optional[str]:
    """The refusal for a recorded dead handle (with one log line), or None."""
    since = dead_since(tool, user)
    if since is None:
        return None
    when = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    tool.logger.info("twitter: %s for @%s skipped — recorded as not found on X since %s",
                     verb, canon_handle(user), when)
    return (f"'{user}' is recorded as not found on X (since {when}); skipped without a "
            "lookup. If the inbox listing shows this conversation, pass its numeric id "
            "instead of the @handle.")


async def resolve_user_id(tool: Any, user: str) -> Optional[str]:
    """Return a numeric user id; pass digit strings through, else resolve the
    handle (listing cache -> dead mark -> OAuth2 lookup -> OAuth 1.0a lookup)."""
    if str(user).isdigit():
        return str(user)
    key = canon_handle(user)
    if key in _HANDLE_IDS:
        return _HANDLE_IDS[key]
    if dead_since(tool, user) is not None:
        return None
    if getattr(tool, "oauth2_access_token", None) and not tool._oauth2_relogin_needed():
        try:
            resp = await tool._make_request(
                func=(tool.dm_client or tool.client).get_user,
                endpoint_type="users", username=str(user).lstrip("@"),
                user_auth=False)
            if resp and getattr(resp, "data", None):
                remember_handles([{"id": resp.data.id, "username": key}])
                return str(resp.data.id)
        except Exception as e:
            if key not in _WARNED_LOOKUPS:
                _WARNED_LOOKUPS.add(key)
                tool.logger.warning("twitter: OAuth2 username lookup for @%s failed (%s); "
                                    "trying the OAuth 1.0a lookup", key, e)
            else:
                tool.logger.debug("OAuth2 username lookup failed", exc_info=True)
    info = await tool.get_user_by_identifier(user)
    store = dead_store(tool)
    if info and info.get("id"):
        remember_handles([{"id": info["id"], "username": key}])
        if store is not None:
            try:
                store.clear(X_DEAD_SURFACE, key)
            except Exception:
                pass
        return str(info["id"])
    # During initialization a rate limit also answers None (_make_request); that
    # is "not known yet", never "does not exist" — do not mark it dead for days.
    if getattr(getattr(tool, "rate_limiter", None), "_initialization_mode", False):
        return None
    if store is not None:
        try:
            store.mark(X_DEAD_SURFACE, key, X_DEAD_REASON)
            tool.logger.warning("twitter: @%s does not resolve on X; recorded as a dead "
                                "target (looked up again after %d days)", key,
                                X_DEAD_RECHECK_SEC // 86400)
        except Exception:
            tool.logger.debug("dead-target mark failed for %s", key, exc_info=True)
    return None
