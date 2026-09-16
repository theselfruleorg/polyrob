"""CLI regroup + `identity` umbrella + aliases (043 A14/A25 §2.4, §4.1).

`polyrob --help` groups its ~45 commands into sections so a first-run user
sees "Start here" instead of a flat wall (027 WP6). Before this task, `apps`,
`autonomy`, and `persona` fell into the computed "Other" catch-all by
accident — nobody had added them to `_HELP_GROUPS`. This pins the regroup:
a new **Owner** section (`owner identity profile`), `apps`/`autonomy` folded
into "Autonomy & work" alongside `tools`/`kb`, `journey` folded into "Money",
and a new `identity` click group (`cli/commands/identity.py`) that mounts the
existing `soul`/`persona`/`pfp` groups as `identity soul` / `identity persona`
/ `identity avatar`.

Old top-level names never stop working (043 §4.1: an alias invokes forever) —
they just fold onto their canonical row in `--help` via `_COMMAND_ALIASES`:
`soul`/`persona`/`pfp` -> `identity`, `approvals` -> `owner` (display-only;
`owner approvals` as a real subcommand is phase 3), `skill` -> `skills`.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Run every invoke from an isolated CWD + data dir so a dev box's
    project-local ``./.polyrob/.env`` can never inject into the test process
    (mirrors tests/unit/cli/test_approvals_cmd.py's fixture of the same
    name)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))


def _invoke(args):
    from cli.polyrob import cli
    return CliRunner().invoke(cli, args)


_ALIAS_TOKENS = ("soul", "persona", "pfp", "approvals", "skill",
                  "sessions", "models", "profiles", "webgate")


# ---------------------------------------------------------------------------
# (a) Owner section
# ---------------------------------------------------------------------------


def test_help_shows_owner_section_with_owner_identity_profile():
    res = _invoke(["--help"])
    assert res.exit_code == 0, res.output
    assert "Owner:" in res.output
    section = res.output.split("Owner:", 1)[1]
    # Cut off at the next top-level section header (or end of output).
    for stop in ("Other:",):
        if stop in section:
            section = section.split(stop, 1)[0]
    for name in ("owner", "identity", "profile"):
        assert name in section, f"{name!r} missing from the Owner section:\n{section}"


# ---------------------------------------------------------------------------
# (b) Other bucket is exactly datagen + x-account
# ---------------------------------------------------------------------------


def test_other_section_is_exactly_datagen_and_x_account():
    res = _invoke(["--help"])
    assert res.exit_code == 0, res.output
    assert "Other:" in res.output, res.output
    other = res.output.split("Other:", 1)[1]
    rows = [line.strip().split()[0] for line in other.splitlines() if line.strip()]
    assert rows == ["datagen", "x-account"], rows


# ---------------------------------------------------------------------------
# (c) No alias renders as its own row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", _ALIAS_TOKENS)
def test_alias_does_not_appear_as_its_own_help_row(alias):
    res = _invoke(["--help"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    # A "row" starts the line with exactly the alias name followed by
    # whitespace or "(alias:" — not merely substring-contained inside a
    # canonical row's parenthetical (e.g. "identity (alias: soul, ...)").
    offending = [
        line for line in lines
        if line.strip().split(" ", 1)[0] == alias
    ]
    assert not offending, f"{alias!r} rendered as its own row: {offending}"


# ---------------------------------------------------------------------------
# (d) Every alias still invokes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["soul", "persona", "pfp", "approvals", "skill"])
def test_alias_still_invokes(alias):
    res = _invoke([alias, "--help"])
    assert res.exit_code == 0, res.output


# ---------------------------------------------------------------------------
# (e) `identity --help` lists soul/persona/avatar
# ---------------------------------------------------------------------------


def test_identity_group_lists_soul_persona_avatar():
    res = _invoke(["identity", "--help"])
    assert res.exit_code == 0, res.output
    for name in ("soul", "persona", "avatar"):
        assert name in res.output, res.output


# ---------------------------------------------------------------------------
# (f) `identity avatar --help` is pfp's subcommands
# ---------------------------------------------------------------------------


def test_identity_avatar_is_pfp():
    res = _invoke(["identity", "avatar", "--help"])
    assert res.exit_code == 0, res.output
    assert "show" in res.output
    assert "generate" in res.output


# ---------------------------------------------------------------------------
# Every visible command lands in exactly one group
# ---------------------------------------------------------------------------


def test_every_help_group_command_is_visible_exactly_once():
    from cli.polyrob import _HELP_GROUPS

    seen = []
    for _title, names in _HELP_GROUPS:
        seen.extend(names)
    assert len(seen) == len(set(seen)), "a command appears in more than one group"


def test_autonomy_and_apps_and_persona_no_longer_land_in_other():
    """The 043 A14 regression pin: apps/autonomy/persona used to fall into
    the computed Other bucket by accident."""
    res = _invoke(["--help"])
    assert res.exit_code == 0, res.output
    other = res.output.split("Other:", 1)[1]
    for name in ("apps", "autonomy", "persona"):
        assert name not in other.split(), f"{name!r} regressed back into Other"
