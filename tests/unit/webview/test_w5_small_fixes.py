"""113 W5 item 8 — the small fixes that live in templates, CSS and copy.

* IA-3: the Work subtitle names the page, not the Log tab (the Log tab keeps its line).
* A-2: the composer carries a 2 px ring on ``:focus-within``.
* IA-4: the no-wallet words name the one action; the token list summary is in copy.
* ST-3, C-10: a remedy names the console control (the Chat box verb) before the
  terminal command.
* A-1, IA-5 were done in W0: the banner has no flag name, Money follows the hash.
"""
import re
from pathlib import Path

from webview.copy import t

WEBVIEW = Path(__file__).resolve().parents[3] / "webview"


def test_the_work_subtitle_is_about_the_page():
    work = (WEBVIEW / "templates" / "work.html").read_text()
    head = work.split('<nav class="subnav"', 1)[0]
    assert "t('work.sub')" in head
    assert "t('work.log.lead')" in work.split('data-pane="log"', 1)[1]
    assert "goals" in t("work.sub") and "scheduled jobs" in t("work.sub")
    assert t("work.log.lead").startswith("Everything the agent has done")


def test_the_composer_has_a_two_pixel_focus_ring():
    css = (WEBVIEW / "static" / "app" / "app.css").read_text()
    rule = re.search(r"\.composer:focus-within\{([^}]*)\}", css)
    assert rule and "outline:2px solid" in rule.group(1)


def test_the_no_wallet_state_names_its_action():
    money = (WEBVIEW / "templates" / "money.html").read_text()
    assert "data-empty_how=\"{{ t('money.book.empty_how') }}\"" in money
    assert "data-tok_show=\"{{ t('money.tokens.show') }}\"" in money
    assert "polyrob wallet init" in t("money.book.empty_how")
    assert "{count}" in t("money.tokens.show")


def test_remedies_name_the_console_control_first():
    for key, verb in (("agent.ov_identity_no_avatar", "/avatar set"),
                      ("agent.ov_identity_default", "/avatar set"),
                      ("agent.id_face_body", "/avatar set"),
                      ("agent.cap_mcp_note", "/mcp"),
                      ("money.moves.reach", "/bridge")):
        text = t(key)
        assert "in Chat" in text, key
        assert text.index(verb) < text.index("terminal"), key


def test_the_read_only_banner_names_no_flag():
    for name in ("shell.html", "layout.html"):
        assert "WEBVIEW_READ_ONLY" not in (WEBVIEW / "templates" / name).read_text()


def test_a_state_pill_is_filled_and_borderless():
    """113 W5 item 7 (VS-3): a badge must not look like a button. Only the
    "not known" pill keeps a (dashed) rule."""
    css = (WEBVIEW / "static" / "app" / "app.css").read_text()
    base = re.search(r"\n\.pill\{([^}]*)\}", css).group(1)
    assert "border:1px solid transparent" in base and "background:var(--surface-2)" in base
    for state, wash in (("running", "--running-dim"), ("needs-you", "--needs-you-dim"),
                        ("stopped", "--stopped-dim")):
        rule = re.search(r"\.pill\.is-%s\s*\{([^}]*)\}" % state, css).group(1)
        assert f"background:var({wash})" in rule and "border-color" not in rule, state
    unknown = re.search(r"\.pill\.is-unknown\s*\{([^}]*)\}", css).group(1)
    assert "border-style:dashed" in unknown


def test_a_link_in_a_page_line_is_underlined():
    css = (WEBVIEW / "static" / "app" / "app.css").read_text()
    assert re.search(r"\.page-sub a\{[^}]*text-decoration:underline", css)
