"""deployment/polyrob-apps.service is rebuildable from the repo and runs the supervisor."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_unit_file_shape():
    text = (REPO / "deployment" / "polyrob-apps.service").read_text()
    for needle in ("ExecStart=/opt/polyrob/venv/bin/polyrob apps supervise", "User=root",
                   "EnvironmentFile=-/etc/polyrob/polyrob.env", "Restart=on-failure",
                   "After=network-online.target",
                   "WantedBy=multi-user.target"):
        assert needle in text, needle


def test_deploy_restarts_the_supervisor_with_the_family():
    text = (REPO / "scripts" / "deploy_prod.sh").read_text()
    assert text.count("polyrob-apps.service") >= 2


def test_setup_script_is_owner_run_and_names_the_bundle():
    text = (REPO / "scripts" / "setup_apps_vhost.sh").read_text()
    assert "certbot certonly --manual --preferred-challenges dns" in text
    assert "AGENT_BUILDER_MODE=ship" in text and "polyrob-apps.service" in text
    assert 'if [[ "$(id -u)" != "0" ]]' in text
