"""U4 (2026-07-14 review): the inline schema must be at migration HEAD.

On a fresh install `migrations/boot.py` STAMPS every shipped migration as applied
without executing it, on the assumption that the inline schema (the component
`create_table(s)`/`ensure_tables` creators) already produces the same end state. That
invariant was maintained only by discipline — a migration whose DDL isn't mirrored
inline is permanently skipped on fresh installs ("applied" but never run →
`no such table/column` forever).

This contract test builds a DB through the REAL inline creators, stamps at HEAD via
the real boot path, and then requires every shipped migration's own `verify()` to
pass. Adding a migration without mirroring its DDL inline fails here, in CI, instead
of at runtime on somebody's fresh deployment.

(It immediately caught a real drift: `billing_failures` (v1_3_0) had NO inline
creator — a fresh stamped install never created it, and every
`usage_tracker._record_billing_failure` INSERT silently failed.)
"""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_DIR = REPO_ROOT / "migrations" / "versions"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _build_inline_schema(db_path: Path):
    """Create a DB exactly the way a fresh install does: the component creators."""
    from modules.database.connection import DatabaseConnection
    from modules.database.auth_tables import AuthTables
    from modules.database.x402_tables import X402Tables
    from modules.database.user_profiles import UserProfiles
    from modules.database.user_mcp_servers import UserMCPServersHandler
    from modules.database.polymarket import PolymarketDBHandler

    db = DatabaseConnection(str(db_path))
    await db.connect()
    await AuthTables(db).create_tables()
    await X402Tables(db).create_tables()
    await UserProfiles(db).create_table()
    await UserMCPServersHandler(db, encryption=MagicMock()).ensure_tables()
    await PolymarketDBHandler(db, encryption=MagicMock()).ensure_tables()
    return db


async def _stamp_at_head(db):
    from migrations.boot import apply_migrations_at_boot
    summary = await apply_migrations_at_boot(db, versions_dir=VERSIONS_DIR)
    assert summary["error"] is None, f"boot stamping failed: {summary['error']}"
    assert summary["baselined"] is True, (
        "expected a fresh DB to be stamped at HEAD, not to execute migrations")
    return summary


def test_fresh_inline_schema_passes_every_migration_verify(tmp_path):
    async def _run():
        db = await _build_inline_schema(tmp_path / "bot.db")
        try:
            await _stamp_at_head(db)
            failures = []
            for path in sorted(VERSIONS_DIR.glob("v*.py")):
                module = _load_module(path)
                verify = getattr(module, "verify", None)
                if verify is None:
                    continue
                ok = await verify(db, None)
                if ok is not True:
                    failures.append(path.name)
            assert not failures, (
                "inline schema is NOT at migration HEAD — these shipped migrations' "
                "verify() failed on a fresh stamped install (mirror their DDL into "
                "the inline creators): " + ", ".join(failures))
        finally:
            await db.close()

    asyncio.run(_run())


def test_every_shipped_migration_has_a_verify():
    """The contract above only works if migrations ship a verify() — require it."""
    missing = [p.name for p in sorted(VERSIONS_DIR.glob("v*.py"))
               if not hasattr(_load_module(p), "verify")]
    assert not missing, f"migrations without verify(): {missing}"
