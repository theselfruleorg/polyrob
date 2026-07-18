"""render_invoice_card — branded PNG invoice card (Task 6, Phase 1).

Pure Pillow, deterministic, fail-open. This is the FIRST layout on the
compositor (the future presence-plan card layouts reuse its internal
helpers); only the invoice layout is a public entry today.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from modules.pfp import cards

_INVOICE = {
    "request_id": "inv_marker123",
    "amount_usd": 12.34,
    "asset": "usdc",
    "chain": "base",
    "recipient": "0xTREASURYADDR000000000000000000000001",
    "purpose": "MARKER_PURPOSE_STRING for the research widget",
    "expires_at_epoch": 1770000000,  # 2026-02-02 02:40 UTC
    "status": "pending",
}
_ARTIFACT_NO_QR = {
    "pay_text": "Pay $12.34 USDC on base to 0xTREASURYADDR000000000000000000000001",
    "pay_uri": None,
}
_ARTIFACT_WITH_QR = {
    "pay_text": "Pay $12.34 USDC on base to 0xTREASURYADDR000000000000000000000001",
    "pay_uri": "0xTREASURYADDR000000000000000000000001",
}


def _spy_draw_text(monkeypatch):
    """Record every ImageDraw.text() call's text argument — a robust,
    OCR-free way to assert 'this field landed on the card'."""
    drawn = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **kw):
        drawn.append(text)
        return orig(self, xy, text, *a, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return drawn


def test_renders_a_valid_png_with_sane_dimensions(tmp_path):
    out = cards.render_invoice_card(_INVOICE, _ARTIFACT_WITH_QR, tmp_path / "card.png")
    assert isinstance(out, Path)
    assert out.exists()
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(out)
    assert img.width >= 400
    assert img.height >= 400


def test_all_core_fields_land_on_the_card(tmp_path, monkeypatch):
    drawn = _spy_draw_text(monkeypatch)
    cards.render_invoice_card(_INVOICE, _ARTIFACT_WITH_QR, tmp_path / "card.png")
    joined = "\n".join(str(t) for t in drawn)
    assert _INVOICE["request_id"] in joined
    assert "12.34" in joined
    assert "MARKER_PURPOSE_STRING" in joined
    assert "2026-02-02" in joined  # human expiry rendered from epoch


def test_billed_to_lands_when_payer_contact_present(tmp_path, monkeypatch):
    drawn = _spy_draw_text(monkeypatch)
    invoice = dict(_INVOICE, payer_contact="alice@example.com")
    cards.render_invoice_card(invoice, _ARTIFACT_WITH_QR, tmp_path / "card.png")
    joined = "\n".join(str(t) for t in drawn)
    assert "alice@example.com" in joined


def test_billed_to_omitted_when_payer_contact_absent(tmp_path, monkeypatch):
    drawn = _spy_draw_text(monkeypatch)
    assert "payer_contact" not in _INVOICE
    cards.render_invoice_card(_INVOICE, _ARTIFACT_WITH_QR, tmp_path / "card.png")
    joined = "\n".join(str(t) for t in drawn)
    assert "BILLED TO" not in joined.upper() or "billed to" not in joined.lower()


def test_qr_block_omitted_cleanly_when_pay_uri_none(tmp_path, monkeypatch):
    calls = []
    orig = cards._build_qr_image

    def spy(data):
        calls.append(data)
        return orig(data)

    monkeypatch.setattr(cards, "_build_qr_image", spy)
    out = cards.render_invoice_card(_INVOICE, _ARTIFACT_NO_QR, tmp_path / "no_qr.png")
    assert calls == []
    assert out.exists()  # still a clean render, no crash


def test_qr_block_rendered_when_pay_uri_present(tmp_path, monkeypatch):
    calls = []
    orig = cards._build_qr_image

    def spy(data):
        calls.append(data)
        return orig(data)

    monkeypatch.setattr(cards, "_build_qr_image", spy)
    cards.render_invoice_card(_INVOICE, _ARTIFACT_WITH_QR, tmp_path / "with_qr.png")
    assert calls == [_ARTIFACT_WITH_QR["pay_uri"]]


def test_font_loader_falls_back_when_repo_font_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "_FONT_REGULAR", tmp_path / "does-not-exist.ttf")
    monkeypatch.setattr(cards, "_FONT_BOLD", tmp_path / "does-not-exist-bold.ttf")
    font = cards._load_font(20)
    assert font is not None
    assert hasattr(font, "getbbox") or hasattr(font, "getsize")
    # end-to-end: the whole card still renders cleanly on the fallback font
    out = cards.render_invoice_card(_INVOICE, _ARTIFACT_NO_QR, tmp_path / "fallback.png")
    assert out.exists()


def test_bold_font_loader_falls_back_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "_FONT_BOLD", tmp_path / "does-not-exist-bold.ttf")
    font = cards._load_font(20, bold=True)
    assert font is not None


def test_deterministic_given_same_inputs(tmp_path):
    p1 = cards.render_invoice_card(_INVOICE, _ARTIFACT_WITH_QR, tmp_path / "a.png")
    p2 = cards.render_invoice_card(_INVOICE, _ARTIFACT_WITH_QR, tmp_path / "b.png")
    assert p1.read_bytes() == p2.read_bytes()


def test_never_invokes_the_chromium_renderer(tmp_path, monkeypatch):
    """The card must use the store.py fallback logic (generated pfp -> committed
    reference PNG) WITHOUT ever invoking the headless Chromium render path."""
    def boom(*a, **kw):
        raise AssertionError("cards.py must never invoke the Chromium renderer")

    monkeypatch.setattr("modules.pfp.renderer.render_still", boom)
    out = cards.render_invoice_card(_INVOICE, _ARTIFACT_NO_QR, tmp_path / "no_chromium.png")
    assert out.exists()


def test_avatar_falls_back_to_committed_reference_png(tmp_path, monkeypatch):
    # no generated pfp anywhere under this empty data home -> falls back to the
    # committed avatar/renders/rob.png, never raises, never calls Chromium.
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    path = cards._resolve_avatar_path("rob")
    assert path is not None
    assert path.is_file()
    assert path.name == "rob.png"


def test_avatar_prefers_generated_pfp_when_present(tmp_path, monkeypatch):
    from core.instance import pfp_dir
    d = pfp_dir(tmp_path, "rob")
    d.mkdir(parents=True)
    generated = d / "pfp.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(generated, "PNG")
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    path = cards._resolve_avatar_path("rob")
    assert path == generated


def test_render_survives_missing_avatar_entirely(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "_resolve_avatar_path", lambda instance_id: None)
    out = cards.render_invoice_card(_INVOICE, _ARTIFACT_NO_QR, tmp_path / "no_avatar.png")
    assert out.exists()
