"""
Database migration runner.

Usage:
    python migrations/migrate.py upgrade    # Apply pending migrations
    python migrations/migrate.py status     # Show current version
    python migrations/migrate.py baseline   # Apply v1.0.0 baseline
"""

import asyncio
import sys
from pathlib import Path
import time
import logging

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.config import BotConfig
from core.container import DependencyContainer
from core.initialization import initialize_core
from migrations.version_manager import DatabaseVersionManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('migrations')


async def run_migrations(command: str = 'upgrade'):
    """Run database migrations."""

    logger.info("=" * 60)
    logger.info("DATABASE MIGRATION RUNNER")
    logger.info("=" * 60)

    try:
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

        if command == 'status':
            # Show status
            await version_mgr.display_status()
            return True

        elif command == 'baseline':
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

            for migration_file in pending:
                logger.info(f"\nApplying: {migration_file.name}")

                # Import migration module
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    migration_file.stem,
                    migration_file
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Run upgrade
                start_time = time.time()

                await module.upgrade(db, db_manager)

                execution_time = int((time.time() - start_time) * 1000)

                # Record migration
                await version_mgr.record_migration(
                    version=module.VERSION,
                    description=module.DESCRIPTION,
                    execution_time_ms=execution_time
                )

                logger.info(f"✅ Applied in {execution_time}ms")

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
