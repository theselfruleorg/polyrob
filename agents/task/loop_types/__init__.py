"""The agent loop's own data types (W7).

``ActionResult`` and the action-model views, the per-call execution context, and
the browser/DOM state views. The loop (``agents.task.agent``) builds on them at
module level, and the tools produce them — so they sit in the agents tier and the
tools import them downward. Moved from ``tools/controller/{types,registry/views,
execution_context}.py``, ``agents/task/loop_types/browser_views.py`` and ``tools/dom/{views,
history_tree_processor/*}.py``. Keep this file free of imports.
"""
