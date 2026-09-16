"""Acceptance #5 (2026-08-28): the owner's /status and the agent's agent_status
cannot disagree — both render from ONE snapshot. Plus the other seats: the
doctor report, `polyrob autonomy status --json`, the webview doctor endpoint,
the daily digest and the per-turn <live-health> note all carry the same health
block for the same degraded fixture.
"""
import asyncio
import json
import logging
import os
import time

import pytest

from tests.unit.core.test_status_snapshot import _fake_ledger, _seed_cron, _seed_goals, _seed_telemetry

OWNER = "rob"


@pytest.fixture()
def degraded(tmp_path, monkeypatch):
    data_dir = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", data_dir)
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", os.path.join(data_dir, "telemetry_events.db"))
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("CRON_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", OWNER)
    with open(os.path.join(data_dir, "CREDIT_SENTINEL"), "w") as f:
        json.dump({"providers": {"openrouter": {"ts": time.time() - 60, "release_ts": None,
                                               "reason": "Error code: 402 - Insufficient credits"}}}, f)
    _seed_goals(data_dir)
    _seed_cron(data_dir)
    _seed_telemetry(os.path.join(data_dir, "telemetry_events.db"))
    return data_dir


def _health_block(text: str) -> list:
    """The health lines of a rendered surface: from the 'Health:' headline up
    to the first section line (Session:/Goals:/…) or a non-issue line."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if "Health:" in ln or "health:" in ln)
    out = []
    for ln in lines[start:]:
        core = ln.strip().lstrip("•-").strip()
        if out and not (core.startswith(("⛔", "⚠", "could not verify", "…"))):
            break
        out.append(core.replace("health:", "Health:", 1))
    return out


@pytest.mark.asyncio
async def test_telegram_status_and_agent_status_share_the_health_block(degraded, monkeypatch):
    # --- owner seat: Telegram /status
    from surfaces.telegram.harness import _status_reply

    class _Cfg:
        data_dir = degraded

    class _Container:
        config = _Cfg()

        def get_service(self, name):
            return None

    class _TA:
        container = _Container()

    async def fake_build_ledger(user_id, *, days=7, include_balances=False, db=None):
        return _fake_ledger()

    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger", fake_build_ledger)
    owner_text = await _status_reply(_TA(), OWNER, None, degraded)

    # --- agent seat: the agent_status action
    import agents.task.agent.service  # noqa: F401
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller
    monkeypatch.setenv("AGENT_STATUS_TOOL", "true")
    c = object.__new__(Controller)
    c.logger = logging.getLogger("parity")
    c.registry = Registry()
    c.user_id = OWNER
    c.session_id = "s1"
    c.container = _Container()
    c.list_tools = lambda: ["browser"]
    c._register_agent_status_action()
    action = c.registry.registry.actions["agent_status"]
    result = await action.function(action.param_model(), execution_context=None)
    agent_text = result.extracted_content

    owner_health, agent_health = _health_block(owner_text), _health_block(agent_text)
    assert owner_health[0].startswith("Health: DEGRADED")
    assert owner_health == agent_health, (owner_health, agent_health)
    assert any("credit sentinel TRIPPED for openrouter" in ln for ln in owner_health)


def test_doctor_report_leads_with_the_same_health(degraded):
    from cli.commands.doctor import doctor_report
    lines = doctor_report(dict(os.environ))
    blob = "\n".join(lines)
    assert "health: DEGRADED" in blob
    assert "credit sentinel TRIPPED for openrouter" in blob
    assert "suppressed by the daily cap" in blob
    # health precedes the posture card
    assert blob.index("health: DEGRADED") < blob.index("posture:")


def test_autonomy_status_json_carries_health(degraded, monkeypatch):
    from click.testing import CliRunner
    from cli.commands.autonomy import autonomy
    monkeypatch.setattr("core.identity.resolve_identity", lambda: OWNER)
    res = CliRunner().invoke(autonomy, ["status", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["health"]["overall"] == "degraded"
    assert any(i["key"] == "credit_sentinel:openrouter" for i in payload["health"]["items"])


def test_digest_leads_with_health(degraded, monkeypatch):
    from cron import digest
    monkeypatch.setattr(digest, "_ledger", lambda uid, days: _fake_ledger())
    monkeypatch.setattr(digest, "_episodes", lambda uid, since: [])
    text = asyncio.run(digest.compose_digest(OWNER, data_dir=degraded))
    lines = text.splitlines()
    assert lines[1].startswith("Health: DEGRADED")
    assert "credit sentinel TRIPPED" in text


def test_live_health_note_matches_the_owner_view(degraded):
    from agents.task.agent.core.live_health import build_live_health_text
    from core.status_snapshot import build_status_snapshot
    from core.status_render import health_headline
    note = build_live_health_text(OWNER, data_dir=degraded)
    snap = build_status_snapshot(OWNER, data_dir=degraded, include_money=False)
    assert health_headline(snap) in note
    assert "credit sentinel TRIPPED for openrouter" in note


def _request(path: str = "/api/webgate/doctor"):
    """A REAL ``starlette.requests.Request``.

    ⚠️ 043 W13 gave ``api_doctor`` a ``request`` parameter so the snapshot is
    built for the CALLER's tenant rather than always the instance owner's — a
    multitenant leak. This test called it with none and broke; passing a real
    Request rather than a stub is what keeps it exercising the actual signature,
    including the ``_effective_user_id`` resolution W13 added.
    """
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": [], "query_string": b""})


@pytest.mark.asyncio
async def test_webview_doctor_endpoint_carries_health(degraded, monkeypatch):
    from webview import pages
    monkeypatch.setattr(pages.webgate, "local_owner_id", lambda: OWNER)
    monkeypatch.setattr(pages, "_data_dir", lambda: degraded)
    resp = await pages.api_doctor(_request())
    body = json.loads(resp.body)
    assert body["health"]["overall"] == "degraded"
    assert body["health"]["lines"][0].startswith("Health: DEGRADED")
    assert any("Goals:" in ln for ln in body["status_lines"])


@pytest.mark.asyncio
async def test_the_doctor_endpoint_builds_for_the_CALLERS_tenant(degraded,
                                                                  monkeypatch):
    """The reason W13 added the parameter: before it, an authenticated
    multitenant tenant was served the INSTANCE OWNER's health, goals, cron and
    money. Pinned here so the argument cannot quietly become decorative."""
    from webview import pages
    seen = []
    monkeypatch.setattr(pages, "_data_dir", lambda: degraded)
    monkeypatch.setattr(pages, "_effective_user_id",
                        lambda request: seen.append(request) or OWNER)
    resp = await pages.api_doctor(_request())
    assert len(seen) == 1
    assert seen[0].url.path == "/api/webgate/doctor"
    assert json.loads(resp.body)["health"]["overall"] == "degraded"
