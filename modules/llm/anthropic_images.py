"""OpenAI ``image_url`` → Anthropic ``image`` block conversion.

One pure function, lifted out of ``modules/llm/adapters.py`` (2026-09-21) so the
adapter file stays under its god-file ceiling. Behaviour is unchanged: every
failure degrades to a ``text`` block naming WHY the image is missing rather than
dropping it silently, because a vision turn that quietly loses its picture reads
to the model as a turn that never had one.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

_DEFAULT_MEDIA_TYPE = "image/png"


def convert_image_to_anthropic_format(
    image_item: Dict[str, Any],
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Convert ``{"type": "image_url", "image_url": {"url": …}}`` to Anthropic's shape.

    Returns an Anthropic ``image`` block for a ``data:`` URL (base64 source) or an
    ``http(s)`` URL (url source); anything else becomes a ``text`` block that says
    what went wrong.
    """
    log = logger or logging.getLogger(__name__)
    try:
        url = (image_item.get("image_url") or {}).get("url", "")
        if not url:
            log.warning("Empty image URL in image_url format")
            return {"type": "text", "text": "[IMAGE: Empty URL]"}

        if url.startswith("data:"):
            # data:[<mediatype>][;base64],<data>
            try:
                header, base64_data = url.split(",", 1)
            except ValueError as e:
                log.error(f"Failed to parse base64 data URL: {e}")
                return {"type": "text", "text": "[IMAGE: Invalid data URL]"}
            media_type = _DEFAULT_MEDIA_TYPE
            header_content = header[5:] if header.startswith("data:") else ""
            if ";" in header_content:
                media_type = header_content.split(";")[0]
            elif header_content:
                media_type = header_content
            return {"type": "image",
                    "source": {"type": "base64", "media_type": media_type,
                               "data": base64_data}}

        if url.startswith("http://") or url.startswith("https://"):
            return {"type": "image", "source": {"type": "url", "url": url}}

        log.warning(f"Unknown image URL format: {url[:50]}...")
        return {"type": "text", "text": "[IMAGE: Unsupported format]"}
    except Exception as e:
        log.error(f"Error converting image to Anthropic format: {e}")
        return {"type": "text", "text": "[IMAGE: Conversion error]"}
