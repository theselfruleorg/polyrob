"""E7 fold-in (A6 gap 8): the in-process SessionRegistry has NO notion of
user_id — pin this explicitly so a future change can't silently assume
tenant-scoping exists at this layer. The real tenant boundary lives one layer
down, in agents/task/path.py (get_session_root -> get_user_root); see the A6
assessment finding.
"""
import inspect

from agents.task.session_registry import SessionRegistry
from agents.task.sqlite_session_registry import SqliteSessionRegistry
from core.sqlite_util import execute_retry


def test_session_registry_register_has_no_user_id_param():
    sig = inspect.signature(SessionRegistry.register)
    assert "user_id" not in sig.parameters, (
        "SessionRegistry.register() gained a user_id param — if this is real "
        "tenant scoping, update this pinned test AND confirm every caller threads "
        "a real (non-spoofable) user_id; do not just delete this assertion."
    )


def test_session_registry_get_is_keyless_by_session_id_only():
    sig = inspect.signature(SessionRegistry.get)
    assert list(sig.parameters) == ["self", "session_id"]


def test_sqlite_session_registry_schema_has_no_user_id_column(tmp_path):
    reg = SqliteSessionRegistry(str(tmp_path / "registry.db"))
    cols = execute_retry(reg.db_path, "PRAGMA table_info(active_sessions)", fetch="all")
    col_names = {c["name"] for c in cols}
    assert "user_id" not in col_names, (
        "active_sessions gained a user_id column — force a deliberate decision "
        "about whether the registry itself should now enforce tenant scoping, "
        "rather than silently relying on it once it appears."
    )
