import pytest

from core.security.workspace_io import UnsafePath, read_trusted_config


def test_private_file_in_replaceable_directory_is_not_trusted(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o770)
    parent.chmod(0o770)
    path = parent / "providers.yaml"
    path.write_text("providers: {}")
    path.chmod(0o600)
    with pytest.raises(UnsafePath, match="ancestor"):
        read_trusted_config(path)


def test_trusted_config_refuses_links_and_oversized_files(tmp_path):
    path = tmp_path / "providers.yaml"
    path.write_text("providers: {}")
    path.chmod(0o600)
    assert read_trusted_config(path) == "providers: {}"
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_trusted_config(link)
    with pytest.raises(UnsafePath, match="large"):
        read_trusted_config(path, max_bytes=5)
