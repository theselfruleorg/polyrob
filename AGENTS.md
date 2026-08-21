# AGENTS.md

Guidance for AI coding agents (and humans) working in the POLYROB repository.
The repo-root `CLAUDE.md` imports this file so Claude Code loads it automatically.

## Orientation

- **Architecture overview:** `docs/guide/architecture.md` (framework vs instance,
  DI container, task agent step loop, tools, memory, surfaces, autonomy loops).
- **Configuration SSOT:** `docs/CONFIGURATION.md` — every env flag with default,
  meaning, and code anchor. Trust the code anchor over prose.
- **User guide:** `docs/guide/` (getting-started, CLI, API, skills, self-hosting,
  payments). **Comparison:** `docs/comparison.md`. **Examples:** `docs/examples.md`.

## Layout

| Tier | Path | Role |
|---|---|---|
| Core | `core/` | DI container, config/policy, lifecycle, instance identity, wallet |
| Modules | `modules/` | LLM providers, memory backends, credits/x402 |
| Agents | `agents/` | The Task agent (orchestrator, step loop, message manager) |
| Tools | `tools/` | Browser, MCP, email, code-exec, coding, crypto, controller |
| Surfaces | `surfaces/`, `api/`, `cli/`, `webview/` | Telegram/email, REST, terminal, console |
| Autonomy | `cron/`, `agents/task/goals/` | Scheduled runs, goal board, dispatchers |

## Invariants (enforced by tests — do not fight them)

- **Layering ratchet** (`tests/test_layering_ratchet.py`, `tests/test_import_layering.py`):
  `core/` never imports `agents.*`; the agents tier never imports `tools/`.
  Ratchet allowlists only shrink.
- **Flags catalog:** adding or renaming an env flag requires a row in
  `docs/CONFIGURATION.md` AND `python scripts/gen_flags_catalog.py` (the contract
  test in `tests/unit/core/test_flags.py` fails otherwise).
- **Tool capabilities:** a new tool id must be classified in
  `core/tool_capabilities.py`; `register_optional_tool` refuses unclassified tools.
- **No `from __future__ import annotations`** in action-registration modules —
  the registry introspects live first-param annotations.
- **Version SSOT:** `pyproject.toml` + `core/version.py` move together
  (`tests/test_version_consistency.py`).

## Working here

- Tests: `pytest -q -p no:randomly` from the repo root is the blessed invocation.
- TDD: write the failing test first; keep changes small and focused.
- Style: PEP 8, type hints, composition over inheritance, async for I/O.
- Never commit secrets; env templates live in `config/` as `*.template`.
