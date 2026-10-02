"""058 T1.6 — the single-feature deps live behind the extras that own them.

One parametrised shape per dep: the module still imports without it, and the
feature's refusal names its extra. python-magic additionally must REJECT the
upload — a test checking only the error string would pass on a path that
accepted the file and logged.
"""
import builtins
import logging
import sys

import pytest

from tests.unit.modules.cards.test_cards import _ARTIFACT_WITH_QR, _INVOICE


def _block(monkeypatch, *names):
    for mod in list(sys.modules):
        if any(mod == n or mod.startswith(n + ".") for n in names):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    real = builtins.__import__

    def fake(name, *a, **kw):
        if any(name == n or name.startswith(n + ".") for n in names):
            raise ImportError(f"No module named '{name}' (blocked by test)")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")


# --- docs: pypdf / python-docx via the ONE parser seam ------------------------ #

@pytest.mark.parametrize("kind,dist", [("pdf", "pypdf"), ("docx", "python-docx")])
def test_document_parser_refuses_naming_docs_and_the_workspace(monkeypatch, kind, dist):
    import core.lazy_deps as ld
    monkeypatch.setattr(ld, "_dist_present", lambda n: n != dist)
    from tools import document_parser as parser
    with pytest.raises(parser.DocumentParseError) as exc:
        parser.parse_document(kind, b"%PDF-1.4 not really")
    msg = str(exc.value)
    assert "polyrob[docs]" in msg and "workspace" in msg


def test_filesystem_pdf_mixin_imports_without_pypdf(monkeypatch):
    _block(monkeypatch, "pypdf")
    monkeypatch.delitem(sys.modules, "tools.filesystem_pdf", raising=False)
    import importlib
    mod = importlib.import_module("tools.filesystem_pdf")
    assert mod.pypdf is None
    from core.exceptions import ServiceError
    with pytest.raises(ServiceError, match=r"polyrob\[docs\]"):
        mod.PdfExtractionMixin()._read_pdf_with_recovery(None)


# --- media: imageio / qrcode ----------------------------------------------- #

def test_gif_falls_back_to_text_only_and_names_media(monkeypatch, tmp_path, caplog):
    _block(monkeypatch, "imageio")
    from utils import gif_utils
    shot = tmp_path / "s.png"
    from PIL import Image
    Image.new("RGB", (8, 8)).save(shot)
    with caplog.at_level(logging.WARNING):
        ok = gif_utils.create_gif_with_retry([str(shot)], str(tmp_path / "out.gif"), ["c"], max_retries=3)
    assert ok is True                       # text-only GIF still produced
    assert "polyrob[media]" in caplog.text
    assert caplog.text.count("attempt") <= 1  # an ImportError is not retried


def test_invoice_card_renders_without_qrcode_and_names_media(monkeypatch, tmp_path, caplog):
    _block(monkeypatch, "qrcode")
    from modules.cards import cards
    with caplog.at_level(logging.WARNING):
        out = cards.render_invoice_card(_INVOICE, _ARTIFACT_WITH_QR, tmp_path / "card.png")
    assert out.is_file()
    assert "polyrob[media]" in caplog.text


# --- server: python-magic must REFUSE the upload ---------------------------- #

def test_upload_is_rejected_when_the_sniffer_is_missing(monkeypatch):
    _block(monkeypatch, "magic")
    from fastapi import HTTPException
    from api.upload_sniff import _sniff_upload_mime
    with pytest.raises(HTTPException) as exc:
        _sniff_upload_mime(b"%PDF-1.4", "x.pdf")
    assert exc.value.status_code == 503
    assert "polyrob[server]" in str(exc.value.detail)


def test_upload_endpoint_sniffs_before_it_writes():
    """The refusal above only protects the upload if the endpoint calls the
    sniffer before the write. Pin the order in source."""
    from pathlib import Path
    src = Path("api/task_http_api.py").read_text()
    body = src[src.index("async def upload_document("):]
    assert body.index("_sniff_upload_mime(") < body.index("Sanitize filename")


# --- anysite ------------------------------------------------------------------ #

def test_anysite_missing_binary_names_its_extra(monkeypatch):
    at = pytest.importorskip("polyrob_discovery.anysite.tool")
    monkeypatch.setattr(at, "missing_requirement", lambda: "binary")
    assert "polyrob[anysite]" in at._unavailable_reason()


# --- 058 follow-up: every lazy row has a caller; the extras map knows the moves ---

@pytest.mark.parametrize("feature,call", [
    ("media.gif", lambda: __import__("utils.gif_utils", fromlist=["x"]).create_gif_with_retry(["/nonexistent-shot.png"], "/tmp/x.gif", ["c"], max_retries=1)),
    ("media.qr", lambda: __import__("modules.cards.cards", fromlist=["x"])._build_qr_image("0xabc")),
    ("tool.anysite", lambda: __import__("polyrob_discovery.anysite.client", fromlist=["x"]).missing_requirement()),
    ("docs.pdf", lambda: __import__("tools.filesystem_pdf", fromlist=["x"])._require_pypdf()),
])
def test_first_use_asks_lazy_deps_for_the_extra(monkeypatch, feature, call):
    """core/lazy_deps rows with no caller never install anything: the guide
    promises 'the first use installs it', so each site must ask."""
    import core.lazy_deps as ld
    asked = []
    monkeypatch.setattr(ld, "ensure", lambda f, *, prompt=True: asked.append(f))
    if feature == "media.gif":
        monkeypatch.setattr("utils.gif_utils.create_text_only_gif", lambda *a, **k: True)
    if feature == "tool.anysite":
        pytest.importorskip("polyrob_discovery")
        monkeypatch.setattr("polyrob_discovery.anysite.client.binary_available", lambda: False)
    if feature == "docs.pdf":
        # importlib, not `import … as`: after an eviction test the parent package
        # attribute and sys.modules can name different module objects, and the
        # lambda above resolves through sys.modules.
        import importlib
        monkeypatch.setattr(importlib.import_module("tools.filesystem_pdf"), "pypdf", None)
    try:
        call()
    except Exception:
        pass  # the refusal itself is covered above; here only the ask matters
    assert feature in asked


def test_extras_map_knows_every_058_move():
    from core.optional_extras import EXTRA_FOR_MODULE, MODULES_FOR_EXTRA, missing_extra_hint
    for mod, extra in {"pypdf": "docs", "docx": "docs", "imageio": "media", "qrcode": "media",
                       "magic": "server", "apsw": "memory-vector", "sqlite_vec": "memory-vector",
                       "anthropic": "anthropic", "google": "gemini"}.items():
        assert EXTRA_FOR_MODULE[mod] == extra, mod
        assert f"polyrob[{extra}]" in (missing_extra_hint(f"No module named '{mod}'") or ""), mod
    assert "magic" in MODULES_FOR_EXTRA["server"]
    assert {"docs", "media", "gemini", "anthropic"} <= set(MODULES_FOR_EXTRA)


def test_doctor_names_the_extra_not_the_packages():
    from pathlib import Path
    src = Path("cli/commands/doctor.py").read_text()
    assert "install apsw + sqlite-vec" not in src
    assert src.count("polyrob[memory-vector]") >= 3


# --- 062: the extras preflight OFFERS the install on a lazy-enabled box ------

@pytest.mark.parametrize("extra,feature", [
    ("server", "server.api"),
    ("telegram", "surface.telegram"),
    ("voice", "voice.transcribe"),
])
def test_require_extra_or_exit_asks_lazy_deps(monkeypatch, extra, feature):
    """A row with no caller is a false promise (058's lesson). The ONE preflight
    every surface command already calls is that caller."""
    import core.lazy_deps as ld
    import core.optional_extras as oe
    from cli.commands import _errors

    asked = []
    monkeypatch.setattr(ld, "ensure", lambda f, *, prompt=True: asked.append(f))

    calls = {"n": 0}

    def fake_require(name, modules=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ImportError(f"missing {name}")
        return None  # the "install" landed

    monkeypatch.setattr(oe, "require_extra", fake_require)
    _errors.require_extra_or_exit(extra)
    assert asked == [feature]


def test_require_extra_or_exit_still_exits_when_the_extra_has_no_row(monkeypatch):
    """`crypto` has no lazy row; the preflight must report, never guess."""
    import core.optional_extras as oe
    from cli.commands import _errors

    monkeypatch.setattr(oe, "require_extra",
                        lambda name, modules=None: (_ for _ in ()).throw(ImportError("nope")))
    with pytest.raises(SystemExit):
        _errors.require_extra_or_exit("crypto")
