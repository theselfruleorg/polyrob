# `modules/eip8004` — ERC-8004 Trustless Agents

This package serves the agent's [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004)
registration file and its feedback/validation API at `/eip8004/*`.
For setup, flags and caveats, read the public guide:
[payments guide §8 — ERC-8004](../../docs/guide/payments.md#8-on-chain-identity-and-payment-backed-reputation--erc-8004-moduleseip8004).

## Status

| Part | Status |
|---|---|
| Registration file (`GET /eip8004/registration.json`, the `tokenURI` target) | Real |
| On-chain identity mint (`defi_trade.register_agent` / `set_agent_uri`) | Real, but NOT in this package: it lives in `core/wallet/erc8004.py` (pinned registries) and the DeFi tool, behind `EIP8004_REGISTER_ENABLED` (default OFF) |
| Feedback authorization (EIP-712 grant) | Real signature; grants and payment proofs are consumed once, durably |
| Reputation store | Local simulation: in-memory, lost on restart, never written on-chain |
| Validation registry | Local simulation: in-memory, lost on restart |
| `contracts.py` contract wrappers | Reference only; no caller uses them |

`trustMode` in the registration file is `onchain` only after a confirmed
registration (`attestation: "verified"`) or an explicit operator claim
(`attestation: "operator"`); otherwise it is `local`.

## Files

- `models.py` — Pydantic models for the registration file, feedback and validation.
- `registration.py` — config from the environment and the registration-file builder.
- `reputation.py` — feedback grants, `submit_feedback`, reputation queries.
- `feedback_replays.py` — the durable one-use claim for grants and payment proofs.
- `payment_proof.py` — a settled x402 invoice as a proof of payment.
- `validation.py` — the validation-request simulation.
- `contracts.py` — ABI reference for the three registries.

The HTTP router is `api/eip8004_endpoints.py`.
