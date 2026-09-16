"""043 W9 — the launcher runs uvicorn with proxy headers.

Two console endpoints authorise by CLIENT ADDRESS: `POST /api/internal/emit`
(localhost only) and the `local`-posture bypasses. Behind nginx every request
arrives from 127.0.0.1, so without `proxy_headers` uvicorn reports the PROXY as
the client and those checks answer "localhost" for the whole internet.

`forwarded_allow_ips="127.0.0.1"` is the other half: with it, uvicorn honours
`X-Forwarded-For` only from the local proxy, so a header a remote client sets
itself is ignored rather than believed.
"""
import sys
import types


def test_uvicorn_is_run_with_proxy_headers(monkeypatch):
    import webview.server_launcher as launcher

    captured = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(launcher.uvicorn, "run", fake_run)
    monkeypatch.setattr(launcher, "setup_logging", lambda level: __import__("logging").getLogger("t"))
    monkeypatch.setattr(launcher, "load_environment", lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "webview.server",
                        types.SimpleNamespace(app=object()))
    monkeypatch.setattr(sys, "argv", ["server_launcher.py"])

    launcher.main()

    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == "127.0.0.1"


def test_the_shipped_units_never_trust_every_client():
    """``--forwarded-allow-ips=*`` makes uvicorn believe the X-Forwarded-For of
    ANY client, so a direct caller can claim 127.0.0.1 — and two console
    endpoints authorise by client address. Trust the header only from the proxy
    whose address you name."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3] / "deployment"
    for unit in sorted(root.glob("*webview*.service")) + sorted(root.glob("*webgate*.service")):
        text = unit.read_text(encoding="utf-8")
        if "--forwarded-allow-ips" not in text:
            continue
        assert "--forwarded-allow-ips=*" not in text, unit.name
        assert "--forwarded-allow-ips=127.0.0.1" in text, unit.name
