"""The runtime-sandbox contract, kept in production code by scan.

Under the agent units (``RestrictSUIDSGID=true``, separate ``ReadWritePaths`` bind
mounts, a private ``/tmp``, a setgid data home), proven under the unit on 2026-11-20:

1. ``shutil.copytree`` / ``copystat`` of any data-home dir is EPERM and
   ``shutil.move`` across two mounts falls back to it: use ``core.fs_copy``.
2. A ``tempfile`` call with no ``dir=`` lands in the unit's PRIVATE ``/tmp`` — a
   separate mount (rename into the data home: EXDEV), empty after a restart,
   invisible to the sandbox daemon, a small tmpfs on some boxes. Every such call
   passes ``dir=`` (``core.runtime_paths.scratch_root`` gives one) or is named below with why the
   private ``/tmp`` is right for it.
3. A literal ``"/tmp…"`` path is named below the same way.
4. A mode copied from a source (``stat.S_IMODE(...)``) and handed to
   ``chmod``/``fchmod``/``open``/``mkdir`` keeps setuid/setgid unless masked:
   EPERM under the unit. Mask it (``& 0o777``, ``core.fs_copy.perm_bits``).

The scan reads the AST (a multi-line ``dir=`` counts). ``devtools/`` (a developer
machine only) and tests are not scanned.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = ["agents", "api", "cli", "core", "cron", "migrations", "modules", "polyrob",
         "surfaces", "tools", "utils", "webview", "packs"]
SEAM = os.path.join("core", "fs_copy.py")
BANNED = re.compile(r"\bshutil\.(copytree|copystat|move)\s*\(|"
                    r"^\s*from\s+shutil\s+import\s+.*\b(copytree|copystat|move)\b", re.M)

_TEMPFILE_FNS = {"mkdtemp", "mkstemp", "mktemp", "NamedTemporaryFile", "TemporaryDirectory",
                 "TemporaryFile", "SpooledTemporaryFile", "gettempdir", "gettempdirb"}

#: ``path::function`` -> why the private /tmp is the right place. Small, short-lived,
#: read back by the same process, never renamed into the data home.
PRIVATE_TMP_OK = {
    "agents/task/gif_utils.py::_extract_screenshots_from_history": "a few PNG frames for one GIF",
    "api/request_limits.py::__call__": "a request body spool, read in the same request",
    "cli/commands/pack.py::pack_install": "an owner CLI clone; the install copies out",
    "cli/commands/pack.py::_install_from_index": "a one-line hashed requirements file",
    "cli/commands/pack.py::_install_reviewed_wheel": "an owner CLI wheel build dir",
    "cli/commands/profile_dist.py::install_profile": "a profile clone, copied out with core.fs_copy",
    "cli/commands/profile_dist.py::update_profile": "a profile clone, copied out with core.fs_copy",
    "cli/commands/skill_install.py::install_git": "a skill clone, copied out by install_local",
    "cli/commands/skill_install.py::install_url": "one SKILL.md, copied out by install_local",
    "core/runtime_paths.py::scratch_mkdtemp": "the fallback when TMPDIR is set or the data home holds no scratch",
    "core/lazy_deps.py::_constraint_args": "a pip constraints file (the pip TMPDIR is set apart)",
    "core/lazy_deps.py::_ensure_legacy": "one requirements file for pip",
    "modules/transcription/faster_whisper_transcriber.py::_transcribe_sync": "one voice note",
    "tools/document_parser.py::_parse_reserved": "the parser worker's empty cwd",
    "tools/code_exec/backends/local_subprocess.py::run": "an in-process run's own cwd",
    "tools/browser/downloads.py::save_download": "a capped (32 MiB) download, copied by bytes",
    "packs/singularity/polyrob_singularity/backend.py::_host_workdir": "a singularity run's cwd",
}

#: ``path::function`` -> why a literal /tmp path is right.
TMP_LITERAL_OK = {
    "core/owner_remedy.py::<module>": "a list of system dirs the agent must never offer",
    "core/container_hardening.py::hardening_flags": "the CONTAINER's own tmpfs",
    "tools/shell/remote_executor.py::backend_shell_executor": "a path on the REMOTE host",
    "tools/shell/remote_executor.py::control_dir_for": "this process's own ssh ControlMaster socket (re-made after a restart)",
    "tools/shell/executor.py::<module>": "a path INSIDE the session container",
    "core/box_parity.py::_tmp_fs": "READS which mount holds /tmp (the doctor's tmpfs check)",
    "tools/code_exec/tool_rpc.py::make_socket_dir": "the in-process fallback when the data-home path is too long",
}

_MODE_SINKS = {"chmod", "fchmod", "lchmod", "open", "mkdir", "makedirs"}


def _files():
    for root in ROOTS:
        for dp, dns, fs in os.walk(os.path.join(ROOT, root)):
            dns[:] = [d for d in dns if d not in ("tests", "__pycache__", "node_modules")]
            for f in fs:
                if f.endswith(".py"):
                    path = os.path.join(dp, f)
                    yield os.path.relpath(path, ROOT), path


class _Scan(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel = rel
        self.stack = []
        self.tempfile, self.literals, self.modes = [], [], []

    def _where(self):
        return f"{self.rel}::{self.stack[-1] if self.stack else '<module>'}"

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Constant(self, node):
        if isinstance(node.value, str) and (node.value == "/tmp" or node.value.startswith("/tmp/")):
            self.literals.append((self._where(), node.lineno))

    def visit_Call(self, node):
        fn = node.func
        if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "tempfile" and fn.attr in _TEMPFILE_FNS
                and not any(k.arg == "dir" for k in node.keywords)):
            self.tempfile.append((self._where(), node.lineno))
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name in _MODE_SINKS:
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if _unmasked_imode(arg):
                    self.modes.append((self._where(), node.lineno))
        self.generic_visit(node)


def _unmasked_imode(expr) -> bool:
    """``S_IMODE(...)`` reaching the sink with no ``& <const>`` that drops 0o6000."""
    if isinstance(expr, ast.Call):
        callee = expr.func.attr if isinstance(expr.func, ast.Attribute) else getattr(expr.func, "id", "")
        return callee == "S_IMODE"
    if isinstance(expr, ast.BinOp):
        if isinstance(expr.op, ast.BitAnd):
            for side in (expr.left, expr.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, int) \
                        and not side.value & 0o6000:
                    return False
        return _unmasked_imode(expr.left) or _unmasked_imode(expr.right)
    return False


def _scans():
    out = []
    for rel, path in _files():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        scan = _Scan(rel)
        scan.src = src
        scan.visit(ast.parse(src))
        out.append(scan)
    return out


SCANS = _scans()


def test_no_shutil_tree_copy_or_move_outside_the_seam():
    sites = []
    for scan in SCANS:
        if scan.rel == SEAM:
            continue
        for m in BANNED.finditer(scan.src):
            sites.append(f"{scan.rel}:{scan.src.count(chr(10), 0, m.start()) + 1}")
    assert not sites, (
        "shutil.copytree/copystat/move fail inside the hardened service (setgid data "
        "home + RestrictSUIDSGID, separate bind mounts); use core.fs_copy.copy_tree / "
        "move:\n  " + "\n  ".join(sites))


def test_every_tempfile_call_names_its_mount():
    bad = sorted(f"{w}:{n}" for s in SCANS for w, n in s.tempfile if w not in PRIVATE_TMP_OK)
    assert not bad, (
        "a tempfile call with no dir= lands in the unit's PRIVATE /tmp (separate mount, "
        "wiped on restart, invisible to other processes, a small tmpfs on some boxes). "
        "Pass dir= (core.runtime_paths.scratch_root / scratch_mkdtemp / sandbox_scratch_dir, or the "
        "target's own dir for an atomic write), or name it in PRIVATE_TMP_OK with why:\n  "
        + "\n  ".join(bad))


def test_no_stale_private_tmp_entries():
    seen = {w for s in SCANS for w, _ in s.tempfile}
    stale = sorted(set(PRIVATE_TMP_OK) - seen)
    assert not stale, f"PRIVATE_TMP_OK names a call that no longer exists: {stale}"


def test_every_literal_tmp_path_is_named():
    bad = sorted(f"{w}:{n}" for s in SCANS for w, n in s.literals if w not in TMP_LITERAL_OK)
    assert not bad, ("a literal /tmp path is the unit's private /tmp; use core.runtime_paths.scratch_root, "
                     "or name it in TMP_LITERAL_OK with why:\n  " + "\n  ".join(bad))


def test_a_copied_mode_never_carries_setuid_or_setgid():
    bad = sorted(f"{w}:{n}" for s in SCANS for w, n in s.modes)
    assert not bad, ("stat.S_IMODE(...) keeps setuid/setgid; handed to chmod/open/mkdir it "
                     "is EPERM under RestrictSUIDSGID. Mask it (& 0o777, "
                     "core.fs_copy.perm_bits):\n  " + "\n  ".join(bad))
