from pathlib import Path

import pytest

from tools.browser import downloads


class Download:
    def __init__(self, name, data=b'downloaded'):
        self.suggested_filename = name
        self.data = data
        self.staged = None

    async def save_as(self, path):
        self.staged = Path(path)
        self.staged.write_bytes(self.data)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['../../outside', '/tmp/outside', r'C:\outside\file.txt',
                                 '.env', '..', 'x\x00\n/evil', 'x' * 5000])
async def test_hostile_download_names_stay_in_directory(tmp_path, name):
    root = tmp_path / 'downloads'
    root.mkdir()
    download = Download(name)
    target = Path(await downloads.save_download(download, root))
    assert target.parent == root
    assert not target.name.startswith('.')
    assert target.read_bytes() == download.data
    assert target.stat().st_mode & 0o777 == 0o600
    assert not download.staged.exists()


@pytest.mark.asyncio
async def test_download_replaces_link_entry_without_following_it(monkeypatch, tmp_path):
    root = tmp_path / 'downloads'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'original')
    (root / 'fixed.txt').symlink_to(outside)
    monkeypatch.setattr(downloads, 'download_name', lambda name: 'fixed.txt')
    await downloads.save_download(Download('fixed.txt'), root)
    assert outside.read_bytes() == b'original'
    assert not (root / 'fixed.txt').is_symlink()


@pytest.mark.asyncio
async def test_oversize_download_never_lands(monkeypatch, tmp_path):
    monkeypatch.setattr(downloads, 'MAX_DOWNLOAD_BYTES', 4)
    download = Download('x', b'12345')
    with pytest.raises(ValueError, match='file limit'):
        await downloads.save_download(download, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert not download.staged.exists()


@pytest.mark.asyncio
async def test_failed_download_does_not_click_the_page_again(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools.browser.context import BrowserContext
    monkeypatch.setattr(downloads, 'MAX_DOWNLOAD_BYTES', 1)

    class DownloadEvent:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        async def value(self):
            return Download('report.txt', b'too large')

    page = SimpleNamespace(expect_download=lambda **kwargs: DownloadEvent(), evaluate=AsyncMock())
    element = SimpleNamespace(click=AsyncMock())
    context = BrowserContext.__new__(BrowserContext)
    context.session = None
    context.config = SimpleNamespace(save_downloads_path=str(tmp_path))
    context.get_current_page = AsyncMock(return_value=page)
    context.get_locate_element = AsyncMock(return_value=element)
    with pytest.raises(downloads.DownloadRefused):
        await context._click_element_node(SimpleNamespace(highlight_index=None))
    element.click.assert_awaited_once()
    page.evaluate.assert_not_awaited()
