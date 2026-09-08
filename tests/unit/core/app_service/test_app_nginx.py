"""032 — the two-substitution stanza and the test-then-reload applier."""
import asyncio

import pytest

from core.app_service.nginx import NginxApplier, render_stanza


def test_stanza_has_exactly_the_two_substitutions():
    text = render_stanza(slug="rob-status", host_port=18000, base_domain="apps.example.test",
                         cert_dir="/etc/letsencrypt/live/apps.example.test")
    assert "server_name rob-status.apps.example.test;" in text
    assert "proxy_pass http://127.0.0.1:18000/;" in text
    assert "ssl_certificate     /etc/letsencrypt/live/apps.example.test/fullchain.pem;" in text
    assert "ssl_certificate_key /etc/letsencrypt/live/apps.example.test/privkey.pem;" in text
    assert 'add_header X-Content-Type-Options "nosniff" always;' in text
    assert "client_max_body_size 1m;" in text
    assert "{" in text and "{slug}" not in text and "{port}" not in text


@pytest.mark.parametrize("bad", [
    dict(slug="a;b"), dict(slug="Rob"), dict(slug="a..b"), dict(host_port=0),
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
