"""polyrob profile export/import — backup discipline (W5).

The invariants under test: credentials never enter an archive, secret-shaped
strings are force-scrubbed in the STAGED copy only, the live profile is
byte-unchanged, traversal archives are refused, round-trips are stable.
"""
import io
import tarfile
from pathlib import Path

import pytest

from cli.commands.profile_transfer import export_profile, import_profile

_FAKE_KEY = "sk-" + "a1b2c3d4e5f6g7h8" * 2  # provider-key shaped, fake


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("POLYROB_PROFILES_ROOT", raising=False)
    return home


def _seed_profile(home, name="rob"):
    p = home / "profiles" / name
    (p / "characters").mkdir(parents=True)
    (p / "data").mkdir()
    (p / "wallet").mkdir()
    (p / "profile.yaml").write_text(f"name: {name}\n")
    (p / ".env").write_text(f"ANTHROPIC_API_KEY={_FAKE_KEY}\n")
    (p / "auth.json").write_text('{"token": "secret"}')
    (p / "wallet" / "seed.txt").write_text("abandon abandon abandon")
    (p / "characters" / "rob.character.json").write_text('{"name": "Rob"}')
    (p / "data" / "notes.md").write_text(f"my key is {_FAKE_KEY} ok\n")
    return p


def _member_names(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return sorted(m.name for m in tar.getmembers())


def test_export_excludes_credentials_and_scrubs(env, tmp_path):
    p = _seed_profile(env)
    out = tmp_path / "rob.tar.gz"
    result = export_profile("rob", out)
    names = _member_names(out)
    assert not any(n.endswith(".env") for n in names)
    assert not any("auth.json" in n for n in names)
    assert not any("wallet" in n for n in names)
    assert "rob/characters/rob.character.json" in names
    # planted fake key never appears raw anywhere in the archive
    with tarfile.open(out, "r:gz") as tar:
        blob = b"".join(tar.extractfile(m).read()
                        for m in tar.getmembers() if m.isfile())
    assert _FAKE_KEY.encode() not in blob
    assert result["scrubbed_files"] >= 1
    # live profile untouched
    assert _FAKE_KEY in (p / "data" / "notes.md").read_text()
    assert (p / ".env").is_file() and (p / "auth.json").is_file()


def test_import_roundtrip_and_stability(env, tmp_path):
    _seed_profile(env)
    out1 = tmp_path / "rob1.tar.gz"
    export_profile("rob", out1)
    result = import_profile(out1, name="rob2")
    p2 = result["home"]
    assert (p2 / "characters" / "rob.character.json").is_file()
    assert "POLYROB_INSTANCE_ID=rob2" in (p2 / ".env").read_text()  # re-pinned
    out2 = tmp_path / "rob2.tar.gz"
    export_profile("rob2", out2)
    strip = lambda names, top: sorted(n.split("/", 1)[1] for n in names if "/" in n)
    assert strip(_member_names(out1), "rob") == strip(_member_names(out2), "rob2")


def test_import_refuses_existing_profile(env, tmp_path):
    import click
    _seed_profile(env)
    out = tmp_path / "rob.tar.gz"
    export_profile("rob", out)
    with pytest.raises(click.ClickException, match="already exists"):
        import_profile(out)  # 'rob' is already there


def test_import_refuses_traversal_archive(env, tmp_path):
    import click
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo("rob/../../outside.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(click.ClickException, match="traversal"):
        import_profile(evil, name="x")
    assert not (tmp_path / "outside.txt").exists()


def test_import_refuses_symlink_member(env, tmp_path):
    import click
    evil = tmp_path / "link.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("rob/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(click.ClickException, match="link"):
        import_profile(evil, name="x")


def test_export_materializes_symlinks(env, tmp_path):
    p = _seed_profile(env)
    secret_src = tmp_path / "outside.txt"
    secret_src.write_text("outside content")
    (p / "data" / "linked.txt").symlink_to(secret_src)
    out = tmp_path / "rob.tar.gz"
    export_profile("rob", out)
    with tarfile.open(out, "r:gz") as tar:
        member = tar.getmember("rob/data/linked.txt")
        assert member.isfile() and not member.issym()  # materialized copy
        assert tar.extractfile(member).read() == b"outside content"
    assert (p / "data" / "linked.txt").is_symlink()  # live profile untouched
