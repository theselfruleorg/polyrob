# Environment Configuration Files

_Last reviewed: 2026-06-22._

## CRITICAL: Environment Files Are Server-Managed

**DO NOT commit .env files to git!** They contain API keys, secrets, and server-specific configuration.

The deployment script now **preserves existing .env files** and only creates templates for initial deployment.

---

## File Locations

### Production Environment (Single Server Setup)
**Server:** <YOUR_SERVER_IP> (Hetzner Cloud CPX31, Helsinki hel1)
**Domain:** your-domain.example
**File:** `/opt/rob/config/.env.production`
**Status:** Manually maintained on server (perms: 0600, owner ubuntu)

**Note:** During beta, we use a single production server. No separate dev/prod servers.
Migrated from AWS EC2 eu-central-1 to Hetzner on 2026-05-17.

---

## Required Environment Variables

### Core Configuration
```bash
ENVIRONMENT=development|production
```

### API Configuration
```bash
API_HOST=127.0.0.1
API_PORT=9000
API_AUTH_TOKEN=secure-random-token
```

### Database & Storage
```bash
DATA_DIR=/opt/rob/data
CHARACTERS_DIR=/opt/rob/data/characters
KNOWLEDGE_DIR=/opt/rob/data/knowledge
CACHE_DIR=/opt/rob/data/cache
DB_PATH=/opt/rob/data/bot.db
CACHE_SIZE=1000
```

### Browser Configuration
```bash
PLAYWRIGHT_BROWSERS_PATH=/opt/rob/.cache/ms-playwright
PLAYWRIGHT_BROWSER_TIMEOUT=120000
BROWSER_DISABLE_SECURITY=true
BROWSER_HEADLESS=false
MAX_BROWSER_CONTEXTS=25
```

### Webview Configuration
```bash
WEBVIEW_HOST=0.0.0.0
WEBVIEW_PORT=3000
WEBVIEW_DOMAIN=your-domain.com
```

### Telemetry
```bash
TELEMETRY_ENABLED=false|true
TELEMETRY_LOG_LEVEL=info
TELEMETRY_DATA_DIR=/opt/rob/data/auto/telemetry
TELEMETRY_BUFFER_SIZE=500
TELEMETRY_BUFFER_TIMEOUT=60.0
```

### Logging
```bash
LOG_LEVEL=DEBUG|INFO|WARNING|ERROR
LOG_FORMAT="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
HTTP_LOG_FORMAT="%(asctime)s - %(name)s - HTTP %(method)s %(url)s %(status_code)d %(reason_phrase)s"
```

### API Keys (REPLACE WITH YOUR ACTUAL KEYS!)
```bash
# OpenAI
OPENAI_API_KEY=sk-proj-YOUR_KEY_HERE

# Anthropic
ANTHROPIC_API_KEY=sk-ant-YOUR_KEY_HERE

# Deepseek
DEEPSEEK_API_KEY=sk-YOUR_KEY_HERE

# Google
GEMINI_API_KEY=YOUR_KEY_HERE

# Meta
LLAMA_API_KEY=llm-YOUR_KEY_HERE
LLAMA_API_URL=https://api.llama-api.com

# Search
PERPLEXITY_API_KEY=pplx-YOUR_KEY_HERE

# Twitter
TWITTER_BEARER_TOKEN=YOUR_TOKEN_HERE
TWITTER_API_KEY=YOUR_KEY_HERE
TWITTER_API_SECRET_KEY=YOUR_SECRET_HERE
TWITTER_ACCESS_TOKEN=YOUR_TOKEN_HERE
TWITTER_ACCESS_TOKEN_SECRET=YOUR_SECRET_HERE
TWITTER_BOT_USER_ID=your_bot_id
TWITTER_BOT_USERNAME=your_username

# Memory backend (Pinecone retired — recall is local SQLite; see docs/CONFIGURATION.md)
MEMORY_BACKEND=sqlite          # sqlite (default) | local_vector | none

# Embedding
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIMENSION=384
```

### Session Management
```bash
SESSION_TTL_SECONDS=3600
MAX_SESSIONS_IN_MEMORY=100
SESSION_CLEANUP_INTERVAL=300
MAX_SESSIONS_PER_USER=10
```

### Memory System
```bash
HIERARCHICAL_MEMORY_ENABLED=true
COMPACTION_ENABLED=true
CONTEXT_SOFT_THRESHOLD=0.70
CONTEXT_HARD_THRESHOLD=0.85
MAX_RECENT_STEPS=20
TOOL_RESULT_MAX_AGE=10
```

### Auth & Security
```bash
ENABLE_AUTH=true
JWT_SECRET_KEY=GENERATE_SECURE_RANDOM_KEY_HERE
PAYMENT_MASTER_SEED=GENERATE_SECURE_RANDOM_SEED_HERE
JWT_ALGORITHM=HS256
JWT_EXPIRATION_HOURS=24
```

### Webhooks & URLs
```bash
WEBHOOK_URL=https://your-domain.com
WEBHOOK_PATH=/mvpbot
API_URL=https://your-domain.com
CORS_ALLOW_ORIGINS=https://your-domain.com,http://localhost:3000
```

### Uvicorn Settings
```bash
UVICORN_HOST=127.0.0.1
UVICORN_PORT=9000
UVICORN_WORKERS=1
UVICORN_LOG_LEVEL=info
UVICORN_ACCESS_LOG=true
UVICORN_RELOAD=false
```

### Rate Limiting
```bash
# Read by api/app.py (the API rate-limit middleware). Names are API_RATE_LIMIT_*.
API_RATE_LIMIT_RPM=60     # requests per minute (default 60)
API_RATE_LIMIT_RPH=1000   # requests per hour (default 1000)
API_RATE_LIMIT_BURST=10   # burst allowance (default 10)
```

> The `RATE_LIMIT_ENABLED` / `RATE_LIMIT_REQUESTS_PER_MINUTE` / `RATE_LIMIT_BURST`
> names previously listed here are NOT read anywhere in the code — removed.
> The `ENABLE_TELEGRAM_BOT` / `ENABLE_WEBVIEW` / `ENABLE_TASK_AGENT` /
> `ENABLE_MCP_TOOLS` "feature flags" were likewise dead (not read) — removed.

> **Flag SSOT:** `docs/CONFIGURATION.md` is the authoritative reference for POLYROB
> configuration flags. Verify against it (and the code) before adding env vars here.

---

## Deployment Script Behavior (FIXED)

### Before Fix (BROKEN):
```bash
# Used cat >> (APPEND) every deployment
cat >> /opt/rob/config/.env.$ENVIRONMENT << 'ENVEOF'
ENV=$ENVIRONMENT
WEBHOOK_URL=...
ENVEOF
```

**Result:** Duplicates added every deploy! ❌

### After Fix (CORRECT):
```bash
# Checks if file exists first
if [ -f /opt/rob/config/.env.$ENVIRONMENT ]; then
    echo "✓ Environment file exists - SKIPPING"
    # Validates required variables
else
    echo "Creating template for first deployment"
    cat > /opt/rob/config/.env.$ENVIRONMENT << 'ENVEOF'
    # ... comprehensive template ...
    ENVEOF
fi
```

**Result:** Existing files preserved! ✅

---

## Manual Environment File Management

### To View Current Config
```bash
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "cat /opt/rob/config/.env.production"
```

### To Edit Config
```bash
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "nano /opt/rob/config/.env.production"
```

### To Backup Config
```bash
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "cp /opt/rob/config/.env.production /opt/rob/config/.env.production.backup.\$(date +%Y%m%d_%H%M%S)"
```

### To Restore from Backup
```bash
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "cp /opt/rob/config/.env.production.backup.TIMESTAMP /opt/rob/config/.env.production"
```

---

## Security Best Practices

1. **Never commit .env files** to git
2. **Use strong random keys** for JWT_SECRET_KEY and PAYMENT_MASTER_SEED
3. **Rotate API keys** periodically
4. **Backup .env files** before making changes
5. **Different keys for dev/prod** - never use dev keys in production
6. **Limit access** - only admins should have access to .env files

---

## Troubleshooting

### Services won't start
Check environment file exists and has required variables:
```bash
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "ls -la /opt/rob/config/.env.production && cat /opt/rob/config/.env.production"
```

### Config seems corrupted
Check for duplicates:
```bash
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "sort /opt/rob/config/.env.production | uniq -d"
```

### Need to reset config
1. Backup first: `cp .env.production .env.production.backup`
2. Remove duplicates: `awk '!seen[$0]++' .env.production > .env.tmp && mv .env.tmp .env.production`
3. Verify: Check services restart successfully

---

## Summary

- ✅ Deploy script now PRESERVES existing .env files
- ✅ Never appends duplicates
- ✅ Validates required variables
- ✅ Creates template only for initial deployment
- ✅ Protected by comprehensive rsync exclude list

**Environment files are safe across deployments!**

