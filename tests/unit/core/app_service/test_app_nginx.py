"""032 — the two-substitution stanza and the test-then-reload applier."""
import asyncio

import pytest

from core.app_service.nginx import NginxApplier, render_stanza


def test_stanza_has_exactly_the_two_substitutions():
    text = render_stanza(slug="demo-status", host_port=18000, base_domain="apps.example.test",
                         cert_dir="/etc/letsencrypt/live/apps.example.test")
    assert "server_name demo-status.apps.example.test;" in text
    assert "proxy_pass http://127.0.0.1:18000/;" in text
    assert "ssl_certificate     /etc/letsencrypt/live/apps.example.test/fullchain.pem;" in text
    assert "ssl_certificate_key /etc/letsencrypt/live/apps.example.test/privkey.pem;" in text
    assert 'add_header X-Content-Type-Options "nosniff" always;' in text
    assert "client_max_body_size 1m;" in text
    assert "{" in text and "{slug}" not in text and "{port}" not in text


@pytest.mark.parametrize("bad", [
    dict(slug="a;b"), dict(slug="Demo"), dict(slug="a..b"), dict(host_port=0),
    dict(host_port=70000), dict(base_domain="apps example"), dict(cert_dir="relative/dir"),
    dict(cert_dir="/etc/x; include /etc/passwd"),
])
def test_invalid_inputs_refused(bad):
    kw = dict(slug="ok", host_port=18000, base_domain="apps.example.test", cert_dir="/etc/c")
    kw.update(bad)
    with pytest.raises(ValueError):
        render_stanza(**kw)


class _Runner:
    def __init__(self, fail_test=False):
        self.calls = []
        self.fail_test = fail_test

    async def __call__(self, argv, *, input=None, timeout=None):
        self.calls.append(list(argv))
        if argv[:2] == ["nginx", "-t"] and self.fail_test:
            return (1, "", "nginx: [emerg] bad directive")
        return (0, "", "")


def test_apply_writes_tests_reloads(tmp_path):
    r = _Runner()
    a = NginxApplier(str(tmp_path / "apps.d"), r)
    ok, err = asyncio.run(a.apply("st", "server {}"))
    assert ok and err == ""
    assert (tmp_path / "apps.d" / "st.conf").read_text() == "server {}"
    assert r.calls == [["nginx", "-t"], ["systemctl", "reload", "nginx"]]
    ok, _ = asyncio.run(a.remove("st"))
    assert ok and not (tmp_path / "apps.d" / "st.conf").exists()
    assert r.calls[-2:] == [["nginx", "-t"], ["systemctl", "reload", "nginx"]]
    ok, _ = asyncio.run(a.remove("st"))  # idempotent, no reload
    assert ok and len(r.calls) == 4


def test_rejected_stanza_is_removed_or_restored_before_any_reload(tmp_path):
    r = _Runner(fail_test=True)
    a = NginxApplier(str(tmp_path / "apps.d"), r)
    ok, err = asyncio.run(a.apply("st", "bad"))
    assert not ok and "emerg" in err
    assert not (tmp_path / "apps.d" / "st.conf").exists()
    assert ["systemctl", "reload", "nginx"] not in r.calls
    # a live stanza survives a bad replacement
    (tmp_path / "apps.d").mkdir(exist_ok=True)
    (tmp_path / "apps.d" / "st.conf").write_text("good")
    ok, _ = asyncio.run(a.apply("st", "worse"))
    assert not ok and (tmp_path / "apps.d" / "st.conf").read_text() == "good"
    with pytest.raises(ValueError):
        a.path_for("../etc")


def test_stanza_is_self_contained_tls():
    """A DNS-01 `certbot certonly` box has neither options-ssl-nginx.conf nor
    ssl-dhparams.pem (only the certbot NGINX plugin writes them), so the stanza
    must name no file outside the cert dir — else every app fails `nginx -t`."""
    text = render_stanza(slug="demo-status", host_port=18000, base_domain="apps.example.test",
                         cert_dir="/etc/letsencrypt/live/apps.example.test")
    assert "options-ssl-nginx.conf" not in text and "ssl_dhparam" not in text
    assert "include " not in text
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in text
    assert "ssl_session_tickets off;" in text
    ciphers = text.split("ssl_ciphers ", 1)[1].split(";", 1)[0].split(":")
    assert ciphers and all(c.startswith("ECDHE-") for c in ciphers)  # no DHE: no dhparam file
    # `http2 on;` is nginx >= 1.25.1; Ubuntu 22.04/24.04 ship 1.18/1.24.
    assert "http2 on;" not in text and "listen 443 ssl http2;" in text


def test_static_publish_vhost():
    from core.app_service.config import RESERVED_SLUGS, valid_app_slug
    from core.app_service.nginx import STATIC_LABEL, render_static_vhost
    text = render_static_vhost(base_domain="apps.example.test", cert_dir="/etc/c",
                               root="/var/www/polyrob-publish")
    assert "server_name static.apps.example.test;" in text
    assert "root /var/www/polyrob-publish;" in text
    assert "ssl_certificate     /etc/c/fullchain.pem;" in text
    assert "options-ssl-nginx.conf" not in text and "ssl_protocols TLSv1.2 TLSv1.3;" in text
    assert "location ~ /\\. { return 404; }" in text  # .pending/<slug>/ is never served
    assert "disable_symlinks on;" in text and "autoindex off;" in text
    # the static host can never be claimed by an app
    assert STATIC_LABEL in RESERVED_SLUGS and not valid_app_slug(STATIC_LABEL)


@pytest.mark.parametrize("bad", [dict(root="relative"), dict(root="/var/www/a b"),
                                 dict(root="/x;y"), dict(base_domain="a b"),
                                 dict(cert_dir="rel")])
def test_static_vhost_refuses_bad_inputs(bad):
    from core.app_service.nginx import render_static_vhost
    kw = dict(base_domain="apps.example.test", cert_dir="/etc/c", root="/var/www/p")
    kw.update(bad)
    with pytest.raises(ValueError):
        render_static_vhost(**kw)
