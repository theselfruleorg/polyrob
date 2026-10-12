"""W11 — packs reach the host through ONE facade, :mod:`polyrob.sdk`.

1. A pack never imports a PRIVATE host name (a ``_name`` or a module under a
   ``_module``): the private surface is the host's to change. The facade gives
   each one a public, stable name.
2. A pack's direct import of any other host module is a SHRINK-ONLY allowlist
   row (``DIRECT_HOST_MODULES``): move a use onto ``polyrob.sdk`` and delete the
   row; a new host module needs a facade row, not an allowlist row.
3. The facade itself imports nothing at import time and every export resolves.
"""
import ast
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
HOST = {"cli", "core", "agents", "modules", "tools", "api", "utils", "cron", "surfaces",
        "webview", "migrations"}

#: Host modules packs still import directly (2026-10-10). SHRINK-ONLY.
DIRECT_HOST_MODULES = frozenset({
    "agents.task.constants",
    "core.autonomy_marker",
    "agents.task.goals.board",
    "agents.task.path",
    "api.dependencies",
    "core.autonomy_control",
    "core.config",
    "core.config_policy",
    "core.container",
    "core.credential_verdicts",
    "core.env",
    "core.event_kinds",
    "core.event_log",
    "core.exceptions",
    "core.exec_identity",
    "core.identity",
    "core.instance",
    "core.lazy_deps",
    "core.logging",
    "core.packs.spec",
    "tools.rails.verification",
    "core.rate_limit",
    "core.remedy",
    "core.runtime_paths",
    "core.security.encryption",
    "core.security.host_execution",
    "core.security.process_hardening",
    "core.security.workspace_io",
    "core.surfaces.attachments",
    "core.surfaces.envelopes",
    "core.surfaces.idempotency",
    "core.surfaces.owner_address",
    "core.surfaces.session_chat_registry",
    "core.surfaces.surface",
    "core.surfaces.user_delivery",
    "core.verbs",
    "core.wallet.authority",
    "core.wallet.factory",
    "core.wallet.submission_journal",
    "modules.database.connection",
    "tools.base_tool",
    "tools.browser.context",
    "tools.browser.launch_security",
    "tools.code_exec.backend",
    "tools.code_exec.backends.remote_sandbox",
    "tools.code_exec.env_policy",
    "tools.code_exec.pack_backends",
    "tools.controller.approval",
    "tools.controller.approval_interactive",
    "tools.controller.approval_queue",
    "agents.task.loop_types.controller_types",
    "tools.credential_cache",
    "tools.descriptors",
    "tools.email_providers.agentmail",
    "tools.mcp.security",
    "tools.oauth.file_store",
    "tools.shell.remote_executor",
    "tools.tool_disclosure",
    "utils.rate_limit_manager",
})


def _pack_imports():
    """(file, module, name) for every host import under packs/ (module + function level)."""
    out = []
    for path in sorted((_REPO / "packs").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(_REPO))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in HOST:
                    out.extend((rel, node.module, a.name) for a in node.names)
            elif isinstance(node, ast.Import):
                out.extend((rel, a.name, "*") for a in node.names
                           if a.name.split(".")[0] in HOST)
    return out


def _module_of(module, name):
    return f"{module}.{name}" if module in HOST else module


def test_no_pack_imports_a_private_host_name():
    private = [(f, m, n) for f, m, n in _pack_imports()
               if n.startswith("_") or any(p.startswith("_") for p in m.split("."))]
    assert not private, ("a pack imports a private host name — export it from "
                         f"polyrob/sdk.py under a public name instead: {private}")


def test_direct_host_imports_only_shrink():
    used = {_module_of(m, n) for _, m, n in _pack_imports()}
    new = sorted(used - DIRECT_HOST_MODULES)
    assert not new, ("a pack imports a host module directly: add the names to "
                     f"polyrob/sdk.py and import them from there: {new}")
    stale = sorted(DIRECT_HOST_MODULES - used)
    assert not stale, f"no pack imports these any more — delete the rows: {stale}"


def test_the_facade_imports_nothing_until_a_name_is_used():
    code = ("import sys, polyrob.sdk as s; "
            "host = [m for m in sys.modules if m.split('.')[0] in "
            "('tools', 'cron', 'surfaces', 'cli', 'agents', 'api')]; "
            "assert not host, host; print('ok')")
    proc = subprocess.run([sys.executable, "-c", code], cwd=_REPO, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0 and "ok" in proc.stdout, proc.stderr[-2000:]


def test_every_facade_export_resolves():
    import polyrob.sdk as sdk
    for name in sdk.__all__:
        assert getattr(sdk, name) is not None, name


def test_the_facade_sees_a_host_monkeypatch(monkeypatch):
    import polyrob.sdk as sdk
    import tools.controller.action_registration as ar
    monkeypatch.setattr(ar, "_is_forged_or_autonomous_turn", lambda *a: "patched")
    assert sdk.is_forged_or_autonomous_turn(None, None) == "patched"
