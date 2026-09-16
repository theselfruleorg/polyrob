"""043 R6 (phase-5 form) — NO console template carries an inline `<script>`.

Fifteen templates once carried one, and they were the reason the console's CSP
shipped ``'unsafe-inline'`` in ``script-src`` (043 §9; the 2026-09-12 audit found
the count had grown from 9 to 15 while proposal 030 §9 claimed the work was
done). An inline handler is also where copy escapes the copy layer, where a page
grows a second nav, and where a value stops matching the store it came from.

Phase 5 finished the job: every template loads its behaviour from an external
``/static/**.js`` file (the copy layer crosses data on ``data-*`` attributes,
the shell.html/chats.js pattern), the last six inline blocks were removed, and
``script-src`` dropped ``'unsafe-inline'`` globally on the console routes.

So the ratchet is no longer "not one more" — it is **zero, ever**. A template
that grows an inline ``<script>`` fails here, and the fix is to move the body
into ``webview/static/**.js`` and load it with ``src=``.

⚠️ ``style-src 'unsafe-inline'`` is DELIBERATELY kept (the shell's
``<dialog style=…>`` escape hatch — see shell.html); this ratchet is about
``<script>``, not ``style=``.
"""
import re
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parents[3] / "webview" / "templates"

#: A `<script>` that carries its own body rather than a `src=`. `type="module"`
#: with a src is fine; a JSON island (`type="application/json"`) is not a script
#: the CSP cares about, and there are none today — if one lands, it will fail
#: here and the right answer is to widen this pattern deliberately, not to
#: widen the allowlist.
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)


def _with_inline_script():
    out = set()
    for path in sorted(_TEMPLATES.rglob("*.html")):
        if _INLINE.search(path.read_text(encoding="utf-8")):
            out.add(path.relative_to(_TEMPLATES).as_posix())
    return out


def test_no_template_carries_an_inline_script():
    found = sorted(_with_inline_script())
    assert not found, (
        "these templates carry an inline <script>, which is what would keep "
        "'unsafe-inline' in the console's CSP script-src: " + ", ".join(found) +
        ". Put the behaviour in webview/static/**.js and load it with src=.")


def test_the_scan_is_not_vacuous():
    """The scan must actually be looking at the templates."""
    scanned = list(_TEMPLATES.rglob("*.html"))
    assert len(scanned) >= 10, f"the scan found only {len(scanned)} templates"
    # A known template with a de-inlined script still parses (proves the scan
    # reads real files, not an empty set).
    assert (_TEMPLATES / "layout.html").is_file()


def test_the_console_csp_dropped_unsafe_inline_from_script_src():
    """R6: the console route CSP emits no `'unsafe-inline'` in `script-src`.

    ⚠️ The `/workspace/serve/` endpoint keeps a RELAXED CSP on purpose — it
    serves agent-built artifacts (presentations, HTML apps) that legitimately
    carry inline scripts — so this pins the console (non-serve) directive only.
    """
    server = (Path(__file__).resolve().parents[3] / "webview" / "server.py").read_text()
    # Isolate the console (else-branch) CSP: the directive that lists cdn.socket.io.
    m = re.search(r'"script-src \'self\'[^"]*cdn\.socket\.io[^"]*"', server)
    assert m, "could not find the console script-src directive in server.py"
    assert "'unsafe-inline'" not in m.group(0), (
        "the console CSP script-src still carries 'unsafe-inline': " + m.group(0))


def test_a_new_shell_template_may_not_carry_one():
    """The five destinations are the point: they start clean and stay clean."""
    shell = {p.relative_to(_TEMPLATES).as_posix()
             for p in sorted(_TEMPLATES.glob("*.html"))
             if 'extends "shell.html"' in p.read_text(encoding="utf-8")
             or p.name == "shell.html"}
    assert shell, "no shell templates found — this test is not looking at them"
    assert not (shell & _with_inline_script()), (
        "a 043 shell template carries an inline script: "
        + ", ".join(sorted(shell & _with_inline_script())))


def test_the_pattern_sees_an_inline_script_and_ignores_a_src():
    assert _INLINE.search('<script>alert(1)</script>')
    assert _INLINE.search('<script type="module">import "x"</script>')
    assert not _INLINE.search('<script src="/static/app/x.js"></script>')
    assert not _INLINE.search('<script type="module" src="/static/app/x.js"></script>')
