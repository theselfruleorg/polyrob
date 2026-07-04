from cli.keys import first_run_no_config


def test_first_run_true_when_no_env_and_no_key(tmp_path):
    assert first_run_no_config(env={}, home=tmp_path / ".polyrob") is True


def test_first_run_false_when_env_exists(tmp_path):
    home = tmp_path / ".polyrob"
    home.mkdir(parents=True)
    (home / ".env").write_text("DEFAULT_MODEL=x\n")
    assert first_run_no_config(env={}, home=home) is False


def test_first_run_false_when_key_present(tmp_path):
    # A well-formed key (>= 20 chars) means it's not a first run.
    assert first_run_no_config(
        env={"OPENROUTER_API_KEY": "sk-or-realkey-0123456789"},
        home=tmp_path / ".polyrob") is False
