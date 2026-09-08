"""reap_unowned: ownership-keyed sandbox sweep.

The safety property under test is asymmetric. Leaking a container is an
annoyance; removing a container out from under a LIVE session destroys that
session's shell state, background jobs and installs. So every ambiguous case
(no label, unparseable timestamp, too young) must resolve to "leave it alone".
"""
import pytest

from tools.code_exec.backends.docker import DockerBackend

_NOW_ISO = "2026-08-24T15:00:00.000000000Z"
_OLD_ISO = "2020-01-01T00:00:00.000000000Z"


def _runner(containers):
    """Fake DockerRunner over {cid: (session_label, started_at)}; records removals."""
    removed = []

    async def run(argv, *a, **kw):
        if argv[0] == "ps":
            return 0, "\n".join(containers), ""
        if argv[0] == "inspect":
            cids = argv[3:]
            return 0, "\n".join(f"{containers[c][0]}\t{containers[c][1]}" for c in cids), ""
        if argv[0] == "rm":
            removed.append(argv[2])
            return 0, "", ""
        return 1, "", "unexpected"

    run.removed = removed
    return run


@pytest.mark.asyncio
async def test_removes_container_whose_session_is_gone():
    run = _runner({"c1": ("dead-session", _OLD_ISO)})
    assert await DockerBackend.reap_unowned(["live-session"], run) == 1
    assert run.removed == ["c1"]


@pytest.mark.asyncio
async def test_keeps_container_of_a_live_but_idle_session():
    """The whole reason this sweep is ownership-keyed and not age-keyed."""
    run = _runner({"c1": ("live-session", _OLD_ISO)})
    assert await DockerBackend.reap_unowned(["live-session"], run) == 0
    assert run.removed == []


@pytest.mark.asyncio
async def test_keeps_unlabeled_container():
    for label in ("", "<no value>"):
        run = _runner({"c1": (label, _OLD_ISO)})
        assert await DockerBackend.reap_unowned(["live"], run) == 0
        assert run.removed == []


@pytest.mark.asyncio
async def test_keeps_young_container_creation_race_guard():
    """A container labeled but not yet registered must survive the sweep."""
    import time
    from datetime import datetime, timezone
    fresh = datetime.fromtimestamp(time.time(), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z")
    run = _runner({"c1": ("not-yet-registered", fresh)})
    assert await DockerBackend.reap_unowned([], run) == 0
    assert run.removed == []


@pytest.mark.asyncio
async def test_keeps_container_with_unparseable_start_time():
    run = _runner({"c1": ("dead-session", "not-a-timestamp")})
    assert await DockerBackend.reap_unowned(["live"], run) == 0
    assert run.removed == []


@pytest.mark.asyncio
async def test_sweeps_only_the_dead_ones_in_a_mixed_set():
    run = _runner({
        "live1": ("s-live", _OLD_ISO),
        "dead1": ("s-dead", _OLD_ISO),
        "dead2": ("s-also-dead", _OLD_ISO),
    })
    assert await DockerBackend.reap_unowned(["s-live"], run) == 2
    assert sorted(run.removed) == ["dead1", "dead2"]


@pytest.mark.asyncio
async def test_docker_ps_failure_is_fail_open():
    async def run(argv, *a, **kw):
        raise RuntimeError("docker daemon down")
    assert await DockerBackend.reap_unowned(["live"], run) == 0
