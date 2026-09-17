"""Shared x402 plumbing: DB resolution + money-telemetry emit.

ONE home for the two private helpers that were duplicated byte-for-byte across
``invoicing.py`` and ``subscriptions.py`` (and imported cross-module by
``settlement_watcher.py`` reaching into ``invoicing._emit``). ``emit`` takes an
explicit ``source`` so each caller keeps its own telemetry label.
"""
from typing import Optional


async def resolve_db(db=None):
    if db is not None:
        return db
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    return container.get_service("database_manager")


def emit(kind: str, *, source: str, user_id: str, session_id: str = "",
         attrs: Optional[dict] = None) -> None:
    """First-class money telemetry (fail-open). attrs passed as an explicit dict —
    the record() reserved-kwarg collision landmine."""
    from core.event_log import emit as _emit
    _emit(kind, source=source, user_id=user_id, session_id=session_id, attrs=attrs)
