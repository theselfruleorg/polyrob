"""T4 — packaged webgate asset resolver.

``core.assets.webgate_asset_dir()`` resolves the directory that holds the
webgate's ``static/`` and ``templates/`` subdirectories. It prefers a packaged
``web_dist/`` bundle (shipped in the wheel, produced by doc 03) when present, and
falls back to the repo ``webview/`` checkout so a dev tree is byte-identical to
before this seam existed.
"""

from pathlib import Path

import core.assets as assets


def test_resolver_prefers_packaged_then_repo(monkeypatch, tmp_path):
    # No packaged bundle present (today's repo) → repo webview/ fallback.
    monkeypatch.setattr(assets, "_packaged_web_dist", lambda: None)
    repo_dir = assets.webgate_asset_dir()
    assert repo_dir.name == "webview"
    assert (repo_dir / "static").is_dir()
    assert (repo_dir / "templates").is_dir()

    # A built bundle present → prefer it.
    fake_bundle = tmp_path / "web_dist"
    fake_bundle.mkdir()
    monkeypatch.setattr(assets, "_packaged_web_dist", lambda: fake_bundle)
    assert assets.webgate_asset_dir() == fake_bundle


def test_repo_fallback_matches_webview_dir():
    """The dev fallback is the repo `webview/` dir (byte-identical to before)."""
    repo_dir = assets._REPO_WEBVIEW_DIR
    expected = Path(__file__).resolve().parents[3] / "webview"
    assert repo_dir == expected
