"""Bound browser downloads and copy them through the confined file writer."""
import asyncio
import re
import tempfile
import uuid
from pathlib import Path

from core.security import workspace_io

MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024


class DownloadRefused(ValueError):
    """A download started but could not be saved; never retry its click."""


def download_name(suggested: str) -> str:
    leaf = str(suggested or '').replace('\\', '/').rsplit('/', 1)[-1]
    leaf = re.sub(r'[^A-Za-z0-9._-]', '_', leaf).lstrip('.') or 'download'
    stem, suffix = Path(leaf).stem, Path(leaf).suffix
    return f'{stem[:80]}-{uuid.uuid4().hex}{suffix[:16]}'


async def save_download(download, directory) -> str:
    """A server filename is display data, never a path or overwrite instruction."""
    root = Path(directory).absolute()
    target = root / download_name(download.suggested_filename)
    # save_as also supports remote browsers, unlike download.path(). The private
    # staging path is never based on server input or a pre-existing target.
    try:
        with tempfile.TemporaryDirectory(prefix='polyrob-browser-download-') as staging:
            source = Path(staging) / 'payload'
            await download.save_as(str(source))

            def copy():
                data = workspace_io.read_bytes(source, staging, max_bytes=MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ValueError('Browser download exceeds the 32 MiB file limit')
                workspace_io.write_bytes(target, root, data, mode=0o600)

            await asyncio.to_thread(copy)
    except Exception as exc:
        raise DownloadRefused('Download could not be saved within the confined file limit; the click was not retried') from exc
    return str(target)
