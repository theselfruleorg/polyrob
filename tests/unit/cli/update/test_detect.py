"""T1.1 — install-method detection is correct and fail-safe."""
from pathlib import Path

from cli.update.detect import (
    DOCKER, EDITABLE_GIT, GIT, PIP, PIPX, SYSTEMD, UNKNOWN,
    classify_install, find_git_root,
)


def _classify(**over):
    base = dict(
        package_dir=Path("/home/u/site-packages"),
        env={},
        dockerenv_exists=False,
        editable_flag=None,
        git_root=None,
        dist_present=True,
    )
    base.update(over)
    return classify_install(**base)


def test_docker_wins_via_env_or_file():
    assert _classify(dockerenv_exists=True).method == DOCKER
    assert _classify(env={"POLYROB_IN_DOCKER": "1"}).method == DOCKER


def test_systemd_via_invocation_id_or_opt_path():
    assert _classify(env={"INVOCATION_ID": "abc"}).method == SYSTEMD
    assert _classify(package_dir=Path("/opt/polyrob")).method == SYSTEMD


def test_editable_git_vs_plain_git():
    assert _classify(git_root=Path("/repo"), editable_flag=True).method == EDITABLE_GIT
    assert _classify(git_root=Path("/repo"), editable_flag=False).method == GIT
    # editable flag unknown but a git tree exists -> treat as plain git
    assert _classify(git_root=Path("/repo"), editable_flag=None).method == GIT


def test_pipx_before_pip():
    ctx = _classify(package_dir=Path("/home/u/.local/pipx/venvs/polyrob/lib"))
    assert ctx.method == PIPX


def test_pip_when_dist_present_and_no_git():
    assert _classify(dist_present=True).method == PIP


def test_unknown_is_failsafe():
    ctx = _classify(dist_present=False)
    assert ctx.method == UNKNOWN
    assert not ctx.self_updatable


def test_precedence_docker_over_git():
    # a container that also has a .git checkout is still docker
    assert _classify(dockerenv_exists=True, git_root=Path("/repo"),
                     editable_flag=True).method == DOCKER


class _FakeDist:
    """Minimal importlib.metadata.Distribution stand-in for read_editable_flag."""
    def __init__(self, name, direct_url=None):
        self._name = name
        self._direct_url = direct_url

    @property
    def metadata(self):
        return {"Name": self._name}

    def read_text(self, filename):
        if filename == "direct_url.json":
            return self._direct_url
        return None


def test_read_editable_flag_ignores_shadowing_egg_info(monkeypatch):
    # Real-world bug: a stray polyrob.egg-info (no direct_url.json) in the source tree
    # is resolved FIRST and shadows the real .dist-info that records editable=true.
    # read_editable_flag must scan all polyrob dists and use the one with direct_url.
    import cli.update.detect as det

    egg = _FakeDist("polyrob", direct_url=None)  # shadowing egg-info
    real = _FakeDist("polyrob", direct_url='{"dir_info": {"editable": true}}')
    other = _FakeDist("click", direct_url='{"dir_info": {"editable": false}}')
    monkeypatch.setattr(det, "_iter_polyrob_dists", lambda: [egg, real, other])
    assert det.read_editable_flag(Path("/repo")) is True


def test_read_editable_flag_false_for_wheel_install(monkeypatch):
    import cli.update.detect as det

    wheel = _FakeDist("polyrob", direct_url='{"dir_info": {"editable": false}}')
    monkeypatch.setattr(det, "_iter_polyrob_dists", lambda: [wheel])
    assert det.read_editable_flag(Path("/x")) is False


def test_read_editable_flag_none_when_no_direct_url(monkeypatch):
    import cli.update.detect as det

    egg_only = _FakeDist("polyrob", direct_url=None)
    monkeypatch.setattr(det, "_iter_polyrob_dists", lambda: [egg_only])
    assert det.read_editable_flag(Path("/x")) is None


def test_find_git_root(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_git_root(nested) == tmp_path
    assert find_git_root(tmp_path.parent) is None or find_git_root(tmp_path.parent) != nested
