"""Durable, atomic consumption of signed feedback grants and purchase proofs."""
import hashlib
from pathlib import Path

from core.runtime_paths import sidecar_db_path
from core.surfaces.idempotency import IdempotencyStore


def consume_feedback_auth(chain_id, auth, proof_key=None):
    path = sidecar_db_path("feedback_replays.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = IdempotencyStore(path)
    identity = f"{chain_id}:{auth.agentId}:{auth.clientAddress.lower()}:{auth.nonce}"
    keys = ["auth:" + hashlib.sha256(identity.encode()).hexdigest()]
    if proof_key:
        keys.append("payment:" + proof_key.lower())
    if not store.claim_permanent(keys):
        raise ValueError("feedback authorization or payment proof already used")
