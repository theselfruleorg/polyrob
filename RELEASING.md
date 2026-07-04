# Releasing POLYROB

This runbook walks a maintainer through building, validating, and publishing a new release.

---

## Prerequisites

```bash
pip install build twine
```

Ensure you have:
- PyPI credentials (or a `~/.pypirc` configured for TestPyPI and PyPI).
- A clean working tree on `main` with all changes committed and tests green.

---

## Build & validate

```bash
# 1. Build source + wheel distributions
python -m build

# 2. Check metadata and long-description rendering
twine check dist/*

# 3. Smoke-test the wheel in a fresh venv
python -m venv /tmp/v && /tmp/v/bin/pip install "dist/"*.whl[all]
/tmp/v/bin/python -c "import cron, surfaces, core"
```

If any step fails, fix the issue before continuing.

---

## Publish

```bash
# 4. Upload to TestPyPI first and verify the package page
twine upload -r testpypi dist/*

# 5. Install from TestPyPI and run a quick sanity check
pip install -i https://test.pypi.org/simple/ polyrob

# 6. Upload to the real PyPI
twine upload dist/*
```

---

## Tag & push

```bash
# 7. Create an annotated tag (replace X.Y.Z with the version)
git tag -a vX.Y.Z -m "POLYROB vX.Y.Z"
git push --tags
```

Then create a GitHub Release from the tag and paste the relevant `CHANGELOG.md`
section as the release notes.

---

## NEVER-PUBLISH LIST

The following must **never** appear in the public repository or a published package:

| Path / pattern | Reason |
|---|---|
| `.superpowers/` | Internal subagent-driven-development working artifacts (gitignored) |
| `config/.env*` (non-template) | Secrets / API keys — only commit `*.env.example` templates |
| Any file containing real API keys, wallet keys, SSH keys, or server IPs | Credentials |

> **`AGENTS.md` and its `CLAUDE.md` pointer DO ship** — `AGENTS.md` is the canonical
> deep architecture + contributor guide for AI agents and humans alike (vendor-neutral;
> all per-layer READMEs reference it). It contains no secrets (infra is placeholdered).
>
> **`deployment/` scripts do NOT ship** — they are instance-specific (real domains,
> admin email, server layout) and sit on the publish denylist
> (`scripts/private_paths.txt`) along with `deploy_unified.sh` and `DEPLOYMENT.md`.
> The public self-hosting story lives in `docs/guide/self-hosting.md`.

> Internal planning, review, and live-test docs (formerly `docs/plans/`,
> `docs/reviews/`, `docs/superpowers/`, `docs/archive/`, `docs/livetest/`) are no
> longer tracked — the `docs/` gitignore block ships only the public guide and
> reference docs. Only `docs/guide/`, `docs/CONFIGURATION.md`,
> `docs/SKILL_AUTHORING_STANDARD.md`, `docs/comparison.md`, and `docs/examples.md`
> are published.

The public repository is published as a **fresh squashed history** to ensure no
internal history leaks into the open-source release.

The boundary is enforced mechanically, not by memory:

- `scripts/public_manifest.txt` — allowlist of paths that ship (everything else is
  private by default).
- `scripts/private_paths.txt` — denylist patterns; matching paths are stripped even
  when a manifest directory contains them.
- `scripts/scrub_gate.sh <tree>` — fail-closed gate: denylist check, no real `.env`,
  `gitleaks` (with `.gitleaks.toml` allowlist) = 0, infra-marker grep = 0.
- `scripts/publish_snapshot.sh` — the one-time seed pipeline: stage manifest → strip
  denylist → scrub gate → fresh repo → single squashed commit + version tag. The
  final `git push` to the public remote is a deliberate manual step.
- `scripts/publish_pr.sh <exp-branch> <slug>` — clean-room export of later private
  work into a public PR (cut from `public/main`, contents only, scrub-gated).
