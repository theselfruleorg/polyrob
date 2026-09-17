"""``data_home_db_path``: env override, explicit home, and the read-both rule
for the dbs that historically lived under the container's ``config.data_dir``."""
import os

from core.runtime_paths import data_home_db_path


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("ZZ_DB_PATH", str(tmp_path / "x.db"))
    assert data_home_db_path("a.db", env_key="ZZ_DB_PATH") == str(tmp_path / "x.db")


def test_explicit_home_wins_over_container(monkeypatch, tmp_path):
    assert data_home_db_path("a.db", data_dir=str(tmp_path), prefer_container=True) \
        == os.path.join(str(tmp_path), "a.db")


def test_fresh_install_lands_on_the_manifest_path(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    assert data_home_db_path("a.db", prefer_container=True) == os.path.join(str(tmp_path), "a.db")


def test_existing_legacy_container_file_keeps_being_used(monkeypatch, tmp_path):
    """Read-both/write-new: history is never forked across two files."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    legacy_dir = tmp_path / "data"; legacy_dir.mkdir()
    (legacy_dir / "a.db").write_bytes(b"")

    class _Cfg:
        data_dir = str(legacy_dir)

    class _Container:
        @staticmethod
        def get_service(name):
            return _Cfg() if name == "config" else None

    import core.runtime_paths as rp
    from core import container as container_mod
    monkeypatch.setattr(container_mod.DependencyContainer, "get_instance",
                        classmethod(lambda cls, *a, **k: _Container()))
    assert rp.data_home_db_path("a.db", prefer_container=True) == str(legacy_dir / "a.db")
    # once the manifest-path file exists it wins
    (tmp_path / "a.db").write_bytes(b"")
    assert rp.data_home_db_path("a.db", prefer_container=True) == str(tmp_path / "a.db")
