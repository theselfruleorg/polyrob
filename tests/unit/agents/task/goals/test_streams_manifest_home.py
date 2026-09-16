"""034 §12.3: the effective manifest lives in the DATA HOME, not the code tree.

`default_manifest_path()` resolved to `<install>/data/streams/streams.yaml` — inside
`/opt/polyrob`, which `scripts/deploy_prod.sh` rsyncs from `git archive HEAD`. Any
owner edit made there is silently reverted by the next deploy, which makes an
"edit the manifest from chat" verb a lie that takes hours to notice.

The repo copy stays the SHIPPED DEFAULT for a fresh install; it is seeded into the
data home once, and after that the data-home copy is the one that is read and
written.
"""
import os
from pathlib import Path

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_STREAMS_MANIFEST", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    # 036 (2026-09-11): the harness no longer SHIPS a manifest — the tree carries
    # the mechanism and zero work. These tests are about the shipped->home seed
    # MECHANISM, which an operator still uses for an imported manifest, so they
    # supply their own "shipped" copy instead of depending on repo content that is
    # deliberately gone. Without this they would fail, and skipping them would
    # leave a live mechanism untested.
    from agents.task.goals import streams as S
    shipped = tmp_path / "install" / "data" / "streams" / "streams.yaml"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("version: 1\nstreams:\n- id: demo\n  objective:\n"
                       "    title: Demo\n  goals:\n  - title: a\n    body: b\n"
                       "    tools: [task]\n")
    monkeypatch.setattr(S, "shipped_manifest_path", lambda: str(shipped))
    return tmp_path


def test_the_resolver_is_pure_and_never_writes(home):
    """`default_manifest_path()` must not have a side effect.

    The first cut seeded from inside the resolver, and three existing tests that
    call it without isolation immediately wrote into the developer's real data
    home. A path resolver that creates files is a landmine; the seed is an
    explicit call (`ensure_manifest_seeded`) at a startup seam instead.
    """
    from agents.task.goals import streams as S
    before = sorted(p.name for p in home.iterdir())
    S.default_manifest_path()
    S.default_manifest_path()
    assert sorted(p.name for p in home.iterdir()) == before, "the resolver wrote something"


def test_the_resolver_returns_the_shipped_copy_until_the_home_copy_exists(home):
    from agents.task.goals import streams as S
    assert S.default_manifest_path() == S.shipped_manifest_path()


def test_ensure_manifest_seeded_creates_the_home_copy_and_the_resolver_then_uses_it(home):
    from agents.task.goals import streams as S
    seeded = Path(S.ensure_manifest_seeded())
    assert home in seeded.parents, f"the seed must land under the data home, got {seeded}"
    assert seeded.name == "streams.yaml"
    assert seeded.read_text() == Path(S.shipped_manifest_path()).read_text()
    assert S.default_manifest_path() == str(seeded), "the resolver must now prefer it"


def test_an_owner_edit_survives_a_later_seed(home):
    """The seed happens ONCE. Every later call — a reboot, an hourly timer tick —
    must not clobber an owner's edit. That is the whole point: a deploy must not
    revert what the owner changed from chat."""
    from agents.task.goals import streams as S
    path = Path(S.ensure_manifest_seeded())
    # Guard the guard: an earlier cut of this test wrote through a failed fixture
    # and truncated the repo's real 286-line manifest to `streams: []`. A test that
    # writes a file must first prove it is writing the isolated copy.
    assert home in path.parents, f"refusing to write outside the tmp home: {path}"
    assert path != Path(S.shipped_manifest_path())
    path.write_text("version: 1\nstreams: []\n")
    again = Path(S.ensure_manifest_seeded())
    assert again == path
    assert again.read_text() == "version: 1\nstreams: []\n", "a later seed clobbered the owner's edit"


def test_explicit_env_override_still_wins(home, tmp_path):
    from agents.task.goals import streams as S
    custom = tmp_path / "elsewhere" / "mine.yaml"
    custom.parent.mkdir(parents=True)
    custom.write_text("version: 1\nstreams: []\n")
    os.environ["POLYROB_STREAMS_MANIFEST"] = str(custom)
    try:
        assert S.default_manifest_path() == str(custom)
    finally:
        del os.environ["POLYROB_STREAMS_MANIFEST"]


def test_falls_back_to_the_shipped_copy_when_the_home_is_unwritable(home, monkeypatch):
    """Fail-open: an unwritable data home must not break seeding entirely — it
    degrades to today's behaviour (read the shipped copy) rather than raising."""
    from agents.task.goals import streams as S

    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(S.shutil, "copyfile", _boom)
    path = Path(S.ensure_manifest_seeded())
    assert path == Path(S.shipped_manifest_path())


def test_the_data_home_manifest_is_still_denied_to_agent_file_writes(home):
    """The seat change must not weaken the self-grant guarantee: the manifest is
    the ONE place an autonomous goal is granted a money verb, so every
    agent-writable file surface must still refuse it at its new location."""
    from core.security.secret_guard import is_protected_config_path
    from agents.task.goals import streams as S
    # NOTE: the guard is typed `path: Path` and uses `path.parts` — a str argument
    # raises AttributeError mid-function rather than answering. Callers pass Path.
    assert is_protected_config_path(Path(S.ensure_manifest_seeded())) is True


def test_the_hourly_seeder_seeds_the_owner_editable_copy(home, monkeypatch, tmp_path):
    """034 §12.3: something has to create the data-home copy on a fresh install.

    The hourly stream seeder is that seam — after its first tick the owner-editable
    manifest exists and `default_manifest_path()` prefers it, so an owner edit made
    from chat is outside the deploy's reach from then on.
    """
    import importlib.util
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[5]
    seeder = root / "scripts" / "seed_streams.py"
    if not seeder.is_file():
        import pytest as _pt
        _pt.skip("scripts/seed_streams.py is operator tooling, absent from the public tree")

    from agents.task.goals import streams as S
    assert not _P(S.home_manifest_path()).exists()

    spec = importlib.util.spec_from_file_location("_seed_streams_home", seeder)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Args:
        db = str(tmp_path / "goals.db")
        user_id = "rob"
        manifest = None
        stream = None
        dry_run = True
        force = False

    mod._run(_Args())
    assert _P(S.home_manifest_path()).is_file(), \
        "the hourly seeder must create the owner-editable manifest copy"
