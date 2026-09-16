"""Harness-owned timing receipts; never infer global order from agent iteration."""
import os
import time
import uuid

# Monotonic times are comparable only within this process incarnation. Persisted
# receipts from another worker/restart remain explicitly incomparable.
CLOCK_ID = uuid.uuid4().hex


def stamp_receipt(result, *, started_ns, context):
    workspace = getattr(context, "workspace_dir", None)
    receipt = {
        "clock_id": CLOCK_ID,
        "started_ns": started_ns,
        "finished_ns": time.monotonic_ns(),
        "workspace": os.path.realpath(workspace) if workspace else None,
        "session_id": getattr(context, "session_id", None),
        "ok": result is not None and not bool(getattr(result, "error", None)),
    }
    result.metadata = {**(getattr(result, "metadata", None) or {}), "execution_receipt": receipt}
    return result
