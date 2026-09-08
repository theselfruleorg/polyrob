"""Child-process environment policy for stdio MCP servers (H2, audit 2026-08-22).

``MCPClient.connect`` used to do ``{**os.environ, **(self.env or {})}``, handing
every stdio MCP server the whole process environment — including
``AGENT_WALLET_MASTER_SEED``, which controls the treasury. A hostile or
compromised MCP package is the entire threat model for ``mcp_install``.

This is deliberately NOT ``tools/code_exec/env_policy.py::build_child_env``.
That one also strips secret-named keys from the caller's ``extra``, which is
correct for agent-authored code but wrong here: ``config/mcp_config.json``
legitimately carries ``MCP_GATEWAY_TOKEN`` / ``ANYSITE_JWT`` through exactly that
channel (``tools/mcp/config.py`` resolves the ``${VAR}`` placeholders). The
operator wrote that file; the agent did not write ``os.environ``.

The rule: **ambient inheritance is denied by default; explicit configuration is
honored.**
"""
from __future__ import annotations

import os
from typing import Dict, Optional

#: The ONLY host env vars a stdio MCP child inherits. These are what a
#: node/python/uvx launcher needs to find its interpreter and cache; nothing here
#: carries a credential. POSIX + Windows, because `npx` fails without the Windows
#: four.
MCP_ENV_ALLOWLIST = frozenset({
    # POSIX process basics
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "TERM",
    "PWD", "SHELL", "USER", "LOGNAME",
    # Runtime lookup paths for the common MCP launchers
    "NODE_PATH", "NPM_CONFIG_PREFIX", "NVM_DIR", "XDG_CACHE_HOME",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME",
    "PYTHONHASHSEED", "VIRTUAL_ENV", "UV_CACHE_DIR",
    # Windows — `npx`/`uvx` do not start without these
    "SystemRoot", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
    "PROGRAMDATA", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
    "NUMBER_OF_PROCESSORS", "OS", "WINDIR",
})


def build_mcp_child_env(configured: Optional[Dict[str, object]] = None) -> Dict[str, str]:
    """Environment for a stdio MCP subprocess.

    1. Inherit ONLY :data:`MCP_ENV_ALLOWLIST` host vars that are actually set.
    2. Overlay ``configured`` verbatim — the operator's ``mcp_config.json`` values,
       secrets included, stringified. A configured key WINS over an inherited one.

    No secret-name filtering is applied to ``configured``: that channel is the
    sanctioned way to give a server its credential, and filtering it would break
    every authenticated MCP server.
    """
    env: Dict[str, str] = {k: os.environ[k] for k in MCP_ENV_ALLOWLIST
                           if k in os.environ}
    for key, value in (configured or {}).items():
        if value is None:
            continue
        env[str(key)] = str(value)
    return env
