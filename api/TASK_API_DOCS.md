# Task Agent HTTP API Documentation

_Last reviewed: 2026-06-22. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

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
  "user_id": "user_identifier",         // Optional, defaults to "_anonymous_"
  "session_id": "custom_session_id",    // Optional, auto-generated if not provided
  "model": "gpt-5",                   // Optional, defaults to "gpt-5"
  "provider": "openai",                 // Optional, defaults to "openai"
  "temperature": 0.0,                   // Optional, defaults to 0.0
  "use_vision": true,                   // Optional, defaults to true
  "max_steps": 50,                      // Optional, defaults to 50
  "tools": ["browser", "filesystem"],   // Optional, defaults to ["browser", "filesystem"]
  "tools_config": {}                    // Optional, tool-specific configuration
}
```

#### Response
```json
{
  "ok": true,
  "session_id": "37155a84-85fe-4456-bd05-187c44612c49",
  "task": "Test AutoV2. Calculate 2+2.",
  "state": "running",
  "status": "running",
  "model": "gpt-5",
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
  "kind": "guidance",                   // Optional: "guidance" or "feedback"
  "metadata": {}                        // Optional additional metadata
}
```

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
  "sudo journalctl -u rob.service --since '2 minutes ago' | grep -E 'session_id|Step|Calculate'"
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
   ssh -i <YOUR_SSH_KEY> root@<YOUR_SERVER_IP> "sudo systemctl status rob.service"
   ```

## Deployment Verification

✅ **Successful Test Session Created**
- Session ID: `37155a84-85fe-4456-bd05-187c44612c49`
- Task: "Test AutoV2. Calculate 2+2."
- Status: Running
- Model: gpt-5
- Tools: browser, filesystem

The AutoV2 fixes have been successfully deployed and are working!