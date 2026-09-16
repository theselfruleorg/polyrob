"""Ratchet: no test may write the SHIPPED stream manifest.

`data/streams/streams.yaml` is the ONE place an autonomous goal is granted a money
verb, and it is git-tracked instance data that deploys to prod. A test that resolves
the manifest path through a failed fixture and writes to it truncates the real file —
which happened while building 034 §12.3 (286 lines -> `streams: []`, caught only
because three unrelated tests then failed to parse it).

`default_manifest_path()` is pure and `ensure_manifest_seeded()` writes only the
data-home copy, so this should never trip. It exists because the failure mode is
silent and the blast radius is prod.
"""
from pathlib import Path

import pytest

from agents.task.goals import streams as S

SHIPPED = Path(S.shipped_manifest_path())

pytestmark = pytest.mark.skipif(
    not SHIPPED.is_file(),
    reason="the shipped manifest is operator data, absent from the public tree",
)


@pytest.fixture(autouse=True)
def _shipped_manifest_is_unchanged():
    before = SHIPPED.read_bytes()
    yield
    assert SHIPPED.read_bytes() == before, "a test wrote the SHIPPED stream manifest"


def test_the_resolver_never_returns_the_shipped_path_as_a_write_target(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_STREAMS_MANIFEST", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    seeded = Path(S.ensure_manifest_seeded())
    assert seeded != SHIPPED
    assert tmp_path in seeded.parents


def test_seeding_copies_the_shipped_manifest_without_modifying_it(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_STREAMS_MANIFEST", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    seeded = Path(S.ensure_manifest_seeded())
    assert seeded.read_bytes() == SHIPPED.read_bytes()
