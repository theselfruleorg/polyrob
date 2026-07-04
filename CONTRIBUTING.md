# Contributing to POLYROB

Thank you for your interest in contributing to POLYROB! This document covers how to get started,
run tests, and submit changes.

---

## Table of contents

1. [Development setup](#development-setup)
2. [Running tests](#running-tests)
3. [Linting](#linting)
4. [Extras layout](#extras-layout)
5. [Contribution flow](#contribution-flow)
6. [Documentation](#documentation)

---

## Development setup

```bash
git clone https://github.com/theselfruleorg/polyrob
cd polyrob
python -m venv venv
source venv/bin/activate
pip install -e ".[dev,all]"
python -m playwright install chromium
```

The `all` extra pulls in every optional dependency group. If you only need a subset,
see the [Extras layout](#extras-layout) section.

---

## Running tests

Run the full suite (this is what CI runs — a bare `pytest`, which also collects the
co-located `test_*.py` files that live next to the code outside `tests/`):

```bash
pytest -q
```

For faster local iteration, run just the unit tests:

```bash
pytest tests/unit -q
```

Tests that touch browser automation (e.g. `tests/unit/test_browser_manager.py`,
`tests/unit/tools/browser/`) mock Playwright and don't need a live Chromium binary. The
`python -m playwright install chromium` step above is only needed to actually run the agent's
browser tool, not to run the test suite.

---

## Linting

```bash
ruff check .
```

We use [Ruff](https://docs.astral.sh/ruff/) for fast Python linting. Fix auto-fixable issues
with `ruff check . --fix`.

---

## Extras layout

`pyproject.toml` declares the following optional dependency groups:

| Extra | What it pulls in |
|---|---|
| `server` | FastAPI, Uvicorn, WebSocket support |
| `browser` | Playwright (headless Chromium automation) |
| `memory-vector` | Semantic vector recall (sentence-transformers + sqlite-vec) |
| `crypto` | web3, eth-account, x402 payment support |
| `telegram` | aiogram Telegram surface |
| `twitter` | Twitter/X API client |
| `voice` | Voice transcription (faster-whisper) |
| `dev` | pytest, ruff, build tooling |
| `all` | Everything above |

Install only what you need, e.g. `pip install -e ".[server,browser]"`.

---

## Contribution flow

POLYROB is developed **in the open** — this public repo is the dev home for both maintainers
and external contributors, and everyone follows the same loop. By participating, you agree to
follow our [Code of Conduct](CODE_OF_CONDUCT.md).

1. **Fork** the repository on GitHub (external contributors), or create a **branch** directly
   (maintainers). Either way, do your work in a dedicated **git worktree** (or a fresh clone) so
   it's isolated from other work in progress:
   ```bash
   git worktree add ../polyrob-my-change -b feat/my-change
   cd ../polyrob-my-change
   ```
2. **Write a failing test first, then make it pass (TDD).** Every non-trivial change should be
   accompanied by unit tests in `tests/unit/`.
3. **Check tests and lint** before opening a PR:
   ```bash
   pytest -q
   ruff check .
   ```
4. **Open a Pull Request** against `main` on the upstream repo.
   - Keep PRs focused: one logical change per PR.
   - Describe *what* you changed and *why* in the PR description (the PR template will prompt you).
   - Reference any related issues with `Closes #<number>`.
5. **CI must be green.** The `ci` workflow runs a `pytest` job and a `gitleaks` secret-scan job
   on every PR; both must pass before merge.
6. **Review.** A maintainer reviews the PR and may ask for changes.
7. **Squash-merge.** Once approved and green, the PR is squash-merged into `main`.

**`main` is branch-protected and never receives direct pushes** — every change, including from
maintainers, lands via a reviewed, green PR. No sign-off/DCO is required, but by submitting a
PR you agree your contribution is licensed under the project's [MIT License](LICENSE).

We review PRs on a best-effort basis. Small, well-tested PRs are much faster to review than
large sweeping ones.

---

## Documentation

- **Architecture overview:** `docs/guide/architecture.md`
- **Release process:** `RELEASING.md`

For a deeper orientation to the codebase start with the architecture guide, then look at the
relevant module under `agents/`, `modules/`, or `tools/`.
