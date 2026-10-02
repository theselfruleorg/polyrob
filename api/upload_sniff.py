"""The upload MIME sniffer (058 T1.6) — extracted because ``api/task_http_api.py``
sits at its size ceiling.

python-magic rides the ``[server]`` extra. A missing sniffer REFUSES the upload
(503, naming the extra); the endpoint used to log "skipping MIME type validation
(security risk!)" and accept the file unsniffed, which is a security check that
turns itself off.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from core.surfaces.inbound_attachments import UPLOAD_MIME_TYPES

logger = logging.getLogger(__name__)


def _sniff_upload_mime(file_content: bytes, filename: str) -> str:
    """Detect the REAL MIME type from bytes and refuse anything outside
    ``UPLOAD_MIME_TYPES``; refuse (503) when the sniffer itself is absent."""
    try:
        import magic
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Upload refused: the MIME sniffer (python-magic) is not installed, "
                   "so the file type cannot be verified. Remedy: pip install "
                   "'polyrob[server]' (add `server` to PROD_EXTRAS on a deployed box).")
    try:
        detected_mime = magic.from_buffer(file_content, mime=True)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Upload refused: MIME detection failed: {e}")
    if detected_mime not in UPLOAD_MIME_TYPES:
        logger.warning(f"Rejected file with MIME type {detected_mime}: {filename}")
        raise HTTPException(status_code=400, detail=f"Invalid file type detected: {detected_mime}.")
    logger.info(f"File MIME type validated: {detected_mime} for {filename}")
    return detected_mime
