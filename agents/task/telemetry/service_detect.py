"""Which tool/service an action name belongs to — ONE detector.

Two full copies lived in ``agents/task/agent/service.py`` and
``agents/task/telemetry/formatters.py`` (with two ``KNOWN_TOOLS`` sets that
had to be kept equal by hand), plus two lazy re-export shims. This is the
single implementation; the old names stay as thin aliases.
"""
from __future__ import annotations

KNOWN_TOOLS = frozenset({
    'polymarket', 'mcp', 'twitter', 'email', 'perplexity',
    'filesystem', 'browser', 'collabland', 'alchemy', 'task',
})

_FILESYSTEM_KW = ('document', 'doc_', 'extract_text', 'process_', 'file',
                  'write_file', 'read_file', 'append_file', 'delete_file',
                  'create_directory', 'list_directory')
_BROWSER_KW = ('click', 'scroll', 'input', 'navigate', 'go_to', 'browse_to',
               'back', 'forward', 'reload', 'screenshot')
_NOT_A_TOOL_PREFIX = frozenset({'get', 'set', 'add', 'del', 'run'})


def detect_service_for_action(action_name: str) -> str:
    """Detect which service an action belongs to based on its name.

    1. A known tool prefix (``polymarket_get_markets`` -> ``polymarket``).
    2. Keyword detection for legacy, unprefixed actions.
    3. The first underscore-delimited token when it looks like a tool name.
    4. ``'default'``.
    """
    name = action_name.lower()
    for tool in KNOWN_TOOLS:
        if name.startswith(f"{tool}_"):
            return tool
    if 'perplexity' in name or 'search_web' in name:
        return 'perplexity'
    if any(kw in name for kw in _FILESYSTEM_KW):
        return 'filesystem'
    if any(kw in name for kw in _BROWSER_KW):
        return 'browser'
    if 'mcp' in name:
        return 'mcp'
    if '_' in name:
        potential_tool = name.split('_')[0]
        if len(potential_tool) >= 3 and potential_tool not in _NOT_A_TOOL_PREFIX:
            return potential_tool
    return 'default'
