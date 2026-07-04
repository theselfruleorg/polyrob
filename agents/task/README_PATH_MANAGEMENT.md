# Task Path Management System

## Overview

The Task path management system provides a centralized way to handle file paths across the entire codebase. This ensures consistency, prevents path-related bugs, and simplifies multiuser support.

## Key Components

- **PathManager**: The central class for all path operations, accessed via the `pm()` singleton.
- **Session Directories**: Structured directories for each session with standardized subdirectories.

## Usage

Always use the centralized path manager, which is accessed through the `pm()` function:

```python
from agents.task.path import pm

# Clean a session ID to remove any unsafe characters
clean_id = pm().clean_session_id(session_id)

# Get the base directory for a session
session_dir = pm().get_session_root(session_id)

# Get subdirectories
workspace_dir = pm().get_workspace_dir(session_id)
screenshots_dir = pm().get_screenshots_dir(session_id)
logs_dir = pm().get_logs_dir(session_id)
data_dir = pm().get_data_dir(session_id)
telemetry_dir = pm().get_telemetry_dir(session_id)
history_dir = pm().get_history_dir(session_id)

# Create a file path within a session subdirectory
file_path = pm().create_file_path(session_id, "results", "output.json")
```

## Directory Structure

Each session gets its own directory with subdirectories:

```
data/task/
└── {user_id}/
    └── {session_id}/
        ├── workspace/   # For files created by the agent (user-facing)
        ├── feed/        # For message feed (real-time events)
        ├── screenshots/ # For saved screenshots
        ├── logs/        # For log files
        ├── artifacts/   # For build outputs, downloads
        ├── memory/      # For conversation history
        └── data/        # For structured data (internal, not user-facing)
            ├── telemetry/   # For telemetry events
            ├── history/     # For agent execution history
            └── llm_usage/   # For LLM usage tracking
```

**Note:** The structure no longer includes a `sessions/` subdirectory. Sessions are stored directly under the user directory.

**Key Distinction:**
- `workspace/` = Files FOR the user (user-facing output)
- `data/` = Internal agent tracking (not user-facing)

## Best Practices

1. **Always use the path manager**: Never construct paths manually using string concatenation or `os.path.join()`.
2. **Clean session IDs**: Always use `pm().clean_session_id()` when handling user-provided session IDs.
3. **Use standard subdirectories**: Stick to the standard subdirectories (`workspace`, `feed`, `screenshots`, `logs`, `data`).
4. **Avoid hardcoded paths**: Don't hardcode paths like `data/task/` - the base directory could change. Always use PathManager methods.

## For Advanced Use Cases

The PathManager singleton is configured automatically. For testing, you can create temporary directories within the standard structure.

## Path Manager API Reference

| Method | Description |
|--------|-------------|
| `pm().clean_session_id(session_id)` | Sanitizes session ID |
| `pm().get_session_root(session_id)` | Gets the session's root directory |
| `pm().get_workspace_dir(session_id)` | Gets the session's workspace directory |
| `pm().get_screenshots_dir(session_id)` | Gets the session's screenshots directory |
| `pm().get_logs_dir(session_id)` | Gets the session's logs directory |
| `pm().get_data_dir(session_id)` | Gets the session's data directory |
| `pm().get_telemetry_dir(session_id)` | Gets the telemetry subdirectory |
| `pm().get_history_dir(session_id)` | Gets the browser history subdirectory |
| `pm().create_file_path(session_id, subdir, filename)` | Creates a file path in a subdirectory |

## Migration Guide

### Using the Centralized PathManager

Always use the PathManager singleton through `pm()`:

```python
from agents.task.path import pm

# Clean session IDs
clean_id = pm().clean_session_id(session_id)

# Get directories
workspace = pm().get_workspace_dir(clean_id)
telemetry = pm().get_telemetry_dir(clean_id)

# Create file paths
file_path = pm().create_file_path(clean_id, "workspace", "output.txt")
```

### Avoid Direct Path Construction

Never construct paths manually:

```python
# Wrong - hardcoded path
path = f"data/task/{user_id}/{session_id}/workspace/file.txt"

# Right - use PathManager
path = pm().create_file_path(session_id, "workspace", "file.txt", user_id=user_id)

# Even simpler
workspace_dir = pm().get_workspace_dir(session_id, user_id)
path = workspace_dir / "file.txt"
```

## Thread Safety and Locking

PathManager uses file locks to prevent race conditions during directory creation, making it safe for multi-threaded and multi-process environments.

## Testing

Run the path management tests:
```
pytest agents/task/agent/tests.py::TestPathManagement
```

## Troubleshooting

If you see "Cannot access PathManager to determine proper data paths" errors, ensure:

1. You have initialized SessionManager before using path functions
2. Your code does not have circular imports
3. You are using path_utils helpers instead of direct string concatenation 