"""Branded PNG card compositor — pure Pillow, deterministic, fail-open (Task 6).

``render_invoice_card`` is the first concrete layout: an x402 invoice rendered
as a branded PNG (Mindprint avatar, amount, purpose, QR, "billed to", pay
instructions). This module is ALSO the intended future home of the
presence-plan card layouts (avatar-card compositor) — the internal helpers
(font loader, text wrap, panel drawer) are written generically enough for a
future ``announcement`` layout to reuse, but no second layout is built yet
(YAGNI: only the invoice layout is public today).

Design constraints:
- Pure Pillow (no headless browser). NEVER invokes the Chromium avatar
  renderer (``modules.pfp.renderer``) — the avatar is resolved the same way
  ``modules.pfp.store.generate_pfp`` falls back (generated instance pfp, else
  the committed reference PNG), reusing that module's fallback constant
  directly rather than re-deriving the path (single source of truth).
- Deterministic given the same inputs: no timestamps/randomness inside (the
  card's own "rendered at" concept doesn't exist — only invoice fields, which
  are supplied by the caller).
- Fail-open at the FONT level (falls back to ``ImageFont.load_default()``
  when the shipped repo font is unavailable) and expects its caller
  (``tools/x402/invoice_tool.py``) to fail-open at the RENDER level (log a
  WARN, keep the text-only result) — this module itself may still raise on a
  genuinely broken output path (e.g. an unwritable ``out_path``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# --- Shipped font (Task 6 deliverable #2) -----------------------------------
# NEVER reference macOS/system font paths (prod is Linux) — the loader falls
# back to PIL's built-in default font, never a guessed OS path.
_FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "dejavu"
_FONT_REGULAR = _FONTS_DIR / "DejaVuSans.ttf"
_FONT_BOLD = _FONTS_DIR / "DejaVuSans-Bold.ttf"

# --- Brand palette (matches web/portal/brand/render_og_card.py) -------------
_BG = (10, 10, 12)
_GREEN = (86, 226, 194)
_WHITE = (235, 235, 235)
_MUTED = (150, 150, 155)
_RULE = (48, 48, 52)
_QR_PANEL = (255, 255, 255)

# --- Layout constants ---------------------------------------------------
CARD_W = 960
# The card is drawn on a generous scratch canvas then CROPPED to the actual
# content height (+footer+margin) at the end, so a short purpose/pay-text
# doesn't leave a huge dead zone and a long one never overflows/overlaps the
# footer — the height is content-driven, not a guessed worst case. Still
# fully deterministic: same inputs -> same content -> same crop -> same PNG.
_SCRATCH_CARD_H = 4000
_MARGIN = 56
_AVATAR_SIZE = 96
_QR_SIZE = 220


def _load_font(size: int, *, bold: bool = False):
    """Repo font -> ``ImageFont.load_default()`` fallback. Never raises.

    Return type deliberately untyped: ``ImageFont.truetype()`` returns a
    ``FreeTypeFont`` and ``ImageFont.load_default()`` returns an unrelated
    ``ImageFont``/``FreeTypeFont`` depending on Pillow version — both are
    duck-type compatible with everything this module does (``draw.text(font=)``).
    """
    path = _FONT_BOLD if bold else _FONT_REGULAR
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        logger.warning("modules.pfp.cards: repo font unavailable at %s, "
                        "falling back to the PIL default font", path)
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            # Older Pillow: load_default() takes no size kwarg.
            return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return draw.textbbox((0, 0), text, font=font)[2]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int,
                *, max_lines: Optional[int] = None) -> List[str]:
    """Word-wrap ``text`` to fit ``max_width`` px, measured via ``draw``/``font``.

    Generic (no invoice-specific knowledge) so a future card layout can reuse
    it. When ``max_lines`` is given and wrapping would exceed it, the last
    line is truncated with an ellipsis rather than silently dropping content
    with no indication.
    """
    if not text:
        return [""]
    lines: List[str] = []
    for para in text.splitlines() or [text]:
        words = para.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or _text_width(draw, candidate, font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _text_width(draw, last + "…", font) > max_width:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines


def _panel(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], *,
           fill=None, outline=None, radius: int = 16, width: int = 2) -> None:
    """A rounded-rect background panel — generic grid primitive for reuse by a
    future layout (e.g. the presence-plan announcement card)."""
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _circular_mask(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, img.size[0], img.size[1]), fill=255)
    img.putalpha(mask)
    return img


def _build_qr_image(data: str) -> Image.Image:
    import qrcode

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def _safe_instance_id() -> str:
    try:
        from core.instance import resolve_instance_id
        return resolve_instance_id()
    except Exception:
        return "polyrob"


def _resolve_avatar_path(instance_id: str) -> Optional[Path]:
    """Generated instance pfp if it exists, else the committed reference PNG
    (mirrors ``modules.pfp.store.generate_pfp``'s fallback) — NEVER invokes
    the Chromium renderer. Fail-open: returns ``None`` on any resolution error
    or when neither file exists."""
    try:
        from core.instance import pfp_path
        from core.runtime_paths import resolve_data_home
        home = resolve_data_home()
        candidate = pfp_path(home, instance_id)
        if candidate.is_file():
            return candidate
    except Exception:
        logger.debug("modules.pfp.cards: generated pfp lookup failed", exc_info=True)
    try:
        # Reuse store.py's own committed-reference constant (single source of
        # truth for "what is the fallback avatar file") rather than
        # re-deriving the same relative path a second time.
        from .store import _DEFAULT_REFERENCE
        return _DEFAULT_REFERENCE if _DEFAULT_REFERENCE.is_file() else None
    except Exception:
        return None


def _load_avatar(instance_id: str) -> Optional[Image.Image]:
    path = _resolve_avatar_path(instance_id)
    if path is None:
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        logger.warning("modules.pfp.cards: failed to open avatar %s", path, exc_info=True)
        return None


def _format_epoch(epoch: Any) -> str:
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "—"


def render_invoice_card(invoice: Dict[str, Any], artifact: Dict[str, Any],
                         out_path) -> Path:
    """Render an x402 invoice as a branded PNG. Pure Pillow, deterministic.

    ``invoice`` is the dict shape ``modules.x402.invoicing.create_payment_request``
    returns (request_id/amount_usd/purpose/expires_at_epoch/...); it may
    optionally carry ``payer_contact`` (a free-form "billed to" string — read
    optimistically, a later task populates it; the line is simply omitted
    when absent). ``artifact`` is ``modules.x402.artifact.build_payment_artifact``'s
    output (``pay_text``/``pay_uri``); the QR block is omitted cleanly when
    ``pay_uri`` is ``None``.

    Returns the written ``Path``. May raise on a genuinely broken output path
    (unwritable ``out_path``) — the caller (``tools/x402/invoice_tool.py``) is
    responsible for the fail-open contract (catch, log WARN, keep text-only).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (CARD_W, _SCRATCH_CARD_H), _BG)
    draw = ImageDraw.Draw(img)

    instance_id = _safe_instance_id()
    name = instance_id or "polyrob"

    y = _MARGIN

    # --- header: avatar + name -------------------------------------------------
    avatar = _load_avatar(instance_id)
    name_x = _MARGIN
    if avatar is not None:
        avatar = avatar.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)
        avatar = _circular_mask(avatar)
        img.paste(avatar, (_MARGIN, y), avatar)
        name_x = _MARGIN + _AVATAR_SIZE + 24

    name_font = _load_font(32, bold=True)
    sub_font = _load_font(17)
    draw.text((name_x, y + 14), name, font=name_font, fill=_WHITE)
    draw.text((name_x, y + 54), "payment request", font=sub_font, fill=_MUTED)
    y += _AVATAR_SIZE + 32

    draw.line([(_MARGIN, y), (CARD_W - _MARGIN, y)], fill=_RULE, width=2)
    y += 36

    # --- amount, prominent -------------------------------------------------
    # Task 11 C1 fix: full precision when the amount carries sub-cent jitter
    # (`modules.x402.invoicing._dedupe_amount_for_treasury`) — this card is a
    # third payer-facing surface (alongside the x402_request text and the
    # artifact pay_text) that must never round away the disambiguating digits,
    # or a payer paying what the card shows could settle a DIFFERENT invoice.
    try:
        from modules.x402.artifact import format_invoice_amount
        amount_text = format_invoice_amount(invoice.get("amount_usd"))
    except Exception:
        try:
            amount_text = f"{float(invoice.get('amount_usd') or 0.0):.2f}"
        except (TypeError, ValueError):
            amount_text = "0.00"
    amount_font = _load_font(68, bold=True)
    draw.text((_MARGIN, y), f"${amount_text} USDC", font=amount_font, fill=_GREEN)
    y += 92

    # --- purpose -------------------------------------------------
    purpose = str(invoice.get("purpose") or "").strip() or "(no purpose given)"
    label_font = _load_font(15, bold=True)
    body_font = _load_font(22)
    draw.text((_MARGIN, y), "PURPOSE", font=label_font, fill=_MUTED)
    y += 24
    for line in _wrap_text(draw, purpose, body_font, CARD_W - 2 * _MARGIN, max_lines=4):
        draw.text((_MARGIN, y), line, font=body_font, fill=_WHITE)
        y += 30
    y += 20

    # --- QR block (top-right of the meta zone), omitted cleanly when absent --
    pay_uri = artifact.get("pay_uri") if isinstance(artifact, dict) else None
    meta_top = y
    text_max_width = CARD_W - 2 * _MARGIN
    if pay_uri:
        qr_img = _build_qr_image(pay_uri).resize((_QR_SIZE, _QR_SIZE), Image.NEAREST)
        qr_x = CARD_W - _MARGIN - _QR_SIZE
        qr_y = meta_top
        pad = 16
        _panel(draw, (qr_x - pad, qr_y - pad, qr_x + _QR_SIZE + pad, qr_y + _QR_SIZE + pad),
               fill=_QR_PANEL, radius=12)
        img.paste(qr_img, (qr_x, qr_y))
        text_max_width = CARD_W - 2 * _MARGIN - (_QR_SIZE + 2 * pad + 24)

    # --- meta rows: request_id / expires / billed to -------------------------
    meta_label_font = _load_font(14, bold=True)
    meta_value_font = _load_font(19)

    def _meta_row(label: str, value: str) -> None:
        nonlocal y
        draw.text((_MARGIN, y), label, font=meta_label_font, fill=_MUTED)
        y += 22
        for line in _wrap_text(draw, value, meta_value_font, text_max_width, max_lines=2):
            draw.text((_MARGIN, y), line, font=meta_value_font, fill=_WHITE)
            y += 25
        y += 14

    _meta_row("REQUEST ID", str(invoice.get("request_id") or "—"))
    _meta_row("EXPIRES", _format_epoch(invoice.get("expires_at_epoch")))

    payer_contact = invoice.get("payer_contact")
    if payer_contact:
        _meta_row("BILLED TO", str(payer_contact))

    if pay_uri:
        y = max(y, meta_top + _QR_SIZE + 32 + 16)

    # --- pay instructions -------------------------------------------------
    pay_text = (artifact.get("pay_text") if isinstance(artifact, dict) else None) or ""
    instr_label_font = _load_font(14, bold=True)
    instr_font = _load_font(17)
    draw.text((_MARGIN, y), "HOW TO PAY", font=instr_label_font, fill=_MUTED)
    y += 22
    for line in _wrap_text(draw, pay_text, instr_font, CARD_W - 2 * _MARGIN, max_lines=6):
        draw.text((_MARGIN, y), line, font=instr_font, fill=_WHITE)
        y += 23

    y += 24

    # --- footer -------------------------------------------------
    footer_font = _load_font(16)
    footer_text = f"⚡ {name} · a polyrob"
    draw.text((_MARGIN, y), footer_text, font=footer_font, fill=_MUTED)
    footer_h = draw.textbbox((0, 0), footer_text, font=footer_font)[3]
    y += footer_h + _MARGIN

    # Content-driven height: crop the generous scratch canvas down to what was
    # actually drawn (+ bottom margin) instead of guessing a worst-case size.
    img = img.crop((0, 0, CARD_W, min(y, _SCRATCH_CARD_H)))
    img.save(out_path, "PNG")
    return out_path
