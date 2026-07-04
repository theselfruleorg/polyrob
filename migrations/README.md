# Database Migrations

_Last reviewed: 2026-06-30._

Organized migration system for POLYROB database schema.

## Current Version: 1.4.0

> **`migrations/versions/` is the authoritative list of migrations.** Run
> `ls migrations/versions/` (and `python migrations/migrate.py status`) for the
> live state; the summary below may lag.

---

## 📁 Directory Structure

```
migrations/
├── README.md                          # This file
├── migrate.py                         # Migration runner
├── version_manager.py                 # Version tracking
└── versions/                          # Migration scripts
    ├── v1_0_0_baseline.py             # v1.0.0 - Auth & Credit System baseline
    ├── v1_1_0_user_mcp_servers.py     # v1.1.0 - Per-user MCP server config
    ├── v1_2_0_den_token_bonuses.py    # v1.2.0 - DEN token bonuses
    ├── v1_3_0_billing_failures.py     # v1.3.0 - Billing-failure tracking table
    └── v1_4_0_polymarket_credentials.py  # v1.4.0 - Polymarket credentials storage
```

---

## 🚀 Quick Start

### Check Database Version

```bash
python migrations/migrate.py status
```

### Apply Baseline (v1.0.0) - First Time Setup

```bash
python migrations/migrate.py baseline
```

### Apply Pending Migrations

```bash
python migrations/migrate.py upgrade
```

---

## 📋 Migration Commands

| Command | Description |
|---------|-------------|
| `status` | Show current database version and history |
| `baseline` | Apply v1.0.0 baseline (first time setup) |
| `upgrade` | Apply all pending migrations |

---

## 🏗️ Version 1.0.0 - Baseline Schema

### Included Tables:

**Core:**
- `user_profiles` - User accounts with auth fields
- `schema_versions` - Migration tracking

**Auth:**
- `auth_nonces` - SIWE nonce management
- `api_keys` - API key storage
- `wallet_history` - Past wallet connections

**Credits:**
- `user_credits` - Credit balances
- `credit_transactions` - Transaction history
- `usage_records` - Resource usage tracking

**Payments:**
- `user_deposit_addresses` - Deposit wallets
- `crypto_payments` - Payment history
- `pending_sweeps` - Treasury transfers

**Other:**
- `conversation_contexts` - Chat contexts
- `simulation_states` - Simulation tracking
- `den_password_codes` - Password generation
- `callback_queries` - Telegram callbacks

### Key Features:

✅ **Account Linking** - Prevents duplicate accounts
✅ **Wallet Chain Tracking** - Multi-chain support
✅ **UNIQUE Constraints** - Data integrity
✅ **Foreign Keys** - Referential integrity
✅ **Indexes** - Query performance

---

## 📝 Creating New Migrations

### Naming Convention:

```
versions/v{MAJOR}_{MINOR}_{PATCH}_{description}.py
```

Examples:
- `v1_0_1_add_user_avatar.py`
- `v1_1_0_add_notification_preferences.py`
- `v2_0_0_major_schema_refactor.py`

### Template:

```python
"""
Database Schema Version X.Y.Z - Description

Brief description of changes.

Created: YYYY-MM-DD
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "X.Y.Z"
DESCRIPTION = "Brief description"


async def upgrade(db, db_manager):
    """Apply migration."""

    logger.info(f"Applying migration: {VERSION} - {DESCRIPTION}")

    # Your migration code here
    await db.execute("""
        ALTER TABLE user_profiles
        ADD COLUMN new_field TEXT
    """)

    logger.info(f"Migration {VERSION} completed successfully")


async def downgrade(db, db_manager):
    """Rollback migration."""

    logger.warning(f"Rolling back migration: {VERSION}")

    # Your rollback code here
    await db.execute("""
        ALTER TABLE user_profiles
        DROP COLUMN new_field
    """)

    logger.warning(f"Migration {VERSION} rolled back")
```

### Rules:

1. **One migration per version**
2. **Always include downgrade()** (even if just a warning)
3. **Test on dev before production**
4. **Never modify applied migrations**
5. **Use transactions where possible**

---

## 🔄 Migration Workflow

### Development

```bash
# 1. Create new migration
cp migrations/versions/v1_0_0_baseline.py \
   migrations/versions/v1_0_1_my_feature.py

# 2. Edit migration
# Update VERSION, DESCRIPTION, upgrade(), downgrade()

# 3. Test locally
python migrations/migrate.py status
python migrations/migrate.py upgrade

# 4. Verify
sqlite3 data/database/bot.db "SELECT * FROM schema_versions"
```

### Production Deployment

```bash
# 1. Backup database
cp data/database/bot.db data/database/bot.db.backup

# 2. Run migrations
python migrations/migrate.py upgrade

# 3. Verify
python migrations/migrate.py status

# 4. If issues, rollback
# (restore from backup)
```

---

## 📊 Version Tracking

All applied migrations are tracked in `schema_versions` table:

```sql
CREATE TABLE schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT DEFAULT 'system',
    checksum TEXT,
    execution_time_ms INTEGER
)
```

### Query Migration History:

```sql
SELECT version, description, applied_at, execution_time_ms
FROM schema_versions
ORDER BY applied_at ASC;
```

---

## 🛡️ Safety Features

### Idempotent Operations

All migrations use `IF NOT EXISTS` / `IF EXISTS`:

```sql
CREATE TABLE IF NOT EXISTS new_table (...);
CREATE INDEX IF NOT EXISTS idx_name ON table(column);
ALTER TABLE ... DROP COLUMN IF EXISTS column_name;
```

### Version Checking

Migration runner automatically:
- ✅ Checks if migration already applied
- ✅ Skips applied migrations
- ✅ Records execution time
- ✅ Logs all operations

### Transaction Support

For complex migrations:

```python
async def upgrade(db, db_manager):
    await db.execute("BEGIN TRANSACTION")

    try:
        # ... migration operations ...
        await db.execute("COMMIT")
    except Exception as e:
        await db.execute("ROLLBACK")
        raise
```

---

## ⚠️ Important Notes

### Baseline Migration (v1.0.0)

- **First time setup ONLY**
- Creates all tables from scratch
- Initializes credit balances for existing users
- **Do NOT run on existing production database with data!**

### For Existing Deployments:

If you already have a database with user_profiles:

```bash
# Option 1: Mark as baseline without re-creating tables
python -c "
from migrations.migrate import run_migrations
import asyncio

async def mark_baseline():
    # ... connect to db ...
    version_mgr = DatabaseVersionManager(db)
    await version_mgr.initialize()
    await version_mgr.record_migration('1.0.0', 'Baseline - existing deployment', 0)

asyncio.run(mark_baseline())
"

# Option 2: Fresh start (backup first!)
mv data/database/bot.db data/database/bot.db.old
python migrations/migrate.py baseline
```

---

## 🔍 Troubleshooting

### "Migration already applied"

```bash
# Check what's applied
python migrations/migrate.py status

# If you need to re-apply (dangerous!)
sqlite3 data/database/bot.db "DELETE FROM schema_versions WHERE version='X.Y.Z'"
python migrations/migrate.py upgrade
```

### "Table already exists"

Migration failed mid-way. Check database state:

```bash
sqlite3 data/database/bot.db ".tables"
sqlite3 data/database/bot.db ".schema table_name"

# Clean up partial migration
# (restore from backup recommended)
```

### Testing Migrations

```bash
# 1. Backup
cp data/database/bot.db data/database/bot.db.test

# 2. Test migration
python migrations/migrate.py upgrade

# 3. Verify
python migrations/migrate.py status
# Check tables manually

# 4. If good, apply to production
# If bad, restore backup
mv data/database/bot.db.test data/database/bot.db
```

---

## 📈 Future Migrations

Examples of what future migrations might include:

**v1.0.1** - Minor fixes
- Add indexes
- Fix data inconsistencies
- Small schema tweaks

**v1.1.0** - New features (backward compatible)
- Add new tables (e.g., notifications)
- Add new columns (e.g., user preferences)
- New indexes

**v2.0.0** - Breaking changes
- Major schema refactor
- Table renames/merges
- Data type changes

---

## 🎯 Best Practices

1. **Test First** - Always test on dev before production
2. **Backup Always** - Backup before running migrations
3. **Small Migrations** - One feature per migration
4. **Clear Descriptions** - Explain what and why
5. **Reversible** - Provide downgrade when possible
6. **Version Properly** - Follow semver
7. **Document** - Update this README

---

**Current Version:** 1.4.0 (see `migrations/versions/` for the authoritative list)
**Last Updated:** 2026-06-22
