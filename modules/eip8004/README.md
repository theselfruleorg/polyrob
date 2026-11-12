# `modules/eip8004` — ERC-8004 Trustless Agents

This package serves the agent's [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004)
registration file and its validation API at `/eip8004/*`. There is no feedback
API: the agent reads feedback from the on-chain Reputation Registry
(`core/wallet/erc8004_reputation.py`).
For setup, flags and caveats, read the public guide:
[payments guide §6 — ERC-8004](../../docs/guide/payments.md#6-on-chain-identity-and-reputation--erc-8004-moduleseip8004).

## Status

| Part | Status |
|---|---|
| Registration file (`GET /eip8004/registration.json`, the `tokenURI` target) | Real |
| On-chain identity mint (`defi_trade.register_agent` / `set_agent_uri`) | Real, but NOT in this package: it lives in `core/wallet/erc8004.py` (pinned registries) and the DeFi tool, behind `EIP8004_REGISTER_ENABLED` (default OFF) |
| Reputation | Read on-chain, NOT in this package: `core/wallet/erc8004_reputation.py` |
| Validation registry | Local simulation: in-memory, lost on restart |
| `contracts.py` contract wrappers | Reference only; no caller uses them |

`trustMode` in the registration file is `onchain` only after a confirmed
registration (`attestation: "verified"`) or an explicit operator claim
(`attestation: "operator"`); otherwise it is `local`.

## Files

- `models.py` — Pydantic models for the registration file and validation.
- `registration.py` — config from the environment and the registration-file builder.
- `validation.py` — the validation-request simulation.
- `contracts.py` — ABI reference for the Identity and Validation registries.

The HTTP router is `api/eip8004_endpoints.py`.
