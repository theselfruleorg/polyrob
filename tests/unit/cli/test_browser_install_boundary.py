from pathlib import Path

import click
import pytest

from cli.commands import browser as b


def test_browser_update_never_installs_in_previous_tree(tmp_path, monkeypatch):
    root = tmp_path / 'browser'
    root.mkdir()
    (root / 'old').write_text('untrusted')
    monkeypatch.setattr(b, '_check_install_parent', lambda p: None)
    def download(staging, with_deps):
        assert staging != root and not (staging / 'old').exists()
        assert (root / 'old').read_text() == 'untrusted'
        (staging / 'chrome').write_text('new')
        return '999'
    monkeypatch.setattr(b, '_download_chromium', download)
    assert b._install_chromium(root, 'polyrob-browser', False) == '999'
    assert (root / 'chrome').read_text() == 'new'
    assert not (root / 'old').exists()


def test_failed_browser_download_preserves_previous_tree(tmp_path, monkeypatch):
    root = tmp_path / 'browser'
    root.mkdir()
    (root / 'old').write_text('old')
    monkeypatch.setattr(b, '_check_install_parent', lambda p: None)
    def failed(*args):
        raise click.ClickException('download failed')
    monkeypatch.setattr(b, '_download_chromium', failed)
    with pytest.raises(click.ClickException):
        b._install_chromium(root, 'polyrob-browser', False)
    assert (root / 'old').read_text() == 'old'


def test_browser_refuses_unprotected_parent(tmp_path):
    tmp_path.chmod(0o777)
    with pytest.raises(click.ClickException, match='protected root-owned'):
        b._check_install_parent(tmp_path / 'browser')


def test_chromium_download_uses_isolation_and_keeps_installer_ownership(tmp_path, monkeypatch):
    from types import SimpleNamespace
    def run(cmd, **kwargs):
        assert '-I' in cmd and 'playwright' in cmd
        assert Path(kwargs['env']['PLAYWRIGHT_BROWSERS_PATH']) == tmp_path
        binary = tmp_path / 'chromium-999' / 'chrome-linux64' / 'chrome'
        binary.parent.mkdir(parents=True)
        binary.write_text('binary')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(b.subprocess, 'run', run)
    monkeypatch.setattr(b, '_run', lambda *a: pytest.fail('ownership must not transfer to browser'))
    assert b._download_chromium(tmp_path, False) == '999'
