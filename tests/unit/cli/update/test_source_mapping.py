"""C5: `polyrob update` must resolve its "latest version" from the CORRECT channel.

POLYROB's canonical release channel is GitHub Releases/tags. git checkouts, editable
installs, and systemd/server installs (deployed via git/rsync, updated with `git pull`)
all track GitHub. Only true PyPI wheel installs (pip/pipx) track PyPI. The old mapping
sent SYSTEMD → pypi, so the prod/server path queried a channel POLYROB isn't published
to and `--check` always said "could not check". This guards the corrected mapping.
"""
from pathlib import Path

from cli.commands.update import _source_for
from cli.update.detect import (
    DOCKER, EDITABLE_GIT, GIT, PIP, PIPX, SYSTEMD, UNKNOWN, InstallContext,
)


def _ctx(method, repo_root=None):
    return InstallContext(method, Path("/opt/polyrob"), repo_root, "test")


def test_git_backed_methods_map_to_github():
    for m in (GIT, EDITABLE_GIT, SYSTEMD):
        assert _source_for(_ctx(m, Path("/opt/polyrob"))) == "github", m


def test_systemd_without_local_repo_still_github():
    # A systemd install deployed by rsync has no local .git, but GitHub Releases
    # is still the right (network) source — not PyPI.
    assert _source_for(_ctx(SYSTEMD, repo_root=None)) == "github"


def test_pip_and_pipx_track_pypi():
    for m in (PIP, PIPX):
        assert _source_for(_ctx(m)) == "pypi", m


def test_unknown_defaults_to_github_canonical_channel():
    assert _source_for(_ctx(UNKNOWN)) == "github"


def test_every_code_update_step_includes_migrate():
    """C3 belt-and-suspenders: non-docker manual steps must run migrate upgrade so
    following the printed instructions can't leave code ahead of the DB schema. Docker
    is exempt — its container auto-migrates at boot (api/app.py lifespan)."""
    from cli.commands.update import _MANUAL_STEPS, DOCKER
    for method, step in _MANUAL_STEPS.items():
        if method == DOCKER:
            continue
        assert "migrate upgrade" in step, f"{method} manual step skips DB migration: {step!r}"
