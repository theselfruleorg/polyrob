"""Two ratchets for 067 P2.

1. The signer invariant: the process that holds the seed never enumerates or
   imports pack code — no module of ``core/signer`` nor the signer entry imports
   ``core.packs`` (source scan), and importing them pulls none in (fresh process).
2. Core never imports a pack by name: no shipped module outside ``packs/``
   imports a ``polyrob_*`` package (the loader uses entry points). The one
   pre-067 edge is allowlisted and may only shrink.
"""
import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SIGNER_FILES = sorted((REPO / "core" / "signer").glob("*.py")) + [
    REPO / "tools" / "defi" / "signer_entry.py"]
SOURCE_DIRS = ("core", "modules", "agents", "tools", "api", "cli", "surfaces", "webview",
               "cron", "utils")
#: (file, package). Shrink-only: 050's agent_nft surface predates the loader (WS-B moves it).
#: 069 v4 renamed the package; the second row is its legacy-name fallback (drop it when the
#: package has renamed).
ALLOWED_PACK_IMPORTS = frozenset({("tools/agent_nft/tool.py", "polyrob_drop"),
                                  ("tools/agent_nft/tool.py", "polyrob_desk")})


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module


def test_signer_sources_never_import_core_packs():
    bad = [(p.relative_to(REPO).as_posix(), m) for p in SIGNER_FILES for m in _imports(p)
           if m == "core.packs" or m.startswith("core.packs.")]
    assert not bad, f"the signer must never load packs: {bad}"


def test_importing_the_signer_pulls_no_pack_module():
    code = ("import sys, core.signer.server, core.signer.runtime, core.signer.client, "
            "tools.defi.signer_entry\n"
            "print(sorted(m for m in sys.modules if m.startswith('core.packs') "
            "or m.startswith('polyrob_')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True,
                         text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"


def test_no_module_outside_packs_imports_a_pack_package():
    found = set()
    for top in SOURCE_DIRS:
        for path in (REPO / top).rglob("*.py"):
            for mod in _imports(path):
                if mod.split(".")[0].startswith("polyrob_"):
                    found.add((path.relative_to(REPO).as_posix(), mod.split(".")[0]))
    new = found - ALLOWED_PACK_IMPORTS
    assert not new, f"import a pack through its entry point, never by name: {sorted(new)}"
    stale = ALLOWED_PACK_IMPORTS - found
    assert not stale, f"allowlisted edge(s) gone — delete the row(s): {sorted(stale)}"
