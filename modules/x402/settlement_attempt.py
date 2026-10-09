"""Persist inbound submissions in the existing payment ledger BEFORE network I/O.

Only the authorization's public fields are retained, never a signature. A timeout,
disconnect or process crash cannot make a submitted invoice payable again.
"""
import base64
import hashlib
import json
import re

from modules.x402._db import resolve_db
from modules.x402.facilitator import SettlementPending


def authorization_record(header: str, requirements) -> dict:
    if len(header) > 65536:
        raise ValueError("payment header too large")
    payment = json.loads(base64.b64decode(header, validate=True))
    auth = payment["payload"]["authorization"]
    sender, nonce = auth["from"], auth["nonce"]
    if (not isinstance(sender, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", sender)
            or not isinstance(nonce, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", nonce)):
        raise ValueError("invalid payment authorization identity")
    identity = [requirements.network, requirements.asset.lower(), sender.lower(), nonce.lower()]
    key = "x402_auth_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return {"id": key, "payer": sender.lower(), "nonce": nonce.lower(),
            "valid_before": int(auth["validBefore"]), "asset": requirements.asset,
            "network": requirements.network, "recipient": requirements.payTo,
            "amount": requirements.maxAmountRequired}


async def mark_invoice_submitted(request_id: str, details: dict, *, db=None) -> None:
    database = await resolve_db(db)
    if database is None:
        raise RuntimeError("payment ledger unavailable")
    cur = await database.execute(
        "UPDATE x402_payment_requests SET metadata = json_set(COALESCE(metadata, '{}'), "
        "'$.facilitator_submitted', 1, '$.authorization', json(?)), updated_at=datetime('now') "
        "WHERE id=? AND status='settling' "
        "AND COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=0",
        (json.dumps(details), request_id))
    if cur.rowcount != 1:
        raise SettlementPending("Invoice already submitted or no longer claimed")


async def prepare_machine_payment(details: dict, *, amount_usd: float,
                                  user_id: str, tenant_id: str, db=None) -> str:
    database = await resolve_db(db)
    if database is None:
        raise RuntimeError("payment ledger unavailable")
    metadata = json.dumps({"kind": "machine_payment", "tenant_id": tenant_id,
                           "payer_user_id": user_id, "facilitator_submitted": 1,
                           "authorization": details})
    # user_id is assigned after profile binding succeeds; retain the payer's
    # derived id in metadata without depending on that later foreign-key write.
    cur = await database.execute(
        "INSERT INTO x402_payment_requests "
        "(id,payer_address,amount,amount_usd,asset,chain,recipient,nonce,deadline,status,metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?,'settling',?) ON CONFLICT DO NOTHING",
        (details['id'], details['payer'], details['amount'], amount_usd, 'usdc',
         details['network'], details['recipient'], details['id'], details['valid_before'], metadata))
    if cur.rowcount != 1:
        raise SettlementPending("Payment authorization already submitted; reconcile its result")
    return details['id']
