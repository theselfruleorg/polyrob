"""SEC-1 detector: does a directory look like a secrets/code tree?"""


def test_detects_env_and_git(tmp_path):
    (tmp_path / ".env.production").write_text("SECRET=1")
    (tmp_path / ".git").mkdir()
    from core.secret_scan import looks_like_secrets_tree
    found = looks_like_secrets_tree(str(tmp_path))
    assert ".env.production" in found
    assert ".git" in found


def test_clean_dir_is_empty(tmp_path):
    (tmp_path / "notes.md").write_text("hi")
    from core.secret_scan import looks_like_secrets_tree
    assert looks_like_secrets_tree(str(tmp_path)) == []


def test_detects_pem_and_credentials(tmp_path):
    (tmp_path / "key.pem").write_text("x")
    (tmp_path / "credentials.json").write_text("{}")
    from core.secret_scan import looks_like_secrets_tree
    found = looks_like_secrets_tree(str(tmp_path))
    assert "key.pem" in found and "credentials.json" in found
