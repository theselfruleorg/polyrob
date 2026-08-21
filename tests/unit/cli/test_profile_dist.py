"""polyrob profile install/update/info — distribution contract (W6).

The load-bearing invariant: distribution-owned paths are replaced on update;
user-owned data (.env, auth.json, memory.db, self.md) is NEVER touched.
"""
from pathlib import Path

import click
import pytest

from cli.commands.profile_dist import install_profile, update_profile


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("POLYROB_PROFILES_ROOT", raising=False)
    return home


def _mk_dist(tmp_path, name="scout", version="1.0", extra_manifest="",
             char_body='{"name": "Scout v1"}'):
    d = tmp_path / f"dist-{version}"
    (d / "characters").mkdir(parents=True)
    (d / "skills" / "greet").mkdir(parents=True)
    (d / "characters" / f"{name}.character.json").write_text(char_body)
    (d / "skills" / "greet" / "SKILL.md").write_text(f"# greet {version}\n")
    (d / "soul.md").write_text(f"SOUL {version}\n")
    (d / "config.yaml").write_text(f"tuning: v{version}\n")
    (d / "polyrob.profile.yaml").write_text(
        f"name: {name}\nversion: '{version}'\ndescription: test dist\n"
        f"env_requires:\n  - name: ANTHROPIC_API_KEY\n    required: true\n"
        + extra_manifest)
    return d


def test_install_from_local_dir(env, tmp_path):
    dist = _mk_dist(tmp_path)
    result = install_profile(str(dist))
    home = result["home"]
    assert home == env / "profiles" / "scout"
    assert (home / "characters" / "scout.character.json").is_file()
    assert (home / "skills" / "greet" / "SKILL.md").is_file()
    assert (home / "data" / "identity" / "scout" / "soul.md").read_text() == "SOUL 1.0\n"
    assert (home / "config.yaml").read_text() == "tuning: v1.0\n"
    assert "POLYROB_INSTANCE_ID=scout" in (home / ".env").read_text()
    import yaml
    meta = yaml.safe_load((home / "profile.yaml").read_text())
    assert meta["distribution"]["source"] == str(dist)


def test_update_replaces_owned_preserves_user_data(env, tmp_path, monkeypatch):
    dist1 = _mk_dist(tmp_path, version="1.0")
    install_profile(str(dist1))
    home = env / "profiles" / "scout"
    # user data accrues
    (home / ".env").write_text("POLYROB_INSTANCE_ID=scout\nANTHROPIC_API_KEY=sk-live\n")
    (home / "auth.json").write_text('{"t": 1}')
    (home / "data" / "memory.db").write_text("MEMORIES")
    self_doc = home / "data" / "identity" / "scout" / "user_x"
    self_doc.mkdir(parents=True)
    (self_doc / "self.md").write_text("MY SELF\n")
    (home / "config.yaml").write_text("tuning: MINE\n")
    # a new version appears at the SAME source path
    dist2 = _mk_dist(tmp_path, version="2.0", char_body='{"name": "Scout v2"}')
    import yaml
    meta = yaml.safe_load((home / "profile.yaml").read_text())
    meta["distribution"]["source"] = str(dist2)
    (home / "profile.yaml").write_text(yaml.safe_dump(meta))

    update_profile("scout")
    assert "Scout v2" in (home / "characters" / "scout.character.json").read_text()
    assert (home / "skills" / "greet" / "SKILL.md").read_text() == "# greet 2.0\n"
    assert (home / "data" / "identity" / "scout" / "soul.md").read_text() == "SOUL 2.0\n"
    # user-owned: NEVER touched
    assert "sk-live" in (home / ".env").read_text()
    assert (home / "auth.json").read_text() == '{"t": 1}'
    assert (home / "data" / "memory.db").read_text() == "MEMORIES"
    assert (self_doc / "self.md").read_text() == "MY SELF\n"
    # config.yaml preserved without --force-config
    assert (home / "config.yaml").read_text() == "tuning: MINE\n"

    update_profile("scout", force_config=True)
    assert (home / "config.yaml").read_text() == "tuning: v2.0\n"


def test_install_refuses_existing_without_force(env, tmp_path):
    dist = _mk_dist(tmp_path)
    install_profile(str(dist))
    with pytest.raises(click.ClickException, match="--force"):
        install_profile(str(dist))
    install_profile(str(dist), force=True)  # replaces owned files, no error


def test_manifest_escaping_path_refused(env, tmp_path):
    dist = _mk_dist(tmp_path, extra_manifest="distribution_owned:\n  - ../../evil\n")
    with pytest.raises(click.ClickException, match="outside the profile root"):
        install_profile(str(dist))


def test_manifest_may_not_claim_user_owned(env, tmp_path):
    dist = _mk_dist(tmp_path, extra_manifest="distribution_owned:\n  - data/\n")
    with pytest.raises(click.ClickException, match="never distribution-owned"):
        install_profile(str(dist))


def test_polyrob_requires_mismatch_warns(env, tmp_path, capsys):
    dist = _mk_dist(tmp_path, extra_manifest="polyrob_requires: '>=999.0'\n")
    install_profile(str(dist))  # warns, does not fail
    assert ">=999.0" in capsys.readouterr().err


def test_missing_manifest_refused(env, tmp_path):
    d = tmp_path / "not-a-dist"
    d.mkdir()
    with pytest.raises(click.ClickException, match="not a profile distribution"):
        install_profile(str(d))


def test_git_env_blocks_ext_fd_transports():
    """A distribution source is attacker-controlled; git's ext::/fd:: run a
    command at clone time (RCE). The clone env must pin the protocol allowlist."""
    from cli.commands.profile_dist import _git_env

    allow = _git_env()["GIT_ALLOW_PROTOCOL"].split(":")
    assert "ext" not in allow and "fd" not in allow
    assert {"https", "ssh", "git"} <= set(allow)


def test_fetch_source_rejects_option_shaped_ref(tmp_path, monkeypatch):
    """An option-shaped #ref must be refused before it reaches git checkout."""
    from cli.commands import profile_dist

    # Make the "clone" step a no-op that creates the dest so we reach the ref step.
    def _fake_run(cmd, **kw):
        if "clone" in cmd:
            (tmp_path / "clone").mkdir(exist_ok=True)
        class _R:
            returncode = 0
            stderr = ""
        return _R()
    monkeypatch.setattr(profile_dist.subprocess, "run", _fake_run)

    with pytest.raises(click.ClickException, match="invalid ref"):
        profile_dist._fetch_source("https://example.test/x.git#--upload-pack=evil",
                                   tmp_path)
