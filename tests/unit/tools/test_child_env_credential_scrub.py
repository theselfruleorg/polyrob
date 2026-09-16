"""Ratchet: no child process is spawned with the agent's own environment (S2).

``AGENT_WALLET_MASTER_SEED`` and every provider API key live in the agent
process environment. Until 2026-09-14 three call sites handed that environment
straight to a child:

* ``tools/browser/browser.py`` — ``os.environ.copy()`` into
  ``playwright.chromium.launch(env=…)``, so every page Chromium rendered ran
  next to the treasury seed;
* ``tools/anysite/client.py`` — ``{**os.environ, …}`` into the third-party
  ``anysite`` CLI;
* ``cli/commands/{profile_dist,skill_install}.py`` — ``dict(os.environ)`` into a
  ``git clone`` of an ATTACKER-NAMED repository.

MCP had already been fixed this way once (``tools/mcp/child_env.py``, H2
2026-08-22) and the pattern came back in four other places, which is exactly
what a ratchet is for. Two rules are pinned here:

1. **No full-environment expression may reach a spawn's ``env=``** anywhere in
   ``tools/`` or ``cli/``. The allowlist is EMPTY.
2. **Every spawn in a hardened module must pass ``env=`` explicitly** — an
   omitted ``env=`` is an implicit full inherit, which is the same leak with no
   grep-able shape.

Plus behavioural checks that the scrubbed builders actually drop the seed while
keeping what their child genuinely needs.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCANNED_DIRS = ("tools", "cli")

#: Attribute/function names that start a child process.
_SPAWN_NAMES = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "create_subprocess_exec", "create_subprocess_shell",
    "launch", "launch_persistent_context",
})

#: A spawn call must be qualified by one of these, so an unrelated ``self.run``
#: or ``pool.call`` is not mistaken for a subprocess.
_SPAWN_QUALIFIERS = frozenset({
    "subprocess", "asyncio", "chromium", "firefox", "webkit", "playwright",
})

#: (module path, reason) — a full-env expression legitimately reaching a spawn.
#: EMPTY BY DESIGN. Adding a row here is a security decision, not a cleanup.
FULL_ENV_SPAWN_ALLOWLIST: dict[str, str] = {}

#: Modules whose every spawn must pass an explicit ``env=``. These are the
#: hardened ones; the rest of the tree is covered by rule 1 only.
MUST_PASS_ENV_MODULES = (
    "tools/browser/browser.py",
    "tools/browser/playwright_utils.py",
    "tools/anysite/client.py",
    "tools/code_exec/backends/docker.py",
    "tools/code_exec/backends/_proc.py",
    "tools/code_exec/backends/local_subprocess.py",
    "cli/commands/profile_dist.py",
    "cli/commands/skill_install.py",
)


def _python_files():
    for d in SCANNED_DIRS:
        for p in sorted((REPO_ROOT / d).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


def _is_os_environ(node: ast.AST) -> bool:
    """``os.environ`` / ``environ`` (a ``from os import environ`` import)."""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id == "environ"


def _is_full_env_expr(node: ast.AST) -> bool:
    """True for ``os.environ``, ``os.environ.copy()``, ``dict(os.environ, …)``
    and ``{**os.environ, …}`` — every way to name the WHOLE environment."""
    if node is None:
        return False
    if _is_os_environ(node):
        return True
    if isinstance(node, ast.Call):
        # os.environ.copy()
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "copy"
                and _is_os_environ(node.func.value)):
            return True
        # dict(os.environ, ...) / os.environ.copy() passed through dict()
        if isinstance(node.func, ast.Name) and node.func.id == "dict":
            return any(_is_full_env_expr(a) for a in node.args)
    if isinstance(node, ast.Dict):
        # {**os.environ, ...}
        for key, value in zip(node.keys, node.values):
            if key is None and _is_full_env_expr(value):
                return True
    return False


def _spawn_calls(tree: ast.AST):
    """Yield every Call node that starts a child process."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _SPAWN_NAMES:
            root = func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            name = root.id if isinstance(root, ast.Name) else None
            if name in _SPAWN_QUALIFIERS:
                yield node


def _env_kwarg(call: ast.Call):
    for kw in call.keywords:
        if kw.arg == "env":
            return kw.value
    return None


def _full_env_names(tree: ast.AST) -> set[str]:
    """Names bound anywhere in the module to a full-environment expression."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_full_env_expr(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and _is_full_env_expr(node.value):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


# ---------------------------------------------------------------------------
# Rule 1 — no full-environment expression reaches a spawn
# ---------------------------------------------------------------------------

def test_no_full_environment_is_passed_to_a_child_process():
    offenders = []
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        env_names = _full_env_names(tree)
        for call in _spawn_calls(tree):
            env = _env_kwarg(call)
            if env is None:
                continue
            leaks = _is_full_env_expr(env) or (
                isinstance(env, ast.Name) and env.id in env_names)
            if leaks and rel not in FULL_ENV_SPAWN_ALLOWLIST:
                offenders.append(f"{rel}:{call.lineno}")

    assert not offenders, (
        "a child process is spawned with the agent's FULL environment "
        "(wallet seed + every API key) at: " + ", ".join(offenders) +
        "\nBuild the child env with tools/code_exec/env_policy.py::"
        "build_child_env (or one of its wrappers: tools/browser/child_env.py, "
        "tools/mcp/child_env.py, cli/git_child_env.py)."
    )


def test_full_env_spawn_allowlist_is_empty():
    """The allowlist exists so an exception is a deliberate, reasoned row —
    not so it can quietly fill up."""
    assert FULL_ENV_SPAWN_ALLOWLIST == {}, (
        "every exemption needs a security rationale reviewed with it: "
        f"{FULL_ENV_SPAWN_ALLOWLIST}"
    )


# ---------------------------------------------------------------------------
# Rule 2 — a hardened module never spawns with an implicit inherit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", MUST_PASS_ENV_MODULES)
def test_hardened_module_spawns_pass_explicit_env(rel):
    path = REPO_ROOT / rel
    assert path.is_file(), f"{rel} moved — update MUST_PASS_ENV_MODULES"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing = [str(c.lineno) for c in _spawn_calls(tree) if _env_kwarg(c) is None]
    assert not missing, (
        f"{rel} spawns a child WITHOUT env= at line(s) {', '.join(missing)} — "
        "an omitted env= is an implicit full inherit of the agent environment."
    )


# ---------------------------------------------------------------------------
# Behaviour — the scrubbed builders drop the seed and keep what's needed
# ---------------------------------------------------------------------------

_SECRETS = {
    "AGENT_WALLET_MASTER_SEED": "seedseedseed",
    "ANTHROPIC_API_KEY": "sk-ant-xxxxxxxxxxxxxxxxxxxx",
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "POLYROB_OWNER_PASSWORD_HASH": "$argon2id$x",
}


@pytest.fixture
def _env_with_secrets(monkeypatch):
    for k, v in _SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", "/usr/bin")
    return monkeypatch


def _assert_no_secret_values(env: dict):
    for name, value in _SECRETS.items():
        assert name not in env, f"{name} reached the child environment"
        assert value not in env.values(), f"the value of {name} reached the child"


def test_browser_env_drops_the_seed_but_keeps_display(_env_with_secrets):
    _env_with_secrets.setenv("DISPLAY", ":99")
    _env_with_secrets.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw")
    from tools.browser.child_env import build_browser_env
    env = build_browser_env()
    _assert_no_secret_values(env)
    assert env["DISPLAY"] == ":99"
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/pw"
    assert env["PATH"] == "/usr/bin"


def test_browser_env_drops_a_secret_named_key_from_the_caller_overlay(_env_with_secrets):
    from tools.browser.child_env import build_browser_env
    env = build_browser_env({"DISPLAY": ":99", "SNEAKY_API_KEY": "nope"})
    assert env["DISPLAY"] == ":99"
    assert "SNEAKY_API_KEY" not in env


def test_anysite_env_carries_only_its_own_credential(_env_with_secrets):
    _env_with_secrets.setenv("ANYSITE_API_KEY", "anysite-live-key")
    from tools.anysite.client import build_anysite_env
    env = build_anysite_env()
    _assert_no_secret_values(env)
    # The one credential this child is SUPPOSED to have, by env rather than argv.
    assert env["ANYSITE_API_KEY"] == "anysite-live-key"
    assert env["ANYSITE_ACCESS_TOKEN"] == "anysite-live-key"


def test_anysite_env_without_a_key_carries_none(_env_with_secrets, monkeypatch):
    monkeypatch.delenv("ANYSITE_API_KEY", raising=False)
    monkeypatch.delenv("ANYSITE_ACCESS_TOKEN", raising=False)
    from tools.anysite.client import build_anysite_env
    env = build_anysite_env()
    _assert_no_secret_values(env)
    assert "ANYSITE_API_KEY" not in env


def test_git_child_env_drops_the_seed_and_pins_the_hardening_flags(_env_with_secrets):
    from cli.git_child_env import build_git_child_env
    env = build_git_child_env()
    _assert_no_secret_values(env)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == "file:git:http:https:ssh"
    assert "ext" not in env["GIT_ALLOW_PROTOCOL"].split(":")


def test_git_child_env_caller_override_wins(_env_with_secrets):
    from cli.git_child_env import build_git_child_env
    env = build_git_child_env(GIT_CONFIG_GLOBAL="/dev/null")
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_build_child_env_widening_never_widens_the_secret_set(_env_with_secrets):
    """A caller naming a secret var in extra_allowlist still does not get it."""
    from tools.code_exec.env_policy import build_child_env
    env = build_child_env(
        extra_allowlist=("AGENT_WALLET_MASTER_SEED", "DISPLAY"),
        allow_prefixes=("ANTHROPIC_",),
    )
    _assert_no_secret_values(env)


def test_build_child_env_default_shape_is_unchanged(_env_with_secrets):
    """No widening args => the original SAFE_ALLOWLIST behaviour."""
    from tools.code_exec.env_policy import SAFE_ALLOWLIST, build_child_env
    _env_with_secrets.setenv("DISPLAY", ":99")
    env = build_child_env()
    assert set(env) <= set(SAFE_ALLOWLIST)
    assert "DISPLAY" not in env
