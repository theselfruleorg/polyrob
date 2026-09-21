# Task Agent HTTP API Documentation

_Last reviewed: 2026-09-21. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

> Paths below mirror `api/task_http_api.py` (router `prefix="/task"`, mounted under `/api` → app-level
> `/api/task/...`). Grep `@router.` in that file for the authoritative path list.

## Base URL
- Production: `https://your-domain.example/api/task`
- Local: `http://localhost:9000/api/task`

## Endpoints

### 1. Create and Start Session ✅
**POST** `/api/task/sessions`

Creates a new AutoV2 session and starts it running in the background.

#### Request Body
```json
{
  "task": "Your task description here",  // REQUIRED
  "session_id": "custom_session_id",    // Optional, auto-generated if not provided
  "model": "…",                         // Optional, see note below
  "provider": "…",                      // Optional, see note below
  "temperature": 0.0,                   // Optional, defaults to 0.0
  "use_vision": true,                   // Optional, defaults to true
  "max_steps": 50,                      // Optional, defaults to 50
  "tools": ["browser", "filesystem"],   // Optional, defaults to ["browser", "filesystem"]
  "tools_config": {}                    // Optional, tool-specific configuration
}
```

⚠️ A `user_id` in the body is **IGNORED**. The tenant is taken from the
authenticated request; trusting the payload let any authenticated caller create,
bill and recall memory as another tenant.

⚠️ `model`/`provider` have **no hardcoded default**. When omitted the session
resolves the operator's configured provider (`DEFAULT_PROVIDER`, else the first
provider with an API key). This document used to claim `gpt-5`/`openai`.

#### Response
```json
{
  "ok": true,
  "session_id": "37155a84-85fe-4456-bd05-187c44612c49",
  "task": "Test AutoV2. Calculate 2+2.",
  "status": "running",
  "model": "<the resolved model>",
  "tools": ["browser", "filesystem"],
  "webview_url": "https://your-domain.example/session/37155a84-85fe-4456-bd05-187c44612c49",
  "message": "Session created and started successfully",
  "error": null
}
```

#### Working Example
```bash
# From local machine (may have curl issues)
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> \
  "curl -X POST http://localhost:9000/api/task/sessions \
  -H 'Content-Type: application/json' \
  -d '{\"task\":\"Test AutoV2. Calculate 2+2.\",\"user_id\":\"test\"}' -s"

# OR save to file first
echo '{"task":"Your task here","user_id":"test"}' > /tmp/request.json
curl -X POST https://your-domain.example/api/task/sessions \
  -H "Content-Type: application/json" \
  -d @/tmp/request.json
```

### 2. Send User Message to Session
**POST** `/api/task/sessions/{session_id}/messages`

Send guidance or feedback to a running AutoV2 session. The session ID is in the URL path.

#### Request Body
```json
{
  "text": "Your message here",          // REQUIRED
  "kind": "guidance",                   // Optional, see the allow-list below
  "metadata": {}                        // Optional additional metadata
}
```

`kind` must be one of `answer`, `correction`, `feedback`, `guidance`,
`question`, `steer`, `user_message` (`ALLOWED_USER_MESSAGE_KINDS` in
`api/models.py`); anything else is **422**. ⚠️ The internal forged-turn kinds
`self_wake` and `delegation_result` are refused on purpose — they stamp the
turn as machine-originated, which is what stops it auto-activating a skill or
reaching a high-impact verb.

#### Response
```json
{
  "success": true,
  "message": "Message sent successfully",
  "metadata": {
    "session_id": "session-uuid"
  }
}
```

### 3. Session Control

#### Cancel Session
**POST** `/api/task/sessions/{session_id}/cancel`

> Note: there are no pause/resume routes.

### 4. Get Session Info
**GET** `/api/task/sessions/{session_id}`

### 5. List User Sessions
**GET** `/api/task/users/{user_id}/sessions`

### 6. Get Capabilities
**GET** `/api/task/capabilities`

### 7. Read a Workspace File
**GET** `/api/task/sessions/{session_id}/workspace/{path}`

Serves ONE file from the session workspace — the URI A2A artifacts point at.
Tenant-scoped (the caller must own the session), confined to the workspace
(`..`, absolute paths and symlinks are refused), and always sent as an
`attachment` with `nosniff`. 400 on a bad path, 403 on an escape or a symlink,
404 when the file is absent, 409 when another worker owns the session.

### 8. Server Metrics — ADMIN ONLY
**GET** `/api/task/metrics`

Requires an admin role. It reports `users.sessions_per_user` and
`users.top_users` — a roster of every tenant on the box — which was reachable
unauthenticated until 2026-09-21.

## Testing the Deployment

### Step 1: Create a Session
```bash
# Direct on server (WORKING)
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> \
  "curl -X POST http://localhost:9000/api/task/sessions \
  -H 'Content-Type: application/json' \
  -d '{\"task\":\"Calculate 2+2 and explain.\",\"user_id\":\"test\",\"max_steps\":5}' -s"
```

### Step 2: Check Logs
```bash
# View session logs
ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> \
  "sudo journalctl -u polyrob.service --since '2 minutes ago' | grep -E 'session_id|Step|Calculate'"
```

### Step 3: View in Browser
The response includes a `webview_url` you can open in your browser to watch the session progress:
```
https://your-domain.example/session/{session_id}
```

## Common Issues

1. **curl issues on macOS**: The curl command may have issues with special characters. Use SSH to run curl directly on the server or save JSON to a file first.

2. **Session not found**: A `GET /api/task/sessions/{id}` may return 404 for an unknown/expired session, or a 409 (with `owner_pid`) if the session is owned by another worker. Check logs if unsure.

3. **Service unavailable**: If you get 503 errors, the AutoV2 agent may not be initialized. Check service status:
   ```bash
   ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "sudo systemctl status polyrob.service"
   ```

## Deployment Verification

✅ **Successful Test Session Created**
- Session ID: `37155a84-85fe-4456-bd05-187c44612c49`
- Task: "Test AutoV2. Calculate 2+2."
- Status: Running
- Model: the operator's configured default
- Tools: browser, filesystem

The AutoV2 fixes have been successfully deployed and are working!