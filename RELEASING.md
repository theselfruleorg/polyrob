# Releasing POLYROB

POLYROB releases are cut from a reviewed commit on `main`. A version tag starts
the GitHub Actions release workflow, which validates the tag, builds the wheel
and source archive, smoke-tests the wheel, publishes to PyPI when the repository
publisher is configured, and attaches the artifacts to a GitHub Release.

## Prepare the release PR

Choose a Semantic Versioning number and update these files together:

- `pyproject.toml`: project version (the build source of truth)
- `core/version.py`: source-tree fallback
- `tests/test_version_consistency.py`: literal release pin and test name
- `CHANGELOG.md`: a dated `## [X.Y.Z] — YYYY-MM-DD` section with a fresh,
  empty `## [Unreleased]` section above it
- `cli/ui/banner.py`: example versions in docstrings

Do not change the database schema version merely to match the application
version. Schema migrations have their own version sequence.

Run the release-alignment checks from the repository root:

```bash
pytest tests/test_version_consistency.py tests/test_changelog_release.py tests/unit/core/test_flags.py
python scripts/gen_flags_catalog.py --check  # when the operator tooling is available
pytest tests/unit/test_no_stray_rob_env.py \
  tests/unit/agents/task/test_skill_rules_integrity.py \
  tests/unit/agents/task/test_toolsets.py
pytest tests/install/
pytest -q -p no:randomly
```

The release PR must pass every required GitHub check. Do not tag a commit whose
release PR is still open or whose checks are red.

## Build and smoke-test locally

Build from a clean checkout of the exact release commit:

```bash
python -m build
python -m twine check dist/*

python -m venv /tmp/polyrob-release-venv
/tmp/polyrob-release-venv/bin/pip install dist/polyrob-X.Y.Z-py3-none-any.whl
cd "$(mktemp -d)"
/tmp/polyrob-release-venv/bin/polyrob version
/tmp/polyrob-release-venv/bin/polyrob --help
/tmp/polyrob-release-venv/bin/polyrob doctor
(cd /tmp && POLYROB_DATA_DIR=/tmp/polyrob-release-data \
  /tmp/polyrob-release-venv/bin/python -m migrations.migrate status)
```

`polyrob doctor` must leave the empty temporary directory unchanged. Repeat the
smoke test with relevant optional extras when the release changes those extras.
On a fresh data directory, migration status may exit 1 after successfully
reporting that the database needs its baseline; any crash or other exit code is
a release failure.

## First-party packs (inside the wheel)

The first-party packs (`packs/discovery`, `packs/markets`, `packs/x`) ship INSIDE
the `polyrob` wheel (067, one install): there is ONE distribution to build, check
and upload. Their SDKs are core extras (`twitter`, `anysite`, `crypto`; each
`pack.toml` names its own). In the release PR:

- set each `packs/*/*/pack.toml` `version = "X.Y.Z"` and its `requires_core` to
  `==X.Y.*`;
- regenerate the pack index (its first-party rows carry `dist = "polyrob"` and the
  core version) and check it:

```bash
python scripts/gen_pack_index.py
python scripts/gen_pack_index.py --check
pytest tests/unit/core/packs tests/unit/core/test_lock_closure_packs.py
```

The wheel smoke test above covers the packs. After the bare install,
`polyrob pack list` must show the three packs `loaded`, each naming its extra
(`needs pip install 'polyrob[twitter]'` …); after `pip install '<wheel>[twitter,anysite,crypto]'`
they must show `loaded` with nothing needed.

⚠️ Never build or upload a separate pack distribution: the retired separate
names are in `core.packs.index.RETIRED_DISTS` — the loader refuses them, and
`polyrob pack install` refuses a third-party source that takes one.

## Tag and publish

After the release PR is merged, update local `main`, verify the commit SHA, and
push one annotated version tag:

```bash
git switch main
git pull --ff-only
git tag -a vX.Y.Z -m "POLYROB vX.Y.Z"
git push origin vX.Y.Z
```

Watch the `Release` workflow to completion. It rejects a tag that does not match
`pyproject.toml`. When publishing is configured, the workflow uploads the
artifacts to PyPI and creates or updates the GitHub Release.

Check PyPI before attempting any fallback upload. PyPI filenames are immutable,
so retrying an artifact that CI already published returns an error and cannot
replace the existing file. Use a manual upload only when the workflow failed
before publishing and the version is absent from PyPI.

Verify the published artifacts in fresh environments:

```bash
python -m venv /tmp/polyrob-pypi-base
/tmp/polyrob-pypi-base/bin/pip install "polyrob==X.Y.Z"
/tmp/polyrob-pypi-base/bin/python -c \
  "import cron, surfaces, core, tools.defi, core.llm_auth; from core.version import get_version; print(get_version())"
/tmp/polyrob-pypi-base/bin/polyrob version

python -m venv /tmp/polyrob-pypi-crypto
/tmp/polyrob-pypi-crypto/bin/pip install "polyrob[crypto]==X.Y.Z"
/tmp/polyrob-pypi-crypto/bin/python -c \
  "import core.wallet.tx_guard, modules.payments.networks; print('crypto extra ok')"
```

## Changelog and release notes

POLYROB keeps two records of every release. They have different readers and
different rules.

**`CHANGELOG.md` — the full technical record.** Every change that a user,
operator or integrator can observe, with the detail they need: commands, flags,
endpoints, file names and migration steps are welcome here.

- Group each release under the Keep a Changelog headings only: `### Added`,
  `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`.
  No dates, topics or work-item names in a heading.
- One change per bullet: what changed, and what it means for the reader.
- Keep it short: a bullet is at most 3 lines (about 40 words). The detail belongs
  in the docs, not here. `tests/test_changelog_release.py` caps long bullets.
- One piece of work is ONE bullet per heading, however many commits it took. Do
  not list its steps, its internal modules, or the bugs you found and fixed in
  it before it shipped — those are part of the feature, not `### Fixed`.
- Add a new bullet at the TOP of its heading, after a blank line, and read the
  section back: never insert inside another session's bullet.
- Describe the shipped behavior, not the work: no proposal, phase, workstream or
  wave names, no review or audit narration, no "not started yet", no production
  incident stories, and no links to documents that do not ship.
- During development, entries accumulate under `## [Unreleased]`. At the release
  cut, merge them into the six headings above and remove anything that is not a
  shipped change. `tests/test_changelog_release.py` enforces the headings and the
  work-item ban on every release after 1.1.0.

**Release notes — the short version.** The GitHub Release page and the website's
What's new page carry the same short notes, written for a person deciding whether
to update:

- a theme of at most 8 words and a one-sentence summary of at most 25 words;
- 3 to 5 highlights, each a label of at most 5 words and one sentence of at most
  20 words that says what the user can now do;
- no code, file paths, environment flags, API routes or internal names — that
  detail lives in `CHANGELOG.md`, and the notes end with a link to it.

Never paste the `CHANGELOG.md` section into the GitHub Release.

## Public-release boundary

Only reviewed product source, tests, user documentation, templates, and community
files belong in a public release. Exclude credentials, real environment files,
runtime data, operator identity, deployment-specific configuration, unpublished
plans and reviews, and infrastructure details. Run secret scanning over the exact
tree that will be tagged, and treat any finding as a release blocker.

Release notes describe shipped user behavior. They must not link to unpublished
documents or narrate internal development and operations.
