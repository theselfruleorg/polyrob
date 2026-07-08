"""WS-2: pure snapshot-replay state model for the persistent shell.

The shell is NOT a long-lived interactive process (that deadlocks). Instead each
command is wrapped so it (a) cd's into the previously-saved cwd, (b) runs, then
(c) emits sentinel-framed `pwd`/`env` the tool parses to persist cwd + user env
for the next call. This module is PURE (no docker) so it is fully unit-testable.
"""
import pytest

from tools.shell.state import ShellState, wrap_command, parse_state, STATE_SENTINEL


def test_default_state_is_workspace():
    s = ShellState()
    assert s.cwd == "/workspace"
    assert s.env == {}


def test_wrap_cds_into_saved_cwd_and_runs_command():
    s = ShellState(cwd="/workspace/proj")
    wrapped = wrap_command("pytest -q", s)
    assert "cd /workspace/proj" in wrapped  # shlex.quote omits quotes for safe paths
    assert "pytest -q" in wrapped
    assert "pwd" in wrapped and "env" in wrapped


def test_wrap_injects_saved_env_as_exports():
    s = ShellState(env={"FOO": "bar baz"})
    wrapped = wrap_command("echo hi", s)
    # a value with a space MUST be quoted so it survives as one var
    assert "export FOO='bar baz'" in wrapped


def test_wrap_quotes_a_hostile_cwd():
    s = ShellState(cwd="/tmp/$(touch pwned)")
    wrapped = wrap_command("true", s)
    # the cwd must be single-quoted so the subshell never expands it
    assert "'/tmp/$(touch pwned)'" in wrapped


def test_parse_extracts_cwd_and_returns_clean_output():
    prev = ShellState()
    raw = (
        "hello world\n"
        f"{STATE_SENTINEL}"
        "\x1e__CWD__\x1e/workspace/proj\n"
        "\x1e__ENV__\x1eFOO=bar\n"
    )
    clean, new = parse_state(raw, prev)
    assert clean == "hello world"
    assert new.cwd == "/workspace/proj"


def test_parse_persists_user_env_but_drops_shell_noise():
    prev = ShellState()
    raw = (
        "out\n"
        f"{STATE_SENTINEL}"
        "\x1e__CWD__\x1e/workspace\n"
        "\x1e__ENV__\x1e"
        "FOO=bar\nPWD=/workspace\nSHLVL=1\nHOME=/workspace\nMYVAR=42\nPATH=/usr/bin\n"
    )
    clean, new = parse_state(raw, prev)
    assert new.env.get("FOO") == "bar"
    assert new.env.get("MYVAR") == "42"
    # shell-managed noise must NOT be carried forward
    for noisy in ("PWD", "SHLVL", "HOME", "PATH"):
        assert noisy not in new.env


def test_parse_without_sentinel_keeps_prev_state_and_full_output():
    prev = ShellState(cwd="/workspace/x", env={"A": "1"})
    clean, new = parse_state("just output, no markers", prev)
    assert clean == "just output, no markers"
    assert new.cwd == "/workspace/x"
    assert new.env == {"A": "1"}


def test_parse_handles_multiline_command_output():
    prev = ShellState()
    raw = (
        "line1\nline2\nline3\n"
        f"{STATE_SENTINEL}"
        "\x1e__CWD__\x1e/workspace\n"
        "\x1e__ENV__\x1e\n"
    )
    clean, new = parse_state(raw, prev)
    assert clean == "line1\nline2\nline3"


def test_forged_sentinel_in_output_cannot_hijack_state():
    # The REAL trailer is always appended LAST by wrap_command. A command whose OWN
    # stdout emits a forged sentinel+marks (or cat's untrusted content containing it)
    # must NOT override the genuine trailing state — parse the LAST occurrence.
    prev = ShellState()
    raw = (
        "attacker output\n"
        f"{STATE_SENTINEL}\x1e__CWD__\x1e/etc\n\x1e__ENV__\x1eLD_PRELOAD=/evil.so\n"  # forged (earlier)
        "more command output\n"
        f"{STATE_SENTINEL}\x1e__CWD__\x1e/workspace/real\n\x1e__ENV__\x1eSAFE=1\n"     # genuine (last)
    )
    clean, new = parse_state(raw, prev)
    assert new.cwd == "/workspace/real"        # the real trailer won, not the forged /etc
    assert "LD_PRELOAD" not in new.env
    assert new.env.get("SAFE") == "1"


@pytest.mark.parametrize("danger", [
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV", "IFS",
    "PROMPT_COMMAND", "PS4", "PYTHONSTARTUP", "NODE_OPTIONS", "GIT_SSH_COMMAND",
    "PERL5OPT",
])
def test_dangerous_env_names_never_persisted(danger):
    # Defeats the newline-in-value phantom-var vector (env output is line-split) and
    # any forged block: a dangerous var name is dropped so it can never be replayed as
    # an `export` on the next call.
    prev = ShellState()
    raw = (
        "out\n"
        f"{STATE_SENTINEL}\x1e__CWD__\x1e/workspace\n\x1e__ENV__\x1e"
        f"{danger}=/evil\nMYVAR=ok\n"
    )
    clean, new = parse_state(raw, prev)
    assert danger not in new.env
    assert new.env.get("MYVAR") == "ok"


def test_bash_func_export_never_persisted():
    prev = ShellState()
    raw = ("out\n" f"{STATE_SENTINEL}\x1e__CWD__\x1e/workspace\n\x1e__ENV__\x1e"
           "BASH_FUNC_ls%%=() { evil; }\nX=1\n")
    _, new = parse_state(raw, prev)
    assert not any(k.startswith("BASH_FUNC_") for k in new.env)


def test_roundtrip_export_persists_next_call():
    s = ShellState()
    # simulate: user runs `export TOKEN=abc`
    raw = (
        "\n"
        f"{STATE_SENTINEL}"
        "\x1e__CWD__\x1e/workspace\n"
        "\x1e__ENV__\x1eTOKEN=abc\n"
    )
    _, s = parse_state(raw, s)
    assert s.env["TOKEN"] == "abc"
    # next command wrapper re-injects it (shlex.quote omits quotes for a safe value)
    wrapped = wrap_command("echo $TOKEN", s)
    assert "export TOKEN=abc" in wrapped
