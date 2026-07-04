"""Authentication tables for wallet-based auth and credit system."""

from modules.database.connection import DatabaseConnection
import logging

logger = logging.getLogger('database.auth_tables')


class AuthTables:
    """Manage authentication-related tables."""

    def __init__(self, db: DatabaseConnection):
        self.db = db
        self.logger = logging.getLogger('database.auth_tables')

    async def create_tables(self) -> None:
        """Create all auth tables."""

        try:
            # Auth nonces table (for SIWE)
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS auth_nonces (
                    wallet_address TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    chain_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    used INTEGER DEFAULT 0,
                    PRIMARY KEY (wallet_address, nonce)
                )
            ''')

            # Idempotent migration for pre-existing DBs (no ALTER ... IF NOT EXISTS in SQLite).
            existing_cols = await self.db.fetch_all("PRAGMA table_info(auth_nonces)")
            if "chain_id" not in {c["name"] for c in existing_cols}:
                await self.db.execute("ALTER TABLE auth_nonces ADD COLUMN chain_id INTEGER")

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_auth_nonces_expires ON auth_nonces(expires_at)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_auth_nonces_wallet ON auth_nonces(wallet_address, used)
            ''')
            # API keys table
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    key_hash TEXT UNIQUE NOT NULL,
                    key_prefix TEXT NOT NULL,
                    name TEXT NOT NULL,
                    scopes TEXT DEFAULT '["*"]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP,
                    expires_at TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    revoked_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(is_active, expires_at)
            ''')

            # User credits table
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS user_credits (
                    user_id TEXT PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    lifetime_earned INTEGER DEFAULT 0,
                    lifetime_spent INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            # Credit transactions
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS credit_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    transaction_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    session_id TEXT,
                    balance_before INTEGER,
                    balance_after INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_credit_trans_user ON credit_transactions(user_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_credit_trans_type ON credit_transactions(transaction_type)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_credit_trans_time ON credit_transactions(timestamp)
            ''')

            # User deposit addresses
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS user_deposit_addresses (
                    user_id TEXT PRIMARY KEY,
                    deposit_address TEXT UNIQUE NOT NULL,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked TIMESTAMP,
                    total_received_usd REAL DEFAULT 0,
                    last_deposit_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_deposit_address ON user_deposit_addresses(deposit_address)
            ''')

            # Crypto payments
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS crypto_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    deposit_address TEXT NOT NULL,
                    tx_hash TEXT,
                    token_symbol TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    credits_purchased INTEGER NOT NULL,
                    status TEXT DEFAULT 'confirmed',
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    swept_at TIMESTAMP,
                    sweep_tx_hash TEXT,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_crypto_payments_user ON crypto_payments(user_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_crypto_payments_status ON crypto_payments(status)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_crypto_payments_time ON crypto_payments(detected_at)
            ''')

            # Usage records
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cached_tokens INTEGER DEFAULT 0,
                    api_cost_usd REAL DEFAULT 0.0,
                    markup_multiplier REAL DEFAULT 1.0,
                    metadata TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_records(timestamp)
            ''')

            # Pending sweeps (treasury transfer queue)
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS pending_sweeps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    from_address TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    swept_at TIMESTAMP,
                    sweep_tx_hash TEXT,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_pending_sweeps_status ON pending_sweeps(swept_at)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_pending_sweeps_chain ON pending_sweeps(chain)
            ''')

            # Wallet history
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS wallet_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    wallet_address TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    connected_at TIMESTAMP NOT NULL,
                    disconnected_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_wallet_history_user ON wallet_history(user_id)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_wallet_history_address ON wallet_history(wallet_address)
            ''')

            # Blocked users table
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id TEXT PRIMARY KEY,
                    is_blocked INTEGER DEFAULT 1,
                    blocked_reason TEXT,
                    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    blocked_by TEXT,
                    unblocked_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_blocked_users_status ON blocked_users(is_blocked)
            ''')

            # DEN Token Bonuses - Track which token IDs have been used for bonus
            # SIMPLE: Each token ID can only grant bonus ONCE (regardless of who holds it)
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS den_token_bonuses (
                    token_id TEXT NOT NULL,
                    contract_address TEXT NOT NULL,
                    bonus_granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (token_id, contract_address)
                )
            ''')

            # SECURITY: Create audit log table for sensitive operations
            from modules.database.audit_log import AuditLogger
            audit_logger = AuditLogger(self.db)
            await audit_logger.create_table()

            self.logger.info("✅ Auth tables created successfully")

        except Exception as e:
            self.logger.error(f"Error creating auth tables: {e}")
            raise
