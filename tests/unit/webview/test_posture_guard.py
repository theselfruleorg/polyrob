"""S8 (2026-09-14): the console must refuse to boot at `local` posture on a
server-shaped deployment.

`webgate.posture()` returns `"local"` — every anonymous request is the owner —
whenever `POLYROB_POSTURE` and `WEBGATE_HOST`/`WEBVIEW_HOST` are all unset. The
shipped unit passes `--host 127.0.0.1` on the uvicorn ARGV, which `posture()`
cannot see, and nginx proxies the internet into that loopback socket. So the
failure is silent: lose `/etc/polyrob/webview.env` and the console serves the
owner control plane to the world with no login and no error.

The guard reads the signals `posture()` structurally cannot: a public URL, the
reverse-proxy argv, and a system data directory.
"""
import pytest

from webview import posture_guard as pg


def _assert(env, argv=(), posture="local"):
    pg.assert_console_posture(env=env, argv=list(argv), posture=posture)


def test_plain_local_workstation_starts():
    """No server signal at all ⇒ the loopback primitive is untouched."""
    _assert({})


def test_public_url_at_local_posture_refuses():
    with pytest.raises(RuntimeError) as e:
        _assert({"WEBVIEW_PUBLIC_URL": "https://console.example.com"})
    assert "WEBVIEW_PUBLIC_URL" in str(e.value)
    assert "POLYROB_POSTURE" in str(e.value)   # names the remedy


@pytest.mark.parametrize("arg", ["--proxy-headers",
                                 "--forwarded-allow-ips=127.0.0.1",
                                 "--forwarded-allow-ips"])
def test_reverse_proxy_argv_at_local_posture_refuses(arg):
    with pytest.raises(RuntimeError) as e:
        _assert({}, argv=["uvicorn", "webview.server:app", "--host", "127.0.0.1", arg])
    assert "reverse proxy" in str(e.value)


def test_system_data_dir_at_local_posture_refuses():
    with pytest.raises(RuntimeError) as e:
        _assert({"POLYROB_DATA_DIR": "/var/lib/polyrob"})
    assert "POLYROB_DATA_DIR" in str(e.value)


def test_profile_data_dir_under_home_is_not_a_server_signal(tmp_path, monkeypatch):
    """A workstation profile sets POLYROB_DATA_DIR too (`~/.polyrob/profiles/x/data`).
    Refusing there would break `polyrob dashboard -P <profile>` — the signal is a
    SYSTEM data directory, not any data directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _assert({"POLYROB_DATA_DIR": str(tmp_path / ".polyrob" / "profiles" / "x" / "data")})


def test_own_ops_posture_never_refuses():
    _assert({"WEBVIEW_PUBLIC_URL": "https://console.example.com",
             "POLYROB_DATA_DIR": "/var/lib/polyrob"},
            argv=["uvicorn", "--proxy-headers"], posture="own_ops")


def test_escape_hatch_allows_it(caplog):
    """An operator who genuinely wants an anonymous console behind their own
    auth layer says so explicitly — and it is logged, never silent."""
    import logging
    # Pin capture on the guard's own logger: an earlier test in a full run may
    # have reconfigured the root logger (level / propagate), which hid this
    # record and made the test order-dependent.
    caplog.set_level(logging.WARNING, logger=pg.__name__)
    logging.getLogger(pg.__name__).propagate = True
    _assert({"WEBVIEW_PUBLIC_URL": "https://console.example.com",
             "WEBVIEW_ALLOW_LOCAL_POSTURE": "1"})
    assert any("WEBVIEW_ALLOW_LOCAL_POSTURE" in r.message for r in caplog.records)


def test_signals_are_reported_together():
    with pytest.raises(RuntimeError) as e:
        _assert({"WEBVIEW_PUBLIC_URL": "https://c.example.com",
                 "POLYROB_DATA_DIR": "/var/lib/polyrob"},
                argv=["uvicorn", "--proxy-headers"])
    msg = str(e.value)
    for token in ("WEBVIEW_PUBLIC_URL", "reverse proxy", "POLYROB_DATA_DIR"):
        assert token in msg


def test_default_reads_the_live_process(monkeypatch):
    """Called with no arguments it must read os.environ / sys.argv / posture()
    itself — that is how the lifespan calls it."""
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("WEBVIEW_PUBLIC_URL", "https://console.example.com")
    monkeypatch.delenv("WEBVIEW_ALLOW_LOCAL_POSTURE", raising=False)
    with pytest.raises(RuntimeError):
        pg.assert_console_posture()


def test_lifespan_calls_the_guard():
    """Wiring, not just the predicate: the startup handler must invoke it."""
    import inspect
    import webview.server as server
    src = inspect.getsource(server.startup_event)
    assert "assert_console_posture" in src
