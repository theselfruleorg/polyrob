"""W11 — packs reach the host through ONE facade, :mod:`polyrob.sdk`.

1. A pack never imports a PRIVATE host name (a ``_name`` or a module under a
   ``_module``): the private surface is the host's to change. The facade gives
   each one a public, stable name.
2. A pack's direct import of any other host module is a SHRINK-ONLY allowlist
   row (``DIRECT_HOST_MODULES``): move a use onto ``polyrob.sdk`` and delete the
   row; a new host module needs a facade row, not an allowlist row.
3. The facade itself imports nothing at import time and every export resolves.
4. (125 S28) The scan sees the composition root (``polyrob.app``), an
   ``importlib.import_module("…")`` / ``__import__("…")`` literal and a
   ``"module:attr"`` string row as host imports too; and a pack never imports
   another pack.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
HOST = {"cli", "core", "agents", "modules", "tools", "api", "utils", "cron", "surfaces",
        "webview", "migrations", "polyrob.app"}


def _is_host(module: str) -> bool:
    """A host module: its top package is a host tier, or it sits under a dotted
    host prefix (``polyrob.app`` — the composition root; ``polyrob.sdk`` is not host)."""
    return any(module == h or module.startswith(h + ".") for h in HOST)


#: A ``"pkg.module:attr"`` string (a registry row importlib resolves at runtime).
_STRING_REF_RE = re.compile(r"""["']([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+):[A-Za-z_]\w*["']""")
_DYNAMIC_IMPORTERS = {"import_module", "__import__"}

#: Host modules packs still import directly (2026-10-10). SHRINK-ONLY.
DIRECT_HOST_MODULES = frozenset({
    "agents.task.constants",
    "core.autonomy_marker",
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
    "utils.rate_limit_manager",
})


def _pack_files():
    for path in sorted((_REPO / "packs").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield str(path.relative_to(_REPO)), path


def _dynamic_targets(tree):
    """Literal module names handed to ``importlib.import_module`` / ``__import__``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        arg = node.args[0]
        if name in _DYNAMIC_IMPORTERS and isinstance(arg, ast.Constant) \
                and isinstance(arg.value, str):
            yield arg.value


def _pack_imports():
    """(file, module, name) for every host import under packs/ (module + function
    level, dynamic literals and ``"module:attr"`` strings included)."""
    out = []
    for rel, path in _pack_files():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if _is_host(node.module):
                    out.extend((rel, node.module, a.name) for a in node.names)
            elif isinstance(node, ast.Import):
                out.extend((rel, a.name, "*") for a in node.names if _is_host(a.name))
        out.extend((rel, m, "*") for m in _dynamic_targets(tree) if _is_host(m))
        out.extend((rel, m, "*") for m in _STRING_REF_RE.findall(text) if _is_host(m))
    return out


def _module_of(module, name):
    return f"{module}.{name}" if module in HOST and name != "*" else module


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


def _pack_packages():
    """{distribution dir: its top package} for every first-party pack."""
    return {d.name: p.name for d in (_REPO / "packs").iterdir() if d.is_dir()
            for p in d.glob("polyrob_*") if p.is_dir()}


def test_no_pack_imports_another_pack():
    """A pack reaches the host through polyrob.sdk and NOTHING else: one pack
    importing another couples two optional installs (a disabled or absent pack
    breaks the other)."""
    packages = set(_pack_packages().values())
    crossed = []
    for rel, path in _pack_files():
        own = next((p for d, p in _pack_packages().items() if rel.startswith(f"packs/{d}/")), None)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = set(_dynamic_targets(tree)) | set(_STRING_REF_RE.findall(path.read_text()))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module)
            elif isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
        for m in mods:
            top = m.split(".")[0]
            if top in packages and top != own:
                crossed.append((rel, m))
    assert not crossed, f"a pack imports another pack: {sorted(crossed)}"


def test_the_scan_sees_dynamic_and_string_host_references():
    tree = ast.parse("import importlib\nimportlib.import_module('core.env')\n"
                     "__import__('tools.base_tool')\nx = 'polyrob.app.bootstrap:f'\n")
    assert set(_dynamic_targets(tree)) == {"core.env", "tools.base_tool"}
    assert _STRING_REF_RE.findall("x = 'polyrob.app.bootstrap:f'") == ["polyrob.app.bootstrap"]
    assert _is_host("polyrob.app.bootstrap") and not _is_host("polyrob.sdk")
