# Webview Package - Real-Time Session Interface

_Last reviewed: 2026-06-30. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

## Overview

The `webview` package provides a real-time web interface for monitoring and interacting with POLYROB task automation sessions. Built with FastAPI and Socket.IO, it offers live session views, continuous chat functionality, workspace file management, and usage statistics.

## Architecture

- **FastAPI**: Web framework for HTTP endpoints (served via Uvicorn)
- **Socket.IO**: Real-time bidirectional communication (`python-socketio` ASGI app)
- **Jinja2**: Template rendering
- **Uvicorn**: ASGI server

## Package Structure

```
webview/
├── server.py               # Main FastAPI + Socket.IO server
├── webgate.py              # Single-user vs multitenant config (mode flag + bind/ownership)
├── pages.py                # Webgate v1 read-only pages (Memory/Autonomy/Identity/System)
├── stats_service.py        # Usage statistics service
├── server_launcher.py      # Server launcher utility
├── repair_sessions.py      # Session repair utility
├── deploy.sh               # Webview deployment script
├── webview.service         # Systemd service definition
│
├── templates/              # Jinja2 HTML templates
│   ├── layout.html         # Base layout
│   ├── index.html          # Home/dashboard
│   ├── session.html        # Session view
│   ├── profile.html        # User profile
│   ├── signin.html         # Sign-in page
│   ├── sidebar.html        # Sidebar component
│   └── error.html          # Error page
│
└── static/                 # Frontend assets
    ├── css/
    │   ├── style.css       # Main styles
    │   ├── chat.css        # Chat interface styles
    │   └── config-panel.css # Config panel styles
    ├── js/
    │   ├── index.js        # Main JavaScript
    │   ├── session.js      # Session management
    │   ├── chat.js         # Chat functionality
    │   ├── workspace.js    # Workspace file viewer
    │   ├── stats.js        # Statistics display
    │   └── ...             # Other modules
    └── img/
        └── favicon.ico
```

## Main Server (`server.py`)

### Core Features

**Session Management**:
- Create, view, pause, resume, cancel sessions
- Real-time session state streaming via Socket.IO
- Session history and telemetry viewing

**Continuous Chat**:
- Send messages to running sessions
- Human-in-the-loop interaction
- Message queue with rate limiting

**Workspace Viewer**:
- Browse session workspace files
- View file contents
- Download artifacts

**Authentication**:
- JWT-based authentication
- Wallet authentication (SIWE)
- Session-based auth with cookies

### Key Endpoints

#### Pages
```
GET /                       # Home/dashboard
GET /session/<session_id>   # Session view page
GET /profile                # User profile page
GET /signin                 # Sign-in page
```

#### Session API
```
GET  /api/sessions                      # List user sessions
GET  /api/session/<id>/state            # Get session state
GET  /api/session/<id>/feed             # Get session feed/events
POST /api/session/<id>/pause            # Pause session
POST /api/session/<id>/resume           # Resume session
POST /api/session/<id>/cancel           # Cancel session
```

#### Continuous Chat API
```
GET  /api/session/<id>/queue-status     # Get message queue status
POST /api/session/<id>/messages         # Send message to session
GET  /api/session/<id>/messages         # Get message history
```

#### Workspace API
```
GET /api/session/<id>/workspace              # List workspace files
GET /api/session/<id>/workspace/<path>       # Get file content
GET /api/session/<id>/workspace/<path>/download  # Download file
```

#### Screenshots
```
GET /api/session/<id>/screenshots            # List screenshots
GET /api/session/<id>/screenshot/<filename>  # Get screenshot
GET /api/session/<id>/screenshot/latest      # Get latest screenshot
```

#### Statistics
```
GET /api/stats                          # Get user statistics
GET /api/stats/usage                    # Get usage breakdown
```

### Socket.IO Events

**Client → Server**:
```javascript
socket.emit('join_session', { session_id: 'xxx' })
socket.emit('leave_session', { session_id: 'xxx' })
socket.emit('subscribe_feed', { session_id: 'xxx' })
```

**Server → Client**:
```javascript
socket.on('session_update', (data) => { ... })
socket.on('feed_event', (data) => { ... })
socket.on('screenshot', (data) => { ... })
socket.on('session_complete', (data) => { ... })
socket.on('error', (data) => { ... })
```

## Stats Service (`stats_service.py`)

Provides user usage statistics and analytics.

**Features**:
- Session count and duration tracking
- Token usage aggregation
- Cost calculation
- Time-based analytics

**API**:
```python
class StatsService:
    async def get_user_stats(self, user_id: str) -> Dict:
        """Get comprehensive user statistics"""
    
    async def get_usage_breakdown(
        self, user_id: str, 
        start_date: datetime, 
        end_date: datetime
    ) -> Dict:
        """Get usage breakdown by time period"""
    
    async def get_session_stats(self, session_id: str) -> Dict:
        """Get statistics for specific session"""
```

## Webgate Mode (`webgate.py`)

`webview/` was built multitenant-first (JWT/SIWE auth, ownership, profile/billing/admin
pages, bound on `0.0.0.0`). The **primitive** is the opposite: a single-user, local-first
webgate (loopback bind, no auth, no admin pages, every session owned by the local owner).
Multitenant is the layer-on-top, gated by ONE flag. `webgate.py` is the single source of
truth for that flag and the bind/ownership decisions derived from it. It is consulted at
the seam points in `server.py` (auth-middleware short-circuit, ownership short-circuit,
auth-router and page mount-gates) and in `server_launcher.py` (bind host/port).

Flags are read directly from `os.environ` here — never via `BotConfig.get(...)`, which is a
`getattr` that silently returns the default (AGENTS.md landmine).

```python
is_multitenant() -> bool   # WEBGATE_MULTITENANT (default OFF → single-user local-first)
bind_host() -> str         # multitenant → 0.0.0.0, single-user → 127.0.0.1
                           #   (WEBGATE_HOST / WEBVIEW_HOST override always wins)
bind_port() -> int         # WEBGATE_PORT / WEBVIEW_PORT (default 5050)
local_owner_id() -> str    # bound owner principal, else POLYROB_LOCAL_OWNER,
                           #   else the instance id (defaults to "rob")
```

## Webgate v1 Pages (`pages.py`)

Four single-user, **read-only** pages POLYROB previously lacked — Memory, Autonomy,
Identity, and System — exposed as a FastAPI `APIRouter` that `server.py` mounts (fail-open:
a mount failure never breaks boot). Each page is one Jinja template plus one JSON read
endpoint; the template renders and the data is fetched client-side from the JSON endpoint.

Every endpoint **reuses the existing service** rather than building a second source of
truth, and is fail-open (a missing provider / disabled flag / read error degrades to an
empty result, never a 500). Because web requests have no agent `execution_context`, the
endpoints call the services directly rather than via the agent tool-action path.

| Page | HTML route | JSON endpoint | Backing service |
|------|-----------|---------------|-----------------|
| Memory | `GET /memory` | `GET /api/webgate/memory?q=&limit=` | active `MemoryProvider.search()` (browse when `q` empty, discover when set) |
| Autonomy | `GET /autonomy` | `GET /api/webgate/goals`, `GET /api/webgate/cron` | `GoalBoard.list()` + `CronService.list_jobs()` (gated by `goals_enabled()` / cron-enabled) |
| Identity | `GET /identity` | `GET /api/webgate/identity` | `core/instance.py` SOUL (`load_self_context`) + SELF (`load_self_doc`) — read-only; editing stays the owner-gated `self_context_manage` action |
| System | `GET /system` | `GET /api/webgate/doctor` | `doctor_report()` (same checks as `polyrob doctor`), plus instance id / version / provider+model / memory backend |

All recall and listings are scoped to `webgate.local_owner_id()` (the single owner).

## Configuration

### Environment Variables

```bash
# Server settings
WEBVIEW_HOST=0.0.0.0
WEBVIEW_PORT=3000          # code default in server_launcher.py (the production systemd unit overrides to 5050)
WEBVIEW_DOMAIN=your-polyrob-host.example

# Socket.IO settings
WEBVIEW_WS_URL=wss://your-polyrob-host.example/socket.io

# Authentication
JWT_SECRET_KEY=your-secret-key
ENABLE_AUTH=true

# API connection
API_URL=http://localhost:9000

# Logging
WEBVIEW_LOG_LEVEL=info     # passed to Uvicorn as --log-level
```

> Port note: `server_launcher.py` defaults to **3000** (`WEBVIEW_PORT`), but the production
> systemd unit sets `WEBVIEW_PORT=5050`, which is why the nginx/proxy examples below use 5050.

### Dependencies

There is no `webview/requirements.txt`. The webview's runtime dependencies (FastAPI +
Uvicorn + Socket.IO, not Flask) ship as the **`server` extra** of the project's root
`pyproject.toml` (`[project.optional-dependencies]`):
```
pip install -e ".[server]"
```
which installs:
```
fastapi==0.123.9
starlette==0.50.0
uvicorn[standard]==0.38.0
Jinja2==3.1.6
python-socketio>=5.11.0
python-multipart>=0.0.6
watchfiles>=1.0.0
```

## Running the Server

### Development
```bash
cd webview
python server.py
# or
python server_launcher.py
```

### Production (Systemd)
```bash
# Install service
sudo cp webview.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable webview
sudo systemctl start webview

# Check status
sudo systemctl status webview

# View logs
sudo journalctl -u webview -f
```

## Frontend Structure

### Templates

**`layout.html`**: Base template with common structure
- Navigation bar
- Sidebar inclusion
- Flash messages
- Footer

**`session.html`**: Main session view
- Screenshot display
- Browser state panel
- Chat interface
- Workspace viewer
- Telemetry/events feed

**`profile.html`**: User profile
- Account information
- Usage statistics
- Credit balance
- Session history

### JavaScript Modules

**`session.js`**: Session state management
```javascript
// Auto-refresh session state
setInterval(refreshSessionState, 2000);

// Socket.IO subscription
socket.emit('join_session', { session_id });
socket.on('session_update', handleUpdate);
```

**`chat.js`**: Continuous chat interface
```javascript
// Send message to session
async function sendMessage(text) {
    const response = await fetch(`/api/session/${sessionId}/messages`, {
        method: 'POST',
        body: JSON.stringify({ text })
    });
}
```

**`workspace.js`**: File browser
```javascript
// Load workspace files
async function loadWorkspace() {
    const files = await fetch(`/api/session/${sessionId}/workspace`);
    renderFileTree(files);
}
```

## Integration with Main API

The webview connects to the main POLYROB API (port 9000) for:
- Session creation and management
- Authentication verification
- Credit balance queries

```python
# In server.py
async def get_session_from_api(session_id: str):
    response = await http_client.get(
        f"{API_URL}/api/task/session/{session_id}"
    )
    return response.json()
```

## Nginx Configuration

See `deployment/nginx.conf` for production setup:
```nginx
# WebView routes
location / {
    proxy_pass http://localhost:5050;
}

# Socket.IO
location /socket.io/ {
    proxy_pass http://localhost:5050;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}

# Session API (webview handles these)
location /api/session/ {
    proxy_pass http://localhost:5050;
}
```

## Session Repair Utility (`repair_sessions.py`)

Utility for repairing broken or stuck sessions:

```bash
python repair_sessions.py --session-id <id> --action reset
python repair_sessions.py --user-id <id> --action cleanup-stale
```

## Best Practices

### Development
1. Use `eventlet` for async support in development
2. Enable debug mode for auto-reload
3. Use browser dev tools for Socket.IO debugging

### Production
1. Run behind nginx reverse proxy
2. Use systemd for process management
3. Configure appropriate worker count
4. Enable access logging

### Security
1. Always validate session ownership
2. Sanitize file paths in workspace viewer
3. Rate limit chat messages
4. Verify JWT tokens on all authenticated endpoints

## Troubleshooting

### Socket.IO Connection Issues
```bash
# Check if server is listening
netstat -tlnp | grep 5050

# Check nginx websocket proxy
curl -i -N -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  https://your-domain/socket.io/
```

### Session Not Updating
```bash
# Check session state file
ls -la data/task/*/session_state.json

# Check feed events
ls -la data/task/*/feed/
```

### Authentication Errors
```bash
# Verify JWT secret matches main API
echo $JWT_SECRET_KEY

# Check cookie settings
# Browser → DevTools → Application → Cookies
```

