from pathlib import Path
import pytest
from core.security.secret_guard import is_secret_path, is_credential_file


@pytest.mark.parametrize('name', ['wallet.env', 'polyrob.env', 'WALLET.ENV', 'backup.env', '.env', '.env.production'])
def test_env_files_are_secret_on_both_read_paths(name):
    path = Path('/workspace/project') / name
    assert is_credential_file(path)
    assert is_secret_path(path, root=path.parent)


def test_ordinary_source_is_not_a_secret():
    path = Path('/workspace/project/environment.py')
    assert not is_credential_file(path)
    assert not is_secret_path(path, root=path.parent)
