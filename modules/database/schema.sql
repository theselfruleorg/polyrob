-- ============================================================================
-- Database Schema Version 1.0.0 - Clean Baseline
-- ============================================================================
-- 
-- Minimal schema for Task Agent application with wallet-based authentication.
-- This is the canonical starting point - no legacy code, no migrations.
--
-- Application: Task Agent (HTTP API)
-- Auth: Wallet-based (SIWE)
-- Identity: Single system (wallet addresses only)
--
-- Created: 2025-11-15
-- ============================================================================

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

-- Record this schema version
INSERT OR IGNORE INTO schema_version (version, description) 
VALUES ('1.0.0', 'Clean baseline - Task Agent with wallet auth');


-- ============================================================================
-- CORE IDENTITY & AUTHENTICATION
-- ============================================================================

-- User profiles (wallet-based identity)
CREATE TABLE IF NOT EXISTS user_profiles (
    -- IDENTIFIERS
    user_id TEXT PRIMARY KEY,
    wallet_address TEXT UNIQUE NOT NULL,
    
    -- OPTIONAL PROFILE
    email TEXT,
    first_name TEXT,
    last_name TEXT,
    
    -- AUTHORIZATION
    role TEXT DEFAULT 'user' NOT NULL,
    tier TEXT DEFAULT 'free' NOT NULL,
    
    -- WALLET TRACKING
    current_wallet_chain TEXT DEFAULT 'ethereum',
    current_wallet_connected_at TIMESTAMP,
    
    -- TOKEN OWNERSHIP (for tier calculation)
    den_token_count INTEGER DEFAULT 0,
    den_token_verified_at TIMESTAMP,
    
    -- METADATA
    total_sessions INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    -- CONSTRAINTS (simplified: only user/admin roles)
    CHECK (role IN ('user', 'admin')),
    CHECK (tier IN ('free', 'free_access', 'holder', 'x402', 'admin'))
);

-- Indices for user_profiles
CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet 
    ON user_profiles(wallet_address);
CREATE INDEX IF NOT EXISTS idx_role 
    ON user_profiles(role);
CREATE INDEX IF NOT EXISTS idx_tier 
    ON user_profiles(tier);
CREATE INDEX IF NOT EXISTS idx_email 
    ON user_profiles(email) WHERE email IS NOT NULL;

-- Authentication nonces (for SIWE wallet authentication)
CREATE TABLE IF NOT EXISTS auth_nonces (
    wallet_address TEXT NOT NULL,
    nonce TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    used INTEGER DEFAULT 0,
    PRIMARY KEY (wallet_address, nonce)
);

-- Indices for auth_nonces
CREATE INDEX IF NOT EXISTS idx_auth_nonces_expires 
    ON auth_nonces(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_nonces_wallet 
    ON auth_nonces(wallet_address, used);

-- API keys for programmatic access
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
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Indices for api_keys
CREATE INDEX IF NOT EXISTS idx_api_keys_user 
    ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash 
    ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_active 
    ON api_keys(is_active, expires_at);


-- ============================================================================
-- CREDITS & USAGE TRACKING
-- ============================================================================

-- User credit balances
CREATE TABLE IF NOT EXISTS user_credits (
    user_id TEXT PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    lifetime_earned INTEGER DEFAULT 0,
    lifetime_spent INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Credit transaction history
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
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Indices for credit_transactions
CREATE INDEX IF NOT EXISTS idx_credit_trans_user 
    ON credit_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_credit_trans_type 
    ON credit_transactions(transaction_type);
CREATE INDEX IF NOT EXISTS idx_credit_trans_time 
    ON credit_transactions(timestamp);

-- Usage records for resource tracking
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
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Indices for usage_records
CREATE INDEX IF NOT EXISTS idx_usage_user 
    ON usage_records(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_session 
    ON usage_records(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_time 
    ON usage_records(timestamp);


-- ============================================================================
-- x402 PAYMENT PROTOCOL
-- ============================================================================

-- x402 payment requests
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
);

-- Indices for x402_payment_requests
CREATE INDEX IF NOT EXISTS idx_x402_requests_nonce 
    ON x402_payment_requests(nonce);
CREATE INDEX IF NOT EXISTS idx_x402_requests_status 
    ON x402_payment_requests(status);
CREATE INDEX IF NOT EXISTS idx_x402_requests_payer 
    ON x402_payment_requests(payer_address);
CREATE INDEX IF NOT EXISTS idx_x402_requests_user 
    ON x402_payment_requests(user_id);

-- x402 access log
CREATE TABLE IF NOT EXISTS x402_access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_request_id TEXT NOT NULL,
    payer_address TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    method TEXT NOT NULL,
    response_status INTEGER,
    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (payment_request_id) REFERENCES x402_payment_requests(id) ON DELETE CASCADE
);

-- Indices for x402_access_log
CREATE INDEX IF NOT EXISTS idx_x402_access_payer 
    ON x402_access_log(payer_address);
CREATE INDEX IF NOT EXISTS idx_x402_access_endpoint 
    ON x402_access_log(endpoint);


-- ============================================================================
-- CRYPTO PAYMENT INFRASTRUCTURE (Optional - for crypto deposits)
-- ============================================================================

-- User deposit addresses for crypto payments
CREATE TABLE IF NOT EXISTS user_deposit_addresses (
    user_id TEXT PRIMARY KEY,
    deposit_address TEXT UNIQUE NOT NULL,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_checked TIMESTAMP,
    total_received_usd REAL DEFAULT 0,
    last_deposit_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Indices for user_deposit_addresses
CREATE UNIQUE INDEX IF NOT EXISTS idx_deposit_address 
    ON user_deposit_addresses(deposit_address);

-- Crypto payment records
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
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Indices for crypto_payments
CREATE INDEX IF NOT EXISTS idx_crypto_payments_user 
    ON crypto_payments(user_id);
CREATE INDEX IF NOT EXISTS idx_crypto_payments_status 
    ON crypto_payments(status);
CREATE INDEX IF NOT EXISTS idx_crypto_payments_time 
    ON crypto_payments(detected_at);

-- Pending sweeps (treasury transfer queue)
CREATE TABLE IF NOT EXISTS pending_sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    chain TEXT NOT NULL,
    from_address TEXT NOT NULL,
    amount TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    swept_at TIMESTAMP,
    sweep_tx_hash TEXT,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Indices for pending_sweeps
CREATE INDEX IF NOT EXISTS idx_pending_sweeps_status 
    ON pending_sweeps(swept_at);
CREATE INDEX IF NOT EXISTS idx_pending_sweeps_chain 
    ON pending_sweeps(chain);

-- Wallet connection history
CREATE TABLE IF NOT EXISTS wallet_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    chain TEXT NOT NULL,
    connected_at TIMESTAMP NOT NULL,
    disconnected_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- Indices for wallet_history
CREATE INDEX IF NOT EXISTS idx_wallet_history_user 
    ON wallet_history(user_id);
CREATE INDEX IF NOT EXISTS idx_wallet_history_address 
    ON wallet_history(wallet_address);


-- ============================================================================
-- OPTIONAL: CONVERSATION CONTEXTS (if needed for chat features)
-- ============================================================================
-- Note: Task Agent sessions are stored in filesystem.
-- This table is optional and can be removed if not used.

CREATE TABLE IF NOT EXISTS conversation_contexts (
    conversation_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    user_id TEXT,
    chat_id TEXT NOT NULL,
    chat_name TEXT,
    messages TEXT NOT NULL DEFAULT '[]',
    metadata TEXT DEFAULT '{}',
    mode TEXT DEFAULT 'active',
    mode_metadata TEXT DEFAULT '{}',
    keywords TEXT DEFAULT '[]',
    last_interaction TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
);

-- Indices for conversation_contexts
CREATE INDEX IF NOT EXISTS idx_conversation_contexts_user_id 
    ON conversation_contexts(user_id);
CREATE INDEX IF NOT EXISTS idx_conversation_contexts_chat_id 
    ON conversation_contexts(chat_id);
CREATE INDEX IF NOT EXISTS idx_conversation_contexts_type 
    ON conversation_contexts(type);
CREATE INDEX IF NOT EXISTS idx_conversation_contexts_mode 
    ON conversation_contexts(mode);


-- ============================================================================
-- SCHEMA SUMMARY
-- ============================================================================
-- 
-- Total Tables: 14 (15 with optional conversation_contexts)
-- 
-- CORE (3):
--   - user_profiles (14 fields)
--   - auth_nonces
--   - api_keys
--
-- CREDITS (3):
--   - user_credits
--   - credit_transactions
--   - usage_records
--
-- PAYMENTS (2):
--   - x402_payment_requests
--   - x402_access_log
--
-- CRYPTO (6):
--   - user_deposit_addresses
--   - crypto_payments
--   - pending_sweeps
--   - wallet_history
--
-- OPTIONAL (1):
--   - conversation_contexts (if chat features needed)
--
-- ============================================================================


