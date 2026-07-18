"""
Database Schema Version 1.0.0 - Baseline

Clean baseline schema for Task Agent with wallet-based authentication.
This is the canonical starting point - no legacy code, no migrations needed.

Created: 2025-11-15
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "1.0.0"
DESCRIPTION = "Clean Baseline - Task Agent with Wallet Auth"


async def upgrade(db, db_manager):
    """
    Apply baseline schema (v1.0.0).
    
    This creates all tables from scratch with the clean schema.
    No migration logic - just fresh install.
    """
    
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")
    
    # ========================================
    # CORE TABLES
    # ========================================
    
    # 1. Create user_profiles table
    logger.info("Creating user_profiles table...")
    await db_manager.user_profiles.create_table()
    logger.info("✓ user_profiles table created")
    
    # 2. Create auth tables
    logger.info("Creating auth tables...")
    auth_tables = db_manager.tables.get('auth_tables')
    if auth_tables:
        await auth_tables.create_tables()
        logger.info("✓ Auth tables created")
    
    # 3. Create x402 tables
    logger.info("Creating x402 tables...")
    x402_tables = db_manager.tables.get('x402_tables')
    if x402_tables:
        await x402_tables.create_tables()
        logger.info("✓ x402 tables created")
    
    # 4. Create conversation contexts (optional)
    logger.info("Creating conversation contexts...")
    conv_contexts = db_manager.tables.get('conversation_contexts')
    if conv_contexts:
        await conv_contexts.create_table()
        logger.info("✓ Conversation contexts created")
    
    # NOTE: recording the applied version is the RUNNER's single responsibility
    # (migrations/migrate.py + boot.py call DatabaseVersionManager.record_migration,
    # which writes the canonical `schema_versions` table). This migration must NOT
    # self-record: it previously INSERTed into `schema_version` (SINGULAR) — a table
    # that is never created — so `migrate upgrade` crashed on a fresh DB with
    # "no such table". Removing the self-record fixes that AND the redundant
    # double-record.

    logger.info(f"✅ Schema {VERSION} applied successfully!")
    logger.info("   - 14 core tables created")
    logger.info("   - Wallet-based authentication ready")
    logger.info("   - Credit system initialized")
    logger.info("   - x402 payment protocol ready")
    
    return True


async def downgrade(db, db_manager):
    """
    Downgrade is not supported for baseline schema.
    This is the starting point - there's nothing to downgrade to.
    """
    logger.warning("Downgrade not supported for baseline schema v1.0.0")
    raise NotImplementedError("Cannot downgrade from baseline schema")


async def verify(db, db_manager):
    """
    Verify schema was applied correctly.
    """
    
    checks = {
        'schema_version': False,
        'user_profiles': False,
        'auth_nonces': False,
        'api_keys': False,
        'user_credits': False,
        'x402_payment_requests': False,
    }
    
    try:
        # Check schema version in the canonical `schema_versions` (plural) table
        # written by DatabaseVersionManager.record_migration.
        result = await db.fetch_one("""
            SELECT version FROM schema_versions WHERE version = ?
        """, (VERSION,))
        checks['schema_version'] = result is not None
        
        # Check critical tables exist
        tables = await db.fetch_all("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """)
        
        table_names = {t['name'] for t in tables}
        
        checks['user_profiles'] = 'user_profiles' in table_names
        checks['auth_nonces'] = 'auth_nonces' in table_names
        checks['api_keys'] = 'api_keys' in table_names
        checks['user_credits'] = 'user_credits' in table_names
        checks['x402_payment_requests'] = 'x402_payment_requests' in table_names
        
        # All checks must pass
        all_passed = all(checks.values())
        
        if all_passed:
            logger.info("✅ All verification checks passed")
        else:
            failed = [k for k, v in checks.items() if not v]
            logger.error(f"❌ Verification failed: {failed}")
        
        return all_passed
        
    except Exception as e:
        logger.error(f"❌ Verification error: {e}")
        return False
