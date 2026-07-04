"""Guards for the nginx reconcile (doc 04): one canonical static config + a demoted
multitenant-proxy variant; deploy installs the canonical one.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CANONICAL = REPO / "deployment" / "nginx-polyrob.conf"
PROXY = REPO / "deployment" / "nginx.conf"
DEPLOY = REPO / "deployment" / ".." / "deploy_unified.sh"  # repo-root deploy_unified.sh


def _deploy_text() -> str:
    return (REPO / "deploy_unified.sh").read_text()


def test_canonical_serves_static_not_proxy():
    """nginx-polyrob.conf serves the static landing root and does NOT proxy / to :5050."""
    text = CANONICAL.read_text()
    assert "/opt/polyrob/deployment/placeholder" in text or \
           "/opt/polyrob/deployment/landing" in text, "canonical root not under /opt/polyrob"
    # The canonical (launch) config must not proxy the site root to the webview.
    assert "proxy_pass http://127.0.0.1:5050" not in text, \
        "canonical config must not proxy / to the multitenant webview"


def test_proxy_variant_demoted():
    """nginx.conf carries the loud 'NOT the launch default' header (deferred multitenant)."""
    text = PROXY.read_text()
    assert "NOT THE LAUNCH DEFAULT" in text.upper()
    assert "5050" in text  # it IS the proxy variant (proxies to webview)


def test_deploy_installs_canonical_config():
    """deploy_unified.sh installs the canonical static config, not the proxy variant."""
    text = _deploy_text()
    assert "nginx-polyrob.conf" in text, "deploy must install the canonical nginx-polyrob.conf"
    # It must not copy the proxy nginx.conf into sites-available as the install step.
    assert "deployment/nginx.conf /etc/nginx" not in text, \
        "deploy must not install the multitenant-proxy nginx.conf at launch"


def test_deploy_dir_is_polyrob():
    """nginx root == deploy dir == /opt/polyrob (doc 02 landed this; this re-locks it)."""
    text = _deploy_text()
    assert 'DEPLOY_DIR="/opt/polyrob"' in text
