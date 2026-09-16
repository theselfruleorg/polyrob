"""043 C4 — the console's voice is one file, and it obeys the copy rules.

Two halves, and the second is the one with teeth.

**Half one: the copy lives in the copy layer.** No prose in a route handler, no
prose in a template. That is not tidiness — copy scattered across 26 templates,
15 inline scripts and a dozen handlers is copy nobody ever reads *as copy*,
which is how ``MODEL``, ``TOOLS``, ``VISION``,
``session 1f9a28e6 · tools filesystem, task, … · /help`` and
``no goals — GOALS_ENABLED=off`` all reached a person's screen.

**Half two: the strings obey rule 3 of the design system** — no ALL-CAPS label,
no ``A · B · C`` meta string, no flag name, no function name, no proposal id.
Those are the current product's own tells, and a rule that is only written down
is a rule that comes back.

The scan grows by itself. Modules are ``webview/*.py`` minus a frozen list of
the legacy console's own files, and templates are ``shell.html`` plus everything
that extends it — so a file added by a later batch is covered the moment it
exists and cannot opt out of the ratchet by being new.
"""
import ast
import re
import string
from pathlib import Path

import pytest

from webview.copy import STRINGS, t

_REPO = Path(__file__).resolve().parents[3]
_WEBVIEW = _REPO / "webview"
_TEMPLATES = _WEBVIEW / "templates"

#: The LEGACY console, frozen as the list of ``webview/*.py`` modules that
#: existed when the copy layer arrived (043 C3/C4). They predate it and are not
#: held to it; bringing one in is a deliberate act of deleting its row here.
#:
#: Everything else in ``webview/*.py`` is scanned. That is the point: a module
#: created AFTER this list — ``inbox.py``, ``chat_open.py``, whatever comes next
#: — is covered the moment it exists, and cannot opt out of the ratchet by
#: being new. ``webview/copy/`` is a package, not a top-level module, so it is
#: never scanned: it IS the copy.
_LEGACY_MODULES = frozenset({
    "__init__.py",
    "activity.py",
    "apps_routes.py",
    "console_commands.py",
    "emit_api.py",
    "knowledge.py",
    "owner_auth.py",
    "owner_login_flow.py",
    "pages.py",
    "posture_guard.py",
    "repair_sessions.py",
    "serve_tokens.py",
    "server.py",
    "server_launcher.py",
    "stats_service.py",
    "template_globals.py",
    "webgate.py",
})

#: The only ALL-CAPS words allowed in a string a person reads. A brand is a
#: proper noun; ``TOOLS`` is not.
_BRANDS = {"POLYROB"}

_ALLCAPS = re.compile(r"\b[A-Z]{4,}\b")
_FLAG_NAME = re.compile(r"[A-Z_]{4,}_[A-Z_]+")
_META_SEPARATOR = " · "
#: A call in prose: ``build_recap()``, ``GoalBoard.asks()``. Machine names stay
#: one disclosure away, never on the first screen.
_FUNCTION_CALL = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*\(\)")


def _modules():
    """Every top-level ``webview`` module the copy layer covers."""
    return [p for p in sorted(_WEBVIEW.glob("*.py"))
            if p.name not in _LEGACY_MODULES]


def _shell_templates():
    """``shell.html`` and everything that extends it."""
    out = []
    for path in sorted(_TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if path.name == "shell.html" or 'extends "shell.html"' in text:
            out.append(path)
    return out


# --- half one: no prose outside the copy layer ------------------------------ #

def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _logged_or_keyed(tree):
    """Strings that are not shown to a person: log messages and copy KEYS."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        target = getattr(func, "value", None)
        is_log = name in {"debug", "info", "warning", "error", "exception",
                          "critical"} and getattr(target, "id", "") == "logger"
        is_copy = name == "t"
        if is_log or is_copy:
            for arg in ([node.args[0]] if (is_copy and node.args) else node.args):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.add(id(arg))
    return out


def _looks_like_prose(value: str) -> bool:
    """More than three words of something a person could read."""
    if len(value.split()) <= 3:
        return False
    # An identifier, a path, a mime type, a format string of machine parts.
    if not re.search(r"[a-z]{2,}\s+[a-z]{2,}", value):
        return False
    return True


def prose_offenders(source: str) -> list:
    """Every prose literal in *source* that does not go through ``t()``."""
    tree = ast.parse(source)
    exempt = _docstring_nodes(tree) | _logged_or_keyed(tree)
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in exempt and _looks_like_prose(node.value)]


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_prose_literal_outside_the_copy_layer(path):
    offenders = prose_offenders(path.read_text(encoding="utf-8"))
    assert not offenders, (
        f"{path.name} says these itself instead of through t(): {offenders}")


def template_prose(text: str) -> list:
    """Every run of prose a template writes itself, Jinja and tags removed."""
    text = re.sub(r"\{#.*?#\}", " ", text, flags=re.S)
    text = re.sub(r"\{%.*?%\}", " ", text, flags=re.S)
    text = re.sub(r"\{\{.*?\}\}", "   ", text, flags=re.S)
    text = re.sub(r"<[^>]*>", " ", text)  # drop tags and their attributes
    return [c.strip() for c in text.split("  ") if _looks_like_prose(c.strip())]


@pytest.mark.parametrize("path", _shell_templates(), ids=lambda p: p.name)
def test_no_prose_text_node_in_a_shell_template(path):
    found = template_prose(path.read_text(encoding="utf-8"))
    assert not found, f"{path.name} writes copy directly: {found}"


@pytest.mark.parametrize("path", _shell_templates(), ids=lambda p: p.name)
def test_every_string_in_a_template_expression_goes_through_t(path):
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\{#.*?#\}", " ", text, flags=re.S)
    for expr in re.findall(r"\{\{(.*?)\}\}|\{%(.*?)%\}", text, flags=re.S):
        source = (expr[0] or expr[1])
        for literal in re.findall(r"""['"]([^'"]{2,})['"]""", source):
            if "t(" in source and literal in STRINGS:
                continue
            # A nav key, a class name, a comparison against a destination id.
            assert " " not in literal, (
                f"{path.name} has a bare string in an expression: {literal!r}")


def test_the_scan_is_not_vacuous():
    assert any(p.name == "pages_new.py" for p in _modules())
    assert {p.name for p in _shell_templates()} >= {"shell.html", "inbox.html"}


def test_a_new_module_is_scanned_without_being_listed():
    """The property Important 4 asks for, asserted rather than described."""
    scanned = {p.name for p in _modules()}
    present = {p.name for p in _WEBVIEW.glob("*.py")}
    assert scanned == present - _LEGACY_MODULES
    assert "pages.py" not in scanned, "the legacy webgate is deliberately exempt"
    assert not any(p.parent.name == "copy" for p in _modules()), (
        "webview/copy is the copy — scanning it would be circular")


def test_the_legacy_exemption_has_no_stale_rows():
    """A renamed or deleted legacy module must not leave a row behind.

    A stale row is an exemption nobody can see is dead, and the next file to
    take that name inherits it silently.
    """
    present = {p.name for p in _WEBVIEW.glob("*.py")}
    stale = sorted(_LEGACY_MODULES - present)
    assert not stale, f"these exemptions name modules that no longer exist: {stale}"


# --- the keys actually resolve ---------------------------------------------- #

def _referenced_keys():
    keys = set()
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call)
                    and (getattr(node.func, "id", None) == "t")
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
    for path in _shell_templates():
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"\{#.*?#\}", " ", text, flags=re.S)
        keys.update(re.findall(r"""\bt\(\s*['"]([^'"]+)['"]""", text))
    return keys


def test_every_key_the_interface_asks_for_exists():
    """A typo must fail here, not on the one render nothing covers."""
    missing = sorted(k for k in _referenced_keys() if k not in STRINGS)
    assert not missing, f"no copy for: {missing}"


def test_the_key_scan_found_the_interface():
    assert len(_referenced_keys()) >= 10


def test_the_destinations_each_have_a_title_and_a_body():
    for key in ("new", "inbox", "work", "money", "agent"):
        assert f"{key}.title" in STRINGS
        assert f"{key}.placeholder" in STRINGS


# --- half two: the copy rules ----------------------------------------------- #

def _values():
    return sorted(STRINGS.items())


@pytest.mark.parametrize("key,value", _values())
def test_no_shouting(key, value):
    """Rule 3. ``MODEL``, ``TOOLS``, ``VISION``, ``AGENTS`` are the tells."""
    shouted = [w for w in _ALLCAPS.findall(value) if w not in _BRANDS]
    assert not shouted, f"{key} shouts: {shouted}"


@pytest.mark.parametrize("key,value", _values())
def test_no_meta_string(key, value):
    """Rule 3. A person reads a sentence, not a status bar."""
    assert _META_SEPARATOR not in value, f"{key} is a meta string"


@pytest.mark.parametrize("key,value", _values())
def test_no_machine_name(key, value):
    """No flag, no function, no proposal id on a default screen."""
    assert not _FLAG_NAME.search(value), f"{key} names a flag"
    assert not _FUNCTION_CALL.search(value), f"{key} names a function"
    assert "§" not in value, f"{key} cites a proposal"


@pytest.mark.parametrize("key,value", _values())
def test_no_arrow_glued_to_the_text(key, value):
    """Rule 3. An arrow is a control's job, not a word's."""
    for arrow in ("->", "→", "»", "&rarr;"):
        assert arrow not in value, f"{key} glues an arrow to its text"


@pytest.mark.parametrize("key,value", _values())
def test_every_placeholder_is_a_named_field(key, value):
    """``t()`` formats with ``str.format``.

    A stray brace is a ``ValueError`` at render time, and a positional field is
    a string no caller can fill by name. Both are caught here instead.
    """
    for _, field, _, _ in string.Formatter().parse(value):
        if field is None:
            continue
        assert field.isidentifier(), f"{key} has a non-named field {field!r}"


@pytest.mark.parametrize("key,value", _values())
def test_the_key_and_the_value_are_well_formed(key, value):
    assert isinstance(value, str) and value.strip(), f"{key} is empty"
    assert re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", key), f"{key} is not a copy key"


# --- t() itself -------------------------------------------------------------- #

def test_a_missing_key_raises_under_test():
    with pytest.raises(KeyError):
        t("nothing.like.this")


def test_a_missing_key_renders_the_key_in_production(monkeypatch):
    """A console that 500s because a string is missing is worse than one that
    shows a key for an afternoon."""
    import webview.copy as copy_mod
    monkeypatch.setattr(copy_mod, "_strict", lambda: False)
    assert copy_mod.t("nothing.like.this") == "nothing.like.this"


def test_a_placeholder_is_filled():
    assert "7" in t("shell.waiting.many", count=7)


def test_the_wrong_placeholder_raises_under_test():
    with pytest.raises(KeyError):
        t("shell.waiting.many", nope=1)


def test_a_string_with_no_placeholder_is_returned_unchanged():
    assert t("shell.waiting.none") == STRINGS["shell.waiting.none"]


def test_missing_keys_reports_the_gap():
    from webview.copy import missing_keys
    assert missing_keys(["shell.waiting.none", "a.b"]) == ["a.b"]


# --- the ratchet's own teeth ------------------------------------------------ #
# A gate nobody has seen fail is a gate nobody has seen work. These feed the
# predicates the product's REAL historical tells.

@pytest.mark.parametrize("tell", ["MODEL", "TOOLS", "VISION", "AGENTS", "SKILLS"])
def test_the_shout_rule_rejects_the_products_own_tells(tell):
    assert [w for w in _ALLCAPS.findall(tell) if w not in _BRANDS], tell


def test_the_shout_rule_and_the_flag_rule_are_complementary():
    """``GOALS_ENABLED`` slips the shout rule — ``_`` is a word character, so
    there is no word boundary after ``GOALS``. The flag rule is what catches it,
    which is why both exist."""
    tell = "no goals — GOALS_ENABLED=off"
    assert not [w for w in _ALLCAPS.findall(tell) if w not in _BRANDS]
    assert _FLAG_NAME.search(tell)


def test_the_meta_rule_rejects_the_repl_session_line():
    assert _META_SEPARATOR in "session 1f9a28e6 · tools filesystem, task · /help"


def test_the_flag_rule_rejects_a_flag_name():
    assert _FLAG_NAME.search("no goals because GOALS_ENABLED is off")
    assert _FLAG_NAME.search("set WEBVIEW_READ_ONLY to stop")


def test_the_function_rule_rejects_a_named_reader():
    assert _FUNCTION_CALL.search("the same figure build_recap() reads")
    assert _FUNCTION_CALL.search("from GoalBoard.asks() for this tenant")


@pytest.mark.parametrize("prose", [
    "This list is incomplete.",
    "I could not read the spend approvals",
    "Work is coming in the next phase.",
])
def test_the_prose_detector_catches_prose(prose):
    assert _looks_like_prose(prose)


@pytest.mark.parametrize("machine", [
    "shell.waiting.none",
    "nav-item",
    "/static/app/app.css",
    "%H:%M UTC",
    "text/html",
])
def test_the_prose_detector_ignores_machine_strings(machine):
    assert not _looks_like_prose(machine)


def test_a_handler_that_writes_its_own_copy_is_caught():
    source = (
        'def page():\n'
        '    """A docstring is exempt, because nobody reads it on a screen."""\n'
        '    logger.info("a log line is exempt for the same reason")\n'
        '    return t("work.placeholder")\n'
        '\n'
        'def bad():\n'
        '    return "Work is coming in the next phase."\n'
    )
    assert prose_offenders(source) == ["Work is coming in the next phase."]


def test_a_template_that_writes_its_own_copy_is_caught():
    good = '{% extends "shell.html" %}{% block body %}<p>{{ t("work.title") }}</p>{% endblock %}'
    bad = '{% extends "shell.html" %}{% block body %}<p>Work is coming in the next phase.</p>{% endblock %}'
    assert template_prose(good) == []
    assert template_prose(bad) == ["Work is coming in the next phase."]
