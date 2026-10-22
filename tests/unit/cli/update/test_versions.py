"""T1.2 — version parsing, comparison, and current-vs-latest resolution."""
import json
import logging

import pytest

from cli.update.versions import (
    UpdateStatus, compare, is_prerelease, parse_semver, resolve_status,
    select_latest,
)


def test_parse_semver():
    assert parse_semver("0.4.2") == (0, 4, 2)
    assert parse_semver("v1.2.3") == (1, 2, 3)
    assert parse_semver("1.2.3-rc1") == (1, 2, 3)
    assert parse_semver("garbage") is None


def test_compare():
    assert compare("0.4.2", "0.5.0") == -1
    assert compare("0.5.0", "0.4.2") == 1
    assert compare("1.0.0", "1.0.0") == 0


def test_prerelease_detection_and_filtering():
    assert is_prerelease("0.5.0-rc1")
    assert not is_prerelease("0.5.0")
    versions = ["0.4.2", "0.5.0-rc1", "0.5.0", "0.6.0-beta"]
    assert select_latest(versions) == "0.5.0"
    assert select_latest(versions, include_prerelease=True) == "0.6.0-beta"


def test_update_status_flags():
    up = UpdateStatus(current="0.4.2", latest="0.5.0", channel="stable")
    assert up.update_available and not up.is_downgrade
    same = UpdateStatus(current="0.4.2", latest="0.4.2", channel="stable")
    assert not same.update_available
    down = UpdateStatus(current="0.5.0", latest="0.4.2", channel="stable")
    assert down.is_downgrade and not down.update_available


def test_resolve_status_pypi():
    body = json.dumps({"releases": {"0.4.2": [], "0.5.0": [], "0.5.1-rc1": []}})
    st = resolve_status(channel="stable", fetch=lambda url: body,
                        source="pypi", current="0.4.2")
    assert st.latest == "0.5.0" and st.update_available


def test_resolve_status_failsoft_on_network_error():
    def boom(url):
        raise OSError("network down")

    st = resolve_status(channel="stable", fetch=boom, current="0.4.2")
    assert st.latest is None and not st.update_available


def test_resolve_status_github_source():
    body = json.dumps([{"tag_name": "v0.5.0"}, {"tag_name": "v0.4.2"}])
    st = resolve_status(channel="stable", fetch=lambda url: body,
                        source="github", current="0.4.2")
    assert st.latest == "v0.5.0"


# --- §2.2 / §2.8: informative failure classification --------------------------

class _FakeHTTPError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def test_resolve_status_offline_is_classified():
    def boom(url):
        raise OSError("dns failure")

    st = resolve_status(channel="stable", fetch=boom, current="0.4.2", source="github")
    assert st.latest is None and st.error == "offline"
    assert "could not" in st.human_note.lower()


def test_resolve_status_not_found_is_classified():
    def boom(url):
        raise _FakeHTTPError(404)

    st = resolve_status(channel="stable", fetch=boom, current="0.4.2",
                        source="github", repo="acme/widget")
    assert st.latest is None and st.error == "not_found"
    assert "acme/widget" in st.human_note


def test_resolve_status_no_releases_is_classified():
    st = resolve_status(channel="stable", fetch=lambda url: "[]",
                        source="github", current="0.4.2", repo="acme/widget",
                        sleep=lambda s: None)
    assert st.latest is None and st.error == "no_releases"
    assert "acme/widget" in st.human_note


def test_resolve_status_retries_a_transient_empty_github_list():
    # GitHub's releases API intermittently answers 200 with a bare `[]` for a repo
    # that has releases; one such reply must not abort an update.
    replies = iter(["[]", "[]", json.dumps([{"tag_name": "v0.5.0"}])])
    waits = []
    st = resolve_status(channel="stable", fetch=lambda url: next(replies),
                        source="github", current="0.4.2", repo="acme/widget",
                        sleep=waits.append)
    assert st.latest == "v0.5.0" and st.error is None
    assert len(waits) == 2


def test_resolve_status_reports_and_logs_each_empty_github_list(caplog):
    with caplog.at_level(logging.DEBUG, logger="cli.update.versions"):
        st = resolve_status(channel="stable", fetch=lambda url: "[]",
                            source="github", current="0.4.2", repo="acme/widget",
                            sleep=lambda s: None)
    assert st.error == "no_releases" and st.empty_replies == 3
    # The user-facing note says what GitHub sent (the CLI and the boot log print it);
    # the per-reply lines are DEBUG so `--check --json` output stays pure JSON.
    assert "came back empty 3 time(s)" in st.human_note
    empties = [r for r in caplog.records if "empty release list" in r.getMessage()]
    assert len(empties) == 3 and all(r.levelno == logging.DEBUG for r in empties)
    assert "acme/widget" in empties[0].getMessage()


def test_filtered_out_releases_are_not_retried():
    # Releases exist but none qualify (all pre-releases): a real answer, not a glitch.
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps([{"tag_name": "v0.5.0-rc1", "prerelease": True}])

    st = resolve_status(channel="stable", fetch=fetch, source="github",
                        current="0.4.2", repo="acme/widget", sleep=lambda s: None)
    assert st.error == "no_releases" and len(calls) == 1
    assert st.empty_replies == 0 and "came back empty" not in st.human_note


def test_resolve_repo_env_override(monkeypatch):
    from cli.update.versions import resolve_repo

    monkeypatch.setenv("POLYROB_UPDATE_REPO", "myorg/myfork")
    assert resolve_repo() == "myorg/myfork"


def test_resolve_pypi_package_env_override(monkeypatch):
    from cli.update.versions import resolve_pypi_package

    monkeypatch.setenv("POLYROB_UPDATE_PYPI", "polyrob-nightly")
    assert resolve_pypi_package() == "polyrob-nightly"


def test_status_dict_carries_error_and_ref():
    st = resolve_status(channel="stable", fetch=lambda url: "[]",
                        source="github", current="0.4.2", repo="acme/widget",
                        sleep=lambda s: None)
    d = st.as_dict()
    assert d["error"] == "no_releases"
    assert d["source_ref"] == "acme/widget"
