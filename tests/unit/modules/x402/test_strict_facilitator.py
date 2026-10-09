"""The HTTP success code alone is never proof that money settled."""
import base64
import json
from types import SimpleNamespace
import httpx
import pytest
from fastapi_x402.models import PaymentRequirements
from modules.x402.facilitator import StrictFacilitator, SettlementPending


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement,success", [
    # DEFI-2: an explicit refusal naming no transaction is DEFINITE, not pending.
    ({"success": False, "transaction": "", "network": "base"}, "rejected"),
    ({"success": False, "transaction": "0x" + "1"*64, "network": "base"}, False),
    ({"success": "true", "transaction": "0x" + "1"*64, "network": "base"}, False),
    ({"success": True, "transaction": "", "network": "base"}, False),
    ({"success": True, "transaction": "0x" + "1"*64, "network": "base-sepolia"}, False),
    ({"success": True, "transaction": "0x" + "1"*64, "network": "base"}, True),
])
async def test_settlement_body_is_authoritative(settlement, success, capsys):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"isValid": True} if request.url.path == "/verify" else settlement)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        upstream = SimpleNamespace(client=http, verify_url="https://x402.org/verify",
            settle_url="https://x402.org/settle", is_coinbase_cdp=False,
            _create_coinbase_headers=lambda endpoint: {"Authorization": "Bearer synthetic-secret"})
        adapter = StrictFacilitator(upstream)
        header = base64.b64encode(json.dumps({"x402Version": 1}).encode()).decode()
        requirements = PaymentRequirements(network="base", maxAmountRequired="10000",
            resource="/v1/chat/completions", payTo="0x"+"2"*40, asset="0x"+"3"*40)
        if success == "rejected":
            verified, settled = await adapter.verify_and_settle_payment(header, requirements)
            assert verified.isValid and settled.success is False
        elif success:
            verified, settled = await adapter.verify_and_settle_payment(header, requirements)
            assert verified.isValid and settled.success
        else:
            with pytest.raises(SettlementPending):
                await adapter.verify_and_settle_payment(header, requirements)
    assert len(calls) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_redirect_not_followed():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as http:
        upstream = SimpleNamespace(client=http, verify_url="https://x402.org/verify",
            is_coinbase_cdp=False, _create_coinbase_headers=lambda endpoint: {})
        adapter = StrictFacilitator(upstream)
        req = PaymentRequirements(network="base", maxAmountRequired="1", resource="/",
            payTo="0x"+"2"*40, asset="0x"+"3"*40)
        with pytest.raises(ValueError):
            await adapter.verify_and_settle_payment("e30=", req)
    assert len(calls) == 1
