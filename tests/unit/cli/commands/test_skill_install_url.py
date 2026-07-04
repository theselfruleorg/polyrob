"""Task 21: direct `SKILL.md` URL resolver tests.

Covers: fetch (size-cap/timeout/content-type gated via a monkeypatchable
`_fetch_text`) → staged into a temp folder named by frontmatter `name` →
handed to Task 19's `install_local` with `source="url:<url>"` so a URL
install NEVER auto-approves.

No network access — `_fetch_text` is monkeypatched in every test.
"""
import tempfile

import pytest

from cli.commands import skill_install


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    """Task 23 gates every install route on ``local_mode_enabled()`` — pin it ON
    for this pipeline suite (see test_skill_install_local.py for rationale)."""
    from agents.task import constants

    monkeypatch.setattr(constants, "local_mode_enabled", lambda: True)


def test_install_url_wraps_single_skill_md(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(
        skill_install,
        "_fetch_text",
        lambda url, **k: "---\nname: fetched\ndescription: A fetched skill. Use it.\n---\n# b\nx",
    )
    res = skill_install.install_url("https://example.com/fetched/SKILL.md", user_id="7", trust="prompt")
    assert res.name == "fetched" and res.approved is False and res.source.startswith("url:")


def test_install_url_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(
        skill_install,
        "_fetch_text",
        lambda url, **k: (_ for _ in ()).throw(skill_install.InstallError("too large")),
    )
    with pytest.raises(skill_install.InstallError):
        skill_install.install_url("https://example.com/x/SKILL.md", user_id="7")


# --- Finding 1: path traversal via untrusted frontmatter `name` ------------

def test_install_url_rejects_traversal_name(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    # Pin the system tempdir so we know exactly where `tempfile.TemporaryDirectory`
    # (created one level below it, e.g. `<systmp>/polyrob-url-XXXXXX`) will land —
    # so we can compute where a `../../evil` traversal would actually escape TO,
    # and prove it was never created (not just that *some* error was eventually
    # raised downstream, which would pass even with the vulnerable code since the
    # id-validation regex also happens to reject slashes, AFTER the write already
    # happened).
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    monkeypatch.setattr(
        skill_install,
        "_fetch_text",
        lambda url, **k: (
            "---\nname: ../../evil\ndescription: A malicious skill. Use it.\n---\n# b\nx"
        ),
    )
    # The unsafe name must be rejected BEFORE any path join/mkdir/write is attempted
    # (not merely fail later with an OSError/regex-mismatch from escaping the dir).
    with pytest.raises(skill_install.InstallError):
        skill_install.install_url("https://example.com/x/SKILL.md", user_id="7", trust="prompt")
    # `<systmp>/polyrob-url-XXXXXX/../../evil` == `<systmp's parent>/evil` == `tmp_path/evil`.
    assert not (tmp_path / "evil").exists()


def test_install_url_rejects_absolute_name(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(
        skill_install,
        "_fetch_text",
        lambda url, **k: "---\nname: /etc/evil\ndescription: A malicious skill. Use it.\n---\n# b\nx",
    )
    with pytest.raises(skill_install.InstallError):
        skill_install.install_url("https://example.com/x/SKILL.md", user_id="7", trust="prompt")


# --- Finding 2: content-type gate must actually reject non-text types -----

class _FakeHeaders:
    def __init__(self, ctype):
        self._ctype = ctype

    def get(self, key, default=None):
        if key == "Content-Type":
            return self._ctype
        return default


class _FakeResponse:
    def __init__(self, ctype, body: bytes):
        self.headers = _FakeHeaders(ctype)
        self._body = body

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_text_rejects_non_text_content_type(monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResponse("image/png", b"\x89PNG\r\n"),
    )
    with pytest.raises(skill_install.InstallError):
        skill_install._fetch_text("https://example.com/x/SKILL.md")


def test_fetch_text_allows_text_markdown_content_type(monkeypatch):
    import urllib.request

    body = b"---\nname: ok\ndescription: An ok skill. Use it.\n---\n# b\nx"
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=30: _FakeResponse("text/markdown", body),
    )
    text = skill_install._fetch_text("https://example.com/x/SKILL.md")
    assert text == body.decode("utf-8")


# --- Finding 3: only http(s) schemes are allowed ---------------------------

def test_fetch_text_rejects_file_scheme():
    with pytest.raises(skill_install.InstallError):
        skill_install._fetch_text("file:///etc/passwd")
