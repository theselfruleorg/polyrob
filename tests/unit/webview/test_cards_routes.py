"""Action cards on the console: the Inbox lists the owner's open cards, a tap
posts the card's own token through the console verb plane, and a money quote
typed in the chat box comes back as a card whose Confirm runs in the
background (never holding the request)."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.surfaces import cards

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"


class _Gate:
    per_tx_cap_usd = 3000.0
    daily_cap_usd = 7000.0

    def rolling_24h_spend_usd(self, venue=None):
        return 0.0


class _Tool:
    def __init__(self):
        self.calls = []

    def _get_wallet(self):
        return SimpleNamespace(policy=_Gate())

    async def transfer(self, params, ctx):
        self.calls.append(params)
        body = ("transfer\n  simulated value: $100.0000\n"
                + ("  RESULT: DRY RUN (simulation only)" if params.dry_run
                   else "  RESULT: SENT AND CONFIRMED\n  tx: 0xabc"))
        return SimpleNamespace(error=None, extracted_content=body)


class _Agent:
    def __init__(self, d):
        self.container = SimpleNamespace(config=SimpleNamespace(data_dir=d),
                                         get_service=lambda name: None)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import webview.pages as pages
    import webview.server as server
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "rob")
    monkeypatch.setattr("core.wallet.authority.owner_refusal", lambda uid: None)
    agent = _Agent(str(tmp_path))
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: agent)
    tool = _Tool()
    monkeypatch.setattr("tools.defi.trade_tool.DefiTradeTool", lambda: tool)
    notices = []

    async def _deliver(container, user_id, text, **kw):
        notices.append(text)
        return "no_sink"
    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    return SimpleNamespace(agent=agent, tool=tool, notices=notices)


@pytest.fixture
def client(rig):
    import webview.cards_routes as routes
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _quote_card(user="rob"):
    reply = (f"transfer\n  RESULT: DRY RUN\n\nTo send it: /send 1 native to {ADDR} "
             f"on base max 105.01 go")
    return cards.quote_card(user, "/send", ["1", "native", "to", ADDR, "on", "base"],
                            reply)[1]


def test_the_inbox_lists_open_cards_with_their_buttons(client):
    card = _quote_card()
    body = client.get("/api/webgate/cards").json()
    assert body["readable"] is True
    (row,) = body["cards"]
    assert row["id"] == card.card_id
    assert [b["act"] for b in row["buttons"]] == ["ok", "re", "no"]


def test_cancel_decides_the_card_and_the_answer_redraws_it(client):
    card = _quote_card()
    r = client.post(f"/api/webgate/cards/{card.card_id}/no").json()
    assert r["ok"] and "Cancelled" in r["message"]
    assert r["card"]["state"] == cards.S_CANCELLED and r["card"]["buttons"] == []
    again = client.post(f"/api/webgate/cards/{card.card_id}/no").json()
    assert not again["ok"]


def test_a_choice_is_answered_from_the_inbox(client):
    card = cards.choice_card("rob", "Which chain?", ["Base", "Ethereum"])
    r = client.post(f"/api/webgate/cards/{card.card_id}/1").json()
    assert r["ok"] and cards.store().get(card.card_id).answer == "Base"


def test_another_tenants_card_is_no_card(client):
    card = _quote_card(user="mallory")
    r = client.post(f"/api/webgate/cards/{card.card_id}/ok").json()
    assert not r["ok"] and "No such card" in r["message"]
    assert cards.store().get(card.card_id).state == cards.S_OPEN


def test_a_bad_tap_is_404(client):
    assert client.post("/api/webgate/cards/zz/ok").status_code == 404
    assert client.post("/api/webgate/cards/0123456789/go").status_code == 404


def test_the_chat_box_quote_is_a_card_and_confirm_runs_in_the_background(rig):
    from webview.console_commands import maybe_handle_console_command

    async def _go():
        quote = await maybe_handle_console_command(
            rig.agent, "s1", "rob", f"/send 1 native to {ADDR} on base")
        assert isinstance(quote, str) and "/card_" in quote
        (card,) = cards.store().open_cards("rob")
        started = await maybe_handle_console_command(
            rig.agent, "s1", "rob", f"/card_{card.card_id}_ok")
        assert started.startswith("⏳ Send started")
        for _ in range(100):
            if cards.store().get(card.card_id).state != cards.S_CONFIRMED:
                break
            await asyncio.sleep(0.02)
        return card

    card = asyncio.run(_go())
    assert cards.store().get(card.card_id).state == cards.S_DONE
    live = [p for p in rig.tool.calls if not p.dry_run]
    assert len(live) == 1 and live[0].max_spend_usd == pytest.approx(105.01)
    assert any("SENT" in n for n in rig.notices)


def test_a_tap_token_on_a_cold_open_is_a_console_verb():
    from webview.console_commands import looks_like_console_verb
    assert looks_like_console_verb("/card_0123456789_ok")
    assert looks_like_console_verb("/approve_p_a1b2c3")
    assert not looks_like_console_verb("/card_nothex_ok")


def test_form_line_builds_only_a_quote_line():
    line, why = cards.form_line("send", {"amount": "1", "token": "native", "to": ADDR,
                                         "chain": "Base"})
    assert line == ["/send", "1", "native", "to", ADDR, "on", "base"] and why is None
    line, _ = cards.form_line("swap", {"amount": "1", "token": "native", "to": ADDR,
                                       "chain": "base", "slippage": "50"})
    assert line[-2:] == ["slippage", "50"]
    for verb, form in (("bridge", {"amount": "1"}),
                       ("send", {"amount": "0", "token": "native", "to": ADDR, "chain": "base"}),
                       ("send", {"amount": "1", "token": "native", "to": "go", "chain": "base"}),
                       ("send", {"amount": "1", "token": "<x>", "to": ADDR, "chain": "base"}),
                       ("send", {"amount": "1", "token": "native", "chain": "base"})):
        assert cards.form_line(verb, form)[0] is None, (verb, form)


def test_the_form_quote_answers_with_its_card(client, rig):
    r = client.post("/api/webgate/cards/quote",
                    json={"verb": "send", "amount": "1", "token": "native", "to": ADDR,
                          "chain": "base"}).json()
    assert r["ok"] and r["card"]["buttons"][0]["act"] == "ok"
    assert [p.dry_run for p in rig.tool.calls] == [True]       # a quote, nothing sent
    bad = client.post("/api/webgate/cards/quote", json={"verb": "send", "amount": "x"})
    assert bad.status_code == 400


def test_a_refused_quote_never_returns_an_older_card(client, rig, monkeypatch):
    _quote_card()                                              # an older open card
    monkeypatch.setattr("webview.console_commands.run_console_line",
                        lambda *a, **k: _async("❌ refused"))
    r = client.post("/api/webgate/cards/quote",
                    json={"verb": "send", "amount": "1", "token": "native", "to": ADDR,
                          "chain": "base"}).json()
    assert r["ok"] is False and r["card"] is None and "refused" in r["message"]


async def _async(v):
    return v


def test_pickers_name_what_they_could_not_read(client, monkeypatch):
    monkeypatch.setattr("core.wallet.token_trust.trust_view",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    body = client.get("/api/webgate/cards/pickers").json()
    assert "tokens" in body["unreadable"] and isinstance(body["chains"], list)
