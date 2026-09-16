"""R15: the REPL pipe rig (043 A12/A22) — a recovered/provoked provider failure
must stay terse on stderr, not dump a multi-hundred-line wall.

Background: a prod 401 that a fallback provider then answered still spilled
~190 lines to the REPL's stderr — mostly full Python tracebacks
(``logger.error(..., exc_info=True)`` deep in the LLM stack) plus a
``core/security_logging_filter.py`` bug where the generic "base64-looking
string" legacy pattern ate long filesystem-path segments, corrupting the few
lines that DID survive.

Fixes (A12, across three review rounds):
1. ``cli/commands/chat.py`` now calls ``cli.ui.log_squelch.apply_single_line_
   errors()`` right after lifting the bootstrap ``logging.disable(NOTSET)``,
   mirroring ``cli/commands/run.py`` — every root stream handler's formatter
   drops ``exc_info``/``exc_text``/``stack_info`` so a run-time error stays one
   line instead of a full traceback.
2. ``core/security_logging_filter.py``'s generic base64-blob LEGACY_PATTERN
   gained lookarounds (still including ``/`` in its character class — a real
   secret can contain ``/`` internally, so dropping it from the class would
   silently stop redacting a real token) so a filesystem path is never
   mistaken for a redactable token.
3. Per-attempt logs in the LLM executor's retry/fallback cascade
   (``modules/llm/openai_client.py``, ``modules/llm/adapters.py``,
   ``agents/task/agent/core/next_action_internal.py``) were downgraded
   ``error`` -> ``warning`` — the REPL's console handler is pinned at level
   ``ERROR``, so every retry attempt printed, not just the last. Exactly one
   ``error`` remains: the retry loop's own final-exhaustion line, naming the
   provider and the error class.

This rig drives ``python -m cli.polyrob chat --plain`` as a real subprocess
(mirrors the isolation pattern in ``tests/unit/cli/test_session_keyless.py``):
a scratch ``HOME`` (so ``~/.polyrob/.env``/``cli.json`` are absent and
``polyrob init`` is never at risk of touching the real one), an isolated
``POLYROB_DATA_DIR``, and a syntactically-valid-but-fake ``OPENAI_API_KEY`` so
the REPL boots past the key-presence preflight.

Determinism (043 review, round 3): the provoked failure must not depend on a
real network round-trip to api.openai.com — a live 401 is slow, flaky under
network restrictions, and (worse) a completely UNRELATED local startup
failure can produce a similarly-shaped small stderr, silently passing for the
wrong reason. ``OPENAI_BASE_URL`` is pointed at a closed local port
(``http://127.0.0.1:9`` — nothing listens there, so the client gets an
immediate, local connection-refused) instead of relying on a real
authentication round-trip. ``PYTHONUSERBASE`` is threaded through from the
PARENT interpreter's own ``site.getuserbase()`` so overriding ``HOME`` for
isolation can never hide user-site packages the parent process can see (a
prior review run showed a scratch ``HOME`` masking user-site packages,
producing an unrelated startup failure that this rig's line-count-only
assertion couldn't distinguish from a genuine pass). The provoked test now
also asserts stdout actually reached the provider-call path (the exact
"dialog layer" line ``cli/ui/plain_renderer.py::_handle_session_done``
prints for a failed session) — so a startup failure can never masquerade as
a pass; there is no more "never reaches the provider is also a pass" branch.

Marked ``slow`` (real subprocess + full CLI container bootstrap), not
``live`` (no real network dependency).
"""

from __future__ import annotations

import os
import site
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Syntactically valid (clears the >=20-char / non-placeholder gate in
#: modules.llm.profiles.looks_like_real_key) but never a real credential.
FAKE_OPENAI_KEY = "sk-invalid-0000000000000000000000000000000000000000"

#: A closed local port: nothing listens on 9 (the historic "discard" port),
#: so a request here gets an immediate, LOCAL connection-refused — no real
#: network access, no dependency on api.openai.com being reachable or slow.
CLOSED_PORT_BASE_URL = "http://127.0.0.1:9"

#: The exact dialog-layer line the plain renderer prints when a session ends
#: in failure (cli/ui/plain_renderer.py::_handle_session_done). Its presence
#: in stdout proves the turn actually reached the provider-call path and
#: came back with a real (if synthetic) failure — a startup failure never
#: gets this far and can't produce this line.
SESSION_FAILED_MARKER = "error: session failed:"


def _playwright_browsers_path() -> str | None:
    """Best-effort discovery of an ALREADY-installed Playwright browser cache.

    Not a workaround for anything under test: the CLI container eagerly wraps
    a BrowserManager (S3, docs/AGENTS.md "dynamic tool rig"), and a live turn
    can touch it even when ``browser`` isn't in the requested tool_ids. CI
    installs Chromium once (``playwright install --with-deps chromium`` in
    ``.github/workflows/ci.yml``) under the runner's REAL home; without
    threading this through, an isolated-``HOME`` subprocess would report
    Chromium "missing" purely because a scratch HOME can't see a cache that
    was never installed under it — an artifact of this rig's own HOME
    isolation, unrelated to the auth-failure/stderr-squelch behaviour under
    test. If no cache is found anywhere, this returns None and the rig runs
    exactly as specified (any resulting noise is then real signal).
    """
    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if explicit:
        return explicit
    real_home = Path(os.path.expanduser("~"))
    for candidate in (
        real_home / "Library" / "Caches" / "ms-playwright",  # macOS
        real_home / ".cache" / "ms-playwright",  # Linux
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _base_env(tmp_path: Path, *, openai_base_url: str | None = None) -> dict:
    """A from-scratch subprocess environment (mirrors
    ``tests/unit/cli/test_session_keyless.py::_keyless_env``): every var is
    named explicitly, so nothing leaks in from the outer test process by
    accident (incl. any real provider key).

    ``PYTHONUSERBASE`` is read from the PARENT interpreter's own
    ``site.getuserbase()`` (not shelled out, not hardcoded — computed by the
    SAME interpreter that will run the subprocess via ``sys.executable``) so
    the child's overridden ``HOME`` cannot hide user-installed site-packages
    the parent process can see.
    """
    home = tmp_path / "home"
    data_dir = tmp_path / "data"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "CI": "1",  # never prompt
        "PYTHONUSERBASE": site.getuserbase(),
        "POLYROB_DATA_DIR": str(data_dir),
        "OPENAI_API_KEY": FAKE_OPENAI_KEY,
        "DEFAULT_PROVIDER": "openai",
        # This rig exercises provider failure and terminal logging, not model
        # downloads/native vector inference (OpenMP cannot run in some sandboxes).
        "MEMORY_BACKEND": "none",
        "KB_ENABLED": "false",
        # ANTHROPIC_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY deliberately
        # absent: a from-scratch env dict carries nothing not listed here.
    }
    if openai_base_url:
        env["OPENAI_BASE_URL"] = openai_base_url
    browsers_path = _playwright_browsers_path()
    if browsers_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    return env


def _run_repl(stdin: bytes, env: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    """``python -m cli.polyrob chat --plain`` as a subprocess. ``sys.executable``
    (not a bare ``python``/``python3``) so this runs under the SAME
    interpreter/venv as the test itself; cwd = repo root with NO explicit
    PYTHONPATH — ``python -m`` adds the cwd to sys.path on its own, exactly
    the module-import path a real end user gets."""
    return subprocess.run(
        [sys.executable, "-m", "cli.polyrob", "chat", "--plain"],
        input=stdin,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=timeout,
    )


@pytest.mark.slow
def test_repl_pipe_rig_clean_exit_stays_terse(tmp_path):
    """Baseline: a clean session (``/exit`` only, no turn ever submitted) must
    exit 0 with next to nothing on stderr."""
    env = _base_env(tmp_path)
    proc = _run_repl(b"/exit\n", env)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    lines = stderr.splitlines()

    assert proc.returncode == 0, (
        f"clean /exit exited {proc.returncode}\n--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )
    assert len(lines) <= 8, f"clean /exit produced {len(lines)} stderr lines:\n{stderr}"


@pytest.mark.slow
def test_repl_pipe_rig_provoked_failure_stays_terse(tmp_path):
    """A22/A12: a provoked provider failure must stay terse on stderr —
    single-line errors, no traceback, no 190-line wall.

    The failure is LOCAL and deterministic (``OPENAI_BASE_URL`` points at a
    closed port — no real network round-trip to api.openai.com), and the
    test asserts the turn actually reached the provider-call path via the
    exact stdout line the plain renderer prints for a failed session — a
    startup failure (e.g. a hidden-user-site-packages import error) can
    never masquerade as a pass here.
    """
    env = _base_env(tmp_path, openai_base_url=CLOSED_PORT_BASE_URL)
    proc = _run_repl(b"hello\n/exit\n", env, timeout=120)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    lines = stderr.splitlines()

    assert SESSION_FAILED_MARKER in stdout, (
        f"turn never reached the provider-call path (missing "
        f"{SESSION_FAILED_MARKER!r} in stdout — this would let a startup "
        f"failure masquerade as a pass):\n--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )
    assert not any("Traceback" in line for line in lines), (
        f"stderr still contains a traceback ({len(lines)} lines):\n{stderr}"
    )
    assert len(lines) <= 8, (
        f"provoked failure produced {len(lines)} stderr lines (budget 8):\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
