"""Database version management system.

Tracks database schema versions and manages migrations.
"""

import logging
import re
from pathlib import Path
from typing import Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"
_MIGRATION_RE = re.compile(r"v(\d+)_(\d+)_(\d+)_")
_FALLBACK_SCHEMA_VERSION = "1.4.0"  # used only if the versions/ dir can't be read


def latest_migration_version(versions_dir: Path = _VERSIONS_DIR) -> str:
    """Highest schema version shipped in ``migrations/versions/``.

    Derived from the ``vMAJOR_MINOR_PATCH_*`` filenames so the schema version can
    never silently drift from the actual migration set. Falls back to a pinned
    literal only if the directory is unreadable (e.g. a trimmed wheel layout).
    """
    versions: List[tuple] = []
    try:
        for path in versions_dir.glob("v*.py"):
            match = _MIGRATION_RE.match(path.name)
            if match:
                versions.append(tuple(int(part) for part in match.groups()))
    except OSError:
        pass
    if not versions:
        return _FALLBACK_SCHEMA_VERSION
    return ".".join(str(part) for part in max(versions))


class DatabaseVersionManager:
    """
    Manages database schema versions and migrations.

    Versions follow semantic versioning: MAJOR.MINOR.PATCH
    - MAJOR: Breaking schema changes
    - MINOR: New tables/columns (backward compatible)
    - PATCH: Index changes, data migrations
    """

    # Schema version (distinct axis from the app version). Derived from the highest
    # file in migrations/versions/ so it can't drift; see latest_migration_version().
    CURRENT_VERSION = latest_migration_version()

    def __init__(self, db):
        """
        Initialize version manager.

        Args:
            db: Database connection instance
        """
        self.db = db
        self.logger = logging.getLogger('migrations.version_manager')

    async def initialize(self):
        """Initialize version tracking table."""

        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS schema_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied_by TEXT DEFAULT 'system',
                checksum TEXT,
                execution_time_ms INTEGER
            )
        ''')

        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_schema_versions_version
            ON schema_versions(version)
        ''')

        await self.db.execute('''
            CREATE INDEX IF NOT EXISTS idx_schema_versions_applied_at
            ON schema_versions(applied_at DESC)
        ''')

        self.logger.info("Version tracking table initialized")

    async def get_current_version(self) -> Optional[str]:
        """Get currently applied database version."""

        result = await self.db.fetch_one("""
            SELECT version FROM schema_versions
            ORDER BY applied_at DESC
            LIMIT 1
        """)

        return result['version'] if result else None

    async def get_version_history(self) -> List[dict]:
        """Get all applied migrations in order."""

        results = await self.db.fetch_all("""
            SELECT version, description, applied_at, execution_time_ms
            FROM schema_versions
            ORDER BY applied_at ASC
        """)

        return [dict(row) for row in results]

    async def is_version_applied(self, version: str) -> bool:
        """Check if a specific version has been applied."""

        result = await self.db.fetch_one("""
            SELECT 1 FROM schema_versions WHERE version = ?
        """, (version,))

        return result is not None

    async def record_migration(
        self,
        version: str,
        description: str,
        execution_time_ms: int = 0,
        checksum: Optional[str] = None
    ):
        """Record a successful migration."""

        await self.db.execute("""
            INSERT INTO schema_versions (
                version, description, applied_at, execution_time_ms, checksum
            ) VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)
        """, (version, description, execution_time_ms, checksum))

        self.logger.info(f"Recorded migration: {version} - {description}")

    async def verify_baseline(self) -> bool:
        """
        Verify the database is at the latest schema version (CURRENT_VERSION).

        Returns:
            True if up to date, False if a migration is needed
        """

        current = await self.get_current_version()

        if current is None:
            self.logger.warning("No version recorded - database needs baseline migration")
            return False

        if current != self.CURRENT_VERSION:
            self.logger.warning(f"Database at version {current}, expected {self.CURRENT_VERSION}")
            return False

        self.logger.info(f"Database at correct version: {self.CURRENT_VERSION}")
        return True

    async def get_pending_migrations(self, migrations_dir: Path) -> List[Path]:
        """Get list of pending migrations that need to be applied."""

        if not migrations_dir.exists():
            return []

        # Get all migration files
        migration_files = sorted(migrations_dir.glob("*.py"))

        # Get applied versions
        history = await self.get_version_history()
        applied_versions = {item['version'] for item in history}

        # Filter out applied migrations
        pending = []
        for migration_file in migration_files:
            # Extract version from filename (format: v1_0_0_description.py)
            parts = migration_file.stem.split('_', 3)
            if len(parts) >= 3:
                version = f"{parts[0][1:]}.{parts[1]}.{parts[2]}"
                if version not in applied_versions:
                    pending.append(migration_file)

        return pending

    async def display_status(self):
        """Display current database version status."""

        print("\n" + "=" * 60)
        print("DATABASE VERSION STATUS")
        print("=" * 60)

        current = await self.get_current_version()

        if current:
            print(f"Current Version: {current}")
        else:
            print("Current Version: NOT SET (needs baseline migration)")

        print(f"Expected Version: {self.CURRENT_VERSION}")

        if current == self.CURRENT_VERSION:
            print("Status: ✅ UP TO DATE")
        else:
            print("Status: ⚠️  MIGRATION NEEDED")

        # Show history
        history = await self.get_version_history()

        if history:
            print(f"\nMigration History ({len(history)} migrations):")
            for item in history:
                print(f"  - {item['version']}: {item['description']}")
                print(f"    Applied: {item['applied_at']}")
                if item['execution_time_ms']:
                    print(f"    Execution: {item['execution_time_ms']}ms")

        print("=" * 60 + "\n")
