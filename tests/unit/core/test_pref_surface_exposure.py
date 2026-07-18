"""T10 exposure contract (corrections item 7): every `PREF_SCHEMA` key must be
visible on all three control-plane surfaces —

  (a) the webview `/api/webgate/preferences` GET payload,
  (b) the Telegram `/config` no-args listing,
  (c) `polyrob config show` output.

A key silently missing from one surface is a real product bug (an owner using
that surface would never learn the knob exists) — this test pins the contract
so a future PREF_SCHEMA addition can't quietly regress one surface.
"""
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.prefs import PREF_SCHEMA


# --- (a) webview preferences GET ---------------------------------------------

def test_webview_preferences_payload_covers_every_schema_key(monkeypatch, tmp_path):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    app = FastAPI()
    app.include_router(pages.router)
    client = TestClient(app)

    r = client.get("/api/webgate/preferences")
    assert r.status_code == 200
    keys = {item["key"] for item in r.json()["preferences"]}
    missing = set(PREF_SCHEMA) - keys
    assert not missing, f"webview preferences payload is missing: {sorted(missing)}"


# --- (b) Telegram /config no-args listing ------------------------------------

@pytest.mark.asyncio
async def test_telegram_config_listing_covers_every_schema_key(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    from core.surfaces.dispatcher import RouteDecision, RouteKind
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    from surfaces.telegram.harness import act_on_inbound
    from surfaces.telegram.inbound import InboundResult

    class _Cfg:
        def __init__(self, data_dir):
            self.data_dir = data_dir

    class _Container:
        def __init__(self, data_dir):
            self.config = _Cfg(data_dir)

        def get_service(self, name):
            return None

    class _Agent:
        def __init__(self, data_dir):
            self.container = _Container(data_dir)

    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text="/config",
                             identity=Identity(user_id="gleb", source=src, raw_user_id="555"))
    result = InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:gleb", command="/config"))

    out = await act_on_inbound(_Agent(str(tmp_path)), result)
    missing = [key for key in PREF_SCHEMA if key not in out]
    assert not missing, f"Telegram /config listing is missing: {missing}"


# --- (c) `polyrob config show` -----------------------------------------------

def test_polyrob_config_show_covers_every_schema_key(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".polyrob").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)

    from cli.commands.config import config as config_group

    res = CliRunner().invoke(config_group, ["show"])
    assert res.exit_code == 0, res.output
    missing = [key for key in PREF_SCHEMA if key not in res.output]
    assert not missing, f"`polyrob config show` is missing: {missing}"
