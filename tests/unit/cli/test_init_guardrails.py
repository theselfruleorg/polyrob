"""`polyrob init` Section 6/6 — Autonomy & guardrails (owner-UX P3 T4).

All-blank-to-skip prompts placed after Owner pairing, before the final
summary: local mode, approval preset, and daily digest channel. Skipped
entirely by `--quick` and non-interactive modes (nested inside the same
`if not quick:` block as Owner pairing/Template).

NOTE: the "Autonomy budget USD/window" prompt (`AUTONOMY_BUDGET_USD`) was
removed along with the autonomy budget gate itself — a $/day rate ceiling
cannot protect a finite balance (see the money-ledger split proposal §5.3
Task 9). Section 6/6 now asks 3 questions, not 4.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from core.prefs import load_preferences
from tools.controller.approval import DEFAULT_APPROVAL_REQUIRED_TOOLS


def _make_home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _invoke(args, home, monkeypatch, input_text=None):
    """Run init_cmd inside an isolated CWD and HOME (mirrors test_init_wizard.py)."""
    proj = home.parent / "proj"
    proj.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.init import init_cmd
    runner = CliRunner()
    result = runner.invoke(init_cmd, args, input=input_text, catch_exceptions=False)
    return result, proj


def _read_env(home: Path) -> dict:
    env_path = home / ".polyrob" / ".env"
    if not env_path.exists():
        return {}
    text = env_path.read_text()
    return {
        k.strip(): v.strip()
        for k, v in (ln.split("=", 1) for ln in text.splitlines() if "=" in ln)
    }


# The full non-quick interactive wizard prompts, in order, BEFORE Section 6/6:
# Section 1 (provider choice/key/another = 3 blanks), model, toolset, template,
# instance id, owner id.
_PRE_SECTION5_BLANKS = ["" for _ in range(8)]


def _full_flow_input(*, local="", autonomy="", preset="", digest="", wallet="n") -> str:
    # Section 6/6 order (0.9.0): interactive-local-tools confirm (default YES),
    # autonomy confirm (default NO), approval preset, digest. Then Task 7's
    # "Optional: agent crypto wallet" confirm (default No).
    return "\n".join(_PRE_SECTION5_BLANKS + [local, autonomy, preset, digest, wallet]) + "\n"


# ---------------------------------------------------------------------------
# Full flow: every Section 6/6 prompt answered writes the expected keys.
# ---------------------------------------------------------------------------

def test_full_flow_writes_expected_keys(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    piped = _full_flow_input(local="y", autonomy="y", preset="y", digest="telegram")
    res, proj = _invoke([], home, monkeypatch, input_text=piped)
    assert res.exit_code == 0, res.output
    assert "Section 6/6: Autonomy & guardrails" in res.output

    env = _read_env(home)
    assert env.get("POLYROB_LOCAL") == "1"
    # 0.9.0: the autonomy prompt is separate — answering yes writes the master flag.
    assert env.get("AUTONOMY_ENABLED") == "true"
    assert env.get("APPROVAL_REQUIRED_TOOLS") == ",".join(DEFAULT_APPROVAL_REQUIRED_TOOLS)
    assert env.get("APPROVAL_PROVIDER") == "interactive_cli"
    # digest goes through prefs (owner uid "rob" known from Owner pairing defaults),
    # NOT into the env file.
    assert "OWNER_DIGEST_ENABLED" not in env

    data_home = proj / ".polyrob"
    prefs = load_preferences(data_home, "rob", "rob")
    assert prefs.get("digest.channel") == "telegram"
    assert prefs.get("digest.enabled") is True


def test_final_summary_mentions_self_capability(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    piped = _full_flow_input()
    res, _proj = _invoke([], home, monkeypatch, input_text=piped)
    assert res.exit_code == 0, res.output
    assert 'Ask me anything about myself — try "what can you do?"' in res.output


# ---------------------------------------------------------------------------
# Blank answers take each Section 6/6 prompt's default (0.9.0): the interactive
# local-tools prompt defaults YES (writes POLYROB_LOCAL), autonomy defaults NO,
# and the approval preset / digest still skip on blank.
# ---------------------------------------------------------------------------

def test_blanks_take_section6_defaults(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    piped = _full_flow_input()  # every Section 6/6 prompt left blank
    res, proj = _invoke([], home, monkeypatch, input_text=piped)
    assert res.exit_code == 0, res.output

    env = _read_env(home)
    # interactive-local-tools prompt defaults YES -> POLYROB_LOCAL written
    assert env.get("POLYROB_LOCAL") == "1"
    # autonomy prompt defaults NO -> master NOT written
    assert "AUTONOMY_ENABLED" not in env
    # approval preset + digest still skip on blank
    assert "APPROVAL_REQUIRED_TOOLS" not in env
    assert "APPROVAL_PROVIDER" not in env
    assert "OWNER_DIGEST_ENABLED" not in env

    data_home = proj / ".polyrob"
    prefs = load_preferences(data_home, "rob", "rob")
    assert "digest.channel" not in prefs
    assert "digest.enabled" not in prefs


# ---------------------------------------------------------------------------
# --quick skips Section 6/6 entirely (it's nested inside `if not quick:`).
# ---------------------------------------------------------------------------

def test_quick_skips_guardrails_section_entirely(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    res, _proj = _invoke(
        ["--quick", "--anthropic-key", "sk-q", "--default-model", "gpt-5"],
        home, monkeypatch, input_text="\n",
    )
    assert res.exit_code == 0, res.output
    assert "Autonomy & guardrails" not in res.output

    env = _read_env(home)
    assert "POLYROB_LOCAL" not in env
    assert "APPROVAL_REQUIRED_TOOLS" not in env
    assert "APPROVAL_PROVIDER" not in env


def test_non_interactive_skips_guardrails_section_entirely(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    res, _proj = _invoke(
        ["--no-prompt", "--anthropic-key", "sk-n"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    assert "Autonomy & guardrails" not in res.output
    env = _read_env(home)
    assert "POLYROB_LOCAL" not in env


# ---------------------------------------------------------------------------
# Digest-with-owner writes typed preferences, never the env file.
# ---------------------------------------------------------------------------

def test_digest_with_owner_writes_prefs_not_env(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    piped = _full_flow_input(digest="email")
    res, proj = _invoke([], home, monkeypatch, input_text=piped)
    assert res.exit_code == 0, res.output

    env = _read_env(home)
    assert "OWNER_DIGEST_ENABLED" not in env
    assert "digest" not in " ".join(env.keys()).lower()

    data_home = proj / ".polyrob"
    prefs = load_preferences(data_home, "rob", "rob")
    assert prefs.get("digest.channel") == "email"
    assert prefs.get("digest.enabled") is True


def test_digest_with_explicit_owner_flag_writes_that_owners_prefs(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    # --owner/--instance-id pre-supplied => Owner-pairing's two prompts are
    # skipped, leaving 6 prompts (Section 1's choice/key/another + model +
    # toolset + template) before Section 6/6's 4 prompts (interactive,
    # autonomy, preset, digest) + the wallet opt-in confirm (Task 7, "n").
    piped = "\n".join(_PRE_SECTION5_BLANKS[:6] + ["", "", "", "email", "n"]) + "\n"
    res, proj = _invoke(
        ["--owner", "alice", "--instance-id", "alice"],
        home, monkeypatch, input_text=piped,
    )
    assert res.exit_code == 0, res.output
    data_home = proj / ".polyrob"
    prefs = load_preferences(data_home, "alice", "alice")
    assert prefs.get("digest.channel") == "email"
