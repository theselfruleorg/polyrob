"""
Database migration runner.

Usage:
    python migrations/migrate.py upgrade    # Apply pending migrations
    python migrations/migrate.py status     # Show current version
    python migrations/migrate.py baseline   # Apply v1.0.0 baseline
"""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
import time
import logging

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('migrations')


def _status_db_path() -> Path:
    """Resolve the live database without constructing the runtime container."""
    explicit = (os.environ.get("DB_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    from core.runtime_paths import resolve_data_home
    return resolve_data_home() / "database" / "bot.db"


def display_status_readonly() -> bool:
    """Print migration status without creating directories, tables, or a DB.

    ``status`` is used by release/install diagnostics. Initializing the normal
    container here used to create a database inside an installed wheel when no
    data home was configured, and made a read-only check mutate the filesystem.
    """
    from migrations.version_manager import latest_migration_version

    expected = latest_migration_version()
    path = _status_db_path()
    current = None
    history = []
    if path.is_file():
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                con.row_factory = sqlite3.Row
                exists = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='schema_versions'"
                ).fetchone()
                if exists:
                    history = con.execute(
                        "SELECT version, description, applied_at, execution_time_ms "
                        "FROM schema_versions ORDER BY applied_at ASC"
                    ).fetchall()
                    if history:
                        current = history[-1]["version"]
        except sqlite3.Error as exc:
            logger.error("Could not read migration status from %s: %s", path, exc)
            return False

    print("\n" + "=" * 60)
    print("DATABASE VERSION STATUS")
    print("=" * 60)
    print(f"Current Version: {current}" if current else
          "Current Version: NOT SET (needs baseline migration)")
    print(f"Expected Version: {expected}")
    print("Status: ✅ UP TO DATE" if current == expected else
          "Status: ⚠️  MIGRATION NEEDED")
    if history:
        print(f"\nMigration History ({len(history)} migrations):")
        for item in history:
            print(f"  - {item['version']}: {item['description']}")
            print(f"    Applied: {item['applied_at']}")
            if item["execution_time_ms"]:
                print(f"    Execution: {item['execution_time_ms']}ms")
    print("=" * 60 + "\n")
    return True


async def apply_pending_migrations(db, db_manager, version_mgr, migrations_dir: Path) -> list:
    """Apply every pending migration in ``migrations_dir``; return the applied versions.

    Recording is guarded with ``is_version_applied`` (mirrors ``migrations/boot.py``):
    several shipped migrations self-record via ``INSERT OR REPLACE INTO schema_versions``,
    and an unconditional ``record_migration`` afterwards is a plain INSERT into a UNIQUE
    column → IntegrityError → the whole run (and `polyrob update --apply`) fails.
    """
    import importlib.util

    pending = await version_mgr.get_pending_migrations(migrations_dir)
    applied = []

    for migration_file in pending:
        logger.info(f"\nApplying: {migration_file.name}")

        spec = importlib.util.spec_from_file_location(
            migration_file.stem,
            migration_file
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        start_time = time.time()

        await module.upgrade(db, db_manager)

        execution_time = int((time.time() - start_time) * 1000)

        # Record via the SSOT exactly once (some migrations self-record).
        if not await version_mgr.is_version_applied(module.VERSION):
            await version_mgr.record_migration(
                version=module.VERSION,
                description=module.DESCRIPTION,
                execution_time_ms=execution_time
            )

        applied.append(module.VERSION)
        logger.info(f"✅ Applied in {execution_time}ms")

    return applied


async def run_migrations(command: str = 'upgrade'):
    """Run database migrations."""

    logger.info("=" * 60)
    logger.info("DATABASE MIGRATION RUNNER")
    logger.info("=" * 60)

    if command == "status":
        return display_status_readonly()

    try:
        # Upgrade/baseline need the runtime container. Keep these imports out of
        # the read-only status path so a base install does not import optional
        # provider SDKs just to inspect a SQLite file.
        from core.config import BotConfig
        from core.container import DependencyContainer
        from core.initialization import initialize_core
        from migrations.version_manager import DatabaseVersionManager

        # Initialize
        config = BotConfig()
        container = DependencyContainer.get_instance(config)

        logger.info("Initializing core services...")
        await initialize_core(container)

        db_manager = container.get_service('database_manager')
        if not db_manager:
            logger.error("Database manager not available!")
            return False

        db = db_manager.connection

        # Initialize version manager
        version_mgr = DatabaseVersionManager(db)
        await version_mgr.initialize()

        if command == 'baseline':
            # Apply baseline (v1.0.0)
            logger.info("\n" + "=" * 60)
            logger.info("APPLYING BASELINE MIGRATION (v1.0.0)")
            logger.info("=" * 60)

            # Check if already applied
            if await version_mgr.is_version_applied("1.0.0"):
                logger.warning("Baseline v1.0.0 already applied!")
                return True

            # Import and run baseline migration
            from migrations.versions.v1_0_0_baseline import upgrade, VERSION, DESCRIPTION

            start_time = time.time()

            await upgrade(db, db_manager)

            execution_time = int((time.time() - start_time) * 1000)

            # Record migration
            await version_mgr.record_migration(
                version=VERSION,
                description=DESCRIPTION,
                execution_time_ms=execution_time
            )

            logger.info(f"\n✅ Baseline migration completed in {execution_time}ms")

            # Show status
            await version_mgr.display_status()

            return True

        elif command == 'upgrade':
            # Apply pending migrations
            logger.info("\n" + "=" * 60)
            logger.info("CHECKING FOR PENDING MIGRATIONS")
            logger.info("=" * 60)

            migrations_dir = Path(__file__).parent / "versions"
            pending = await version_mgr.get_pending_migrations(migrations_dir)

            if not pending:
                logger.info("✅ No pending migrations")
                await version_mgr.display_status()
                return True

            logger.info(f"Found {len(pending)} pending migration(s)")

            await apply_pending_migrations(db, db_manager, version_mgr, migrations_dir)

            logger.info("\n✅ All migrations applied successfully")

            # Show final status
            await version_mgr.display_status()

            return True

        else:
            logger.error(f"Unknown command: {command}")
            logger.info("Usage: python migrations/migrate.py [status|baseline|upgrade]")
            return False

    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else 'upgrade'
    success = asyncio.run(run_migrations(command))
    sys.exit(0 if success else 1)
