"""
Database Schema Version 1.1.0 - User MCP Servers

Adds per-user MCP server configuration tables:
- user_mcp_servers: User's custom MCP server configurations
- user_mcp_settings: Per-user MCP preferences
- user_mcp_audit_log: Security audit trail for MCP changes

Created: 2025-12-04
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "1.1.0"
DESCRIPTION = "User MCP Server Configuration"


async def upgrade(db, db_manager):
    """
    Apply v1.1.0 schema - User MCP servers.

    Adds 3 tables for per-user MCP server management.
    """

    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    # ========================================
    # USER MCP SERVERS TABLE
    # ========================================

    logger.info("Creating user_mcp_servers table...")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS user_mcp_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            -- OWNERSHIP
            user_id TEXT NOT NULL,

            -- SERVER IDENTITY
            server_name TEXT NOT NULL,
            display_name TEXT,
            server_url TEXT NOT NULL,

            -- SERVER TYPE (SSE/HTTP only - NO STDIO for users)
            server_type TEXT NOT NULL,

            -- AUTHENTICATION (Phase 1: API Key only)
            auth_method TEXT NOT NULL DEFAULT 'api_key',
            api_key_encrypted BLOB,
            headers_encrypted BLOB,

            -- CONNECTION CONFIG
            enabled INTEGER DEFAULT 1,
            timeout INTEGER DEFAULT 30,
            retry_attempts INTEGER DEFAULT 3,
            retry_delay INTEGER DEFAULT 5,
            auto_reconnect INTEGER DEFAULT 1,
            max_concurrent_requests INTEGER DEFAULT 5,

            -- STATUS & METADATA
            auth_status TEXT DEFAULT 'configured',
            last_connected_at TIMESTAMP,
            last_error TEXT,
            connection_count INTEGER DEFAULT 0,
            tools_discovered INTEGER DEFAULT 0,

            -- TIMESTAMPS
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            -- CONSTRAINTS
            FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE,
            UNIQUE(user_id, server_name),
            CHECK (server_type IN ('sse', 'http')),
            CHECK (auth_method IN ('api_key', 'bearer', 'none'))
        )
    ''')
    logger.info("  user_mcp_servers table created")

    # Indices for user_mcp_servers
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_user_mcp_servers_user
        ON user_mcp_servers(user_id)
    ''')
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_user_mcp_servers_enabled
        ON user_mcp_servers(user_id, enabled)
    ''')
    logger.info("  user_mcp_servers indices created")

    # ========================================
    # USER MCP SETTINGS TABLE
    # ========================================

    logger.info("Creating user_mcp_settings table...")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS user_mcp_settings (
            user_id TEXT PRIMARY KEY,

            -- GLOBAL BEHAVIOR
            mcp_enabled INTEGER DEFAULT 1,
            include_global_servers INTEGER DEFAULT 1,
            max_servers INTEGER DEFAULT 10,

            -- PREFERENCES
            default_timeout INTEGER DEFAULT 30,

            -- TIMESTAMPS
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
        )
    ''')
    logger.info("  user_mcp_settings table created")

    # ========================================
    # USER MCP AUDIT LOG TABLE
    # ========================================

    logger.info("Creating user_mcp_audit_log table...")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS user_mcp_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            server_name TEXT,
            details TEXT,
            ip_address TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
        )
    ''')
    logger.info("  user_mcp_audit_log table created")

    # Indices for audit log
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_user_mcp_audit_user
        ON user_mcp_audit_log(user_id)
    ''')
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_user_mcp_audit_time
        ON user_mcp_audit_log(timestamp)
    ''')
    logger.info("  user_mcp_audit_log indices created")

    # ========================================
    # RECORD VERSION
    # ========================================

    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    logger.info("   - user_mcp_servers table created")
    logger.info("   - user_mcp_settings table created")
    logger.info("   - user_mcp_audit_log table created")

    return True


async def downgrade(db, db_manager):
    """
    Downgrade from v1.1.0 to v1.0.0.

    Drops all user MCP tables.
    """
    logger.info(f"Downgrading schema: {VERSION}")

    # Drop tables in reverse order (audit log first due to no dependencies)
    await db.execute("DROP TABLE IF EXISTS user_mcp_audit_log")
    await db.execute("DROP TABLE IF EXISTS user_mcp_settings")
    await db.execute("DROP TABLE IF EXISTS user_mcp_servers")

    # Remove version record
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete")
    return True


async def verify(db, db_manager):
    """
    Verify schema was applied correctly.
    """

    checks = {
        'user_mcp_servers': False,
        'user_mcp_settings': False,
        'user_mcp_audit_log': False,
        'version_recorded': False,
    }

    try:
        # Check tables exist
        tables = await db.fetch_all("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name LIKE 'user_mcp_%'
        """)

        table_names = {t['name'] for t in tables}

        checks['user_mcp_servers'] = 'user_mcp_servers' in table_names
        checks['user_mcp_settings'] = 'user_mcp_settings' in table_names
        checks['user_mcp_audit_log'] = 'user_mcp_audit_log' in table_names

        # Check version is recorded
        result = await db.fetch_one("""
            SELECT version FROM schema_versions WHERE version = ?
        """, (VERSION,))
        checks['version_recorded'] = result is not None

        # All checks must pass
        all_passed = all(checks.values())

        if all_passed:
            logger.info(f"All verification checks passed for {VERSION}")
        else:
            failed = [k for k, v in checks.items() if not v]
            logger.error(f"Verification failed for {VERSION}: {failed}")

        return all_passed

    except Exception as e:
        logger.error(f"Verification error for {VERSION}: {e}")
        return False
