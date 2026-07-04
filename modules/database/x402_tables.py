"""x402 payment tables."""

from modules.database.connection import DatabaseConnection
import logging

logger = logging.getLogger('database.x402_tables')


class X402Tables:
    """Manage x402 payment tables."""

    def __init__(self, db: DatabaseConnection):
        self.db = db
        self.logger = logging.getLogger('database.x402_tables')

    async def create_tables(self) -> None:
        """Create x402 tables."""

        try:
            # x402 payment requests
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS x402_payment_requests (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    payer_address TEXT,
                    amount TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    asset TEXT NOT NULL,
                    chain TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    nonce TEXT UNIQUE NOT NULL,
                    deadline INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending',
                    transaction_hash TEXT,
                    payment_id TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE SET NULL
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_nonce
                ON x402_payment_requests(nonce)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_status
                ON x402_payment_requests(status)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_payer
                ON x402_payment_requests(payer_address)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_requests_user
                ON x402_payment_requests(user_id)
            ''')

            # x402 access log
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS x402_access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_request_id TEXT NOT NULL,
                    payer_address TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    method TEXT NOT NULL,
                    response_status INTEGER,
                    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (payment_request_id) REFERENCES x402_payment_requests(id)
                )
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_access_payer
                ON x402_access_log(payer_address)
            ''')

            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_x402_access_endpoint
                ON x402_access_log(endpoint)
            ''')

            self.logger.info("✅ x402 tables created successfully")

        except Exception as e:
            self.logger.error(f"Error creating x402 tables: {e}")
            raise
