"""Back-compat shim (R-4, 2026-07-17): canonical home is core.security.secret_guard.

Kept so existing importers and monkeypatch targets (e.g. tests patching
``agents.task.agent.core.secret_guard.is_secret_path``) keep working. New code
imports from ``core.security.secret_guard``.
"""
from core.security.secret_guard import *  # noqa: F401,F403
