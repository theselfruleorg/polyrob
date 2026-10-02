"""070 W0.3 — every word a console script reads is handed over by its page.

The console scripts carry no prose. A page renders its words from the copy
layer into ``data-<key>`` attributes on one hidden node (``#workpane-copy``,
``#money-copy``, …) and the script reads them back through ``node.dataset``
(``copyFrom(node)``). When the two drift — a script reads ``copy.x`` and no
template passes ``data-x`` — the screen draws an empty sentence or a blank
heading, and nothing fails. The Work pane's partial file reads did exactly that.

This ratchet ties the two halves together:

* ``_nodes_read_by(module)`` — the element ids a script looks up
  (``getElementById('<id>')``, ``byId('<id>')``, ``querySelector('#<id>')``),
  plus the ids of every script that imports it (a helper module is handed the
  ``copy`` object of the page that mounts it).
* ``_keys_read_by(module)`` — every static read ``copy.<k>``, ``copy?.<k>``,
  ``copy['<k>']``, ``copy["<k>"]``. Dynamic reads (``copy[labelKey]``) are
  listed in :data:`_DYNAMIC` as prefixes and checked the same way.
* ``_data_keys(node_id)`` — the ``data-<k>`` attributes on the element with that
  id in any ``webview/templates/*.html`` (a ``data-a-b`` attribute counts as
  ``aB``, the ``dataset`` rule).

A read with no attribute on any node its script reads fails, naming the module,
the key and the nodes. 070 E.3 extends this ratchet (plan §R3).
"""
from __future__ import annotations

import re
from pathlib import Path

_WEBVIEW = Path(__file__).resolve().parents[3] / "webview"
_APP = _WEBVIEW / "static" / "app"
_TEMPLATES = _WEBVIEW / "templates"

#: Reads built at run time, as key PREFIXES per module. A prefix must match at
#: least one ``data-*`` key on a node the module reads.
_DYNAMIC = {
    "chats.js": {"status_", "creator_"},
    "agent.js": {"memory_backend_"},
    "workpane.js": {"tier_", "verdict_"},
}

#: Reads that are known to have no attribute yet, each with the step that
#: fixes it. Must be empty at the end of wave 0.
_KNOWN_GAPS: dict = {}

_ID_LOOKUP = re.compile(
    r"""(?:getElementById|byId)\(\s*['"]([\w-]+)['"]\s*\)"""
    r"""|querySelector\(\s*['"]#([\w-]+)['"]\s*\)""")
_IMPORT = re.compile(r"""from\s+['"]\./([\w-]+\.js)['"]""")
_STATIC_READ = re.compile(
    r"""\bcopy(?:\?\.|\.)([A-Za-z_]\w*)\b(?!\s*\()"""
    r"""|\bcopy(?:\?\.)?\[\s*['"]([A-Za-z_]\w*)['"]\s*\]""")
_TAG = re.compile(r"<[a-zA-Z][^<>]*?>", re.S)
_ID_ATTR = re.compile(r"""\bid\s*=\s*['"]([\w-]+)['"]""")
_DATA_ATTR = re.compile(r"""\bdata-([a-z0-9_-]+)\s*=""")


def _modules() -> dict:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(_APP.glob("*.js"))}


def _strip_js_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?m)(^|[^:'\"\\])//.*$", r"\1", js)


def _own_ids(js: str) -> set:
    return {a or b for a, b in _ID_LOOKUP.findall(_strip_js_comments(js))}


def _nodes_read_by(name: str, modules: dict) -> set:
    """Ids the module reads, plus those of every module that imports it."""
    importers = {m: set(_IMPORT.findall(js)) for m, js in modules.items()}
    out, todo, seen = set(), [name], set()
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        out |= _own_ids(modules.get(cur, ""))
        todo.extend(m for m, deps in importers.items() if cur in deps)
    return out


def _keys_read_by(js: str) -> set:
    return {a or b for a, b in _STATIC_READ.findall(_strip_js_comments(js))}


def _camel(attr: str) -> str:
    return re.sub(r"-([a-z0-9])", lambda m: m.group(1).upper(), attr)


def _template_nodes() -> dict:
    """``{id: {dataset keys}}`` across every template (comments stripped)."""
    out: dict = {}
    for path in sorted(_TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"\{#.*?#\}", " ", text, flags=re.S)
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
        text = re.sub(r"\{\{.*?\}\}", '""', text, flags=re.S)
        text = re.sub(r"\{%.*?%\}", " ", text, flags=re.S)
        for tag in _TAG.findall(text):
            ident = _ID_ATTR.search(tag)
            if not ident:
                continue
            keys = {_camel(a) for a in _DATA_ATTR.findall(tag)}
            out.setdefault(ident.group(1), set()).update(keys)
    return out


def _data_keys(node_ids, nodes: dict) -> set:
    return set().union(*(nodes.get(i, set()) for i in node_ids)) if node_ids else set()


def _gaps() -> list:
    modules = _modules()
    nodes = _template_nodes()
    out = []
    for name, js in modules.items():
        keys = _keys_read_by(js)
        if not keys and name not in _DYNAMIC:
            continue
        read = _nodes_read_by(name, modules)
        have = _data_keys(read, nodes)
        for key in sorted(keys):
            if key not in have and key not in _KNOWN_GAPS.get(name, set()):
                out.append((name, key, sorted(read)))
        for prefix in sorted(_DYNAMIC.get(name, ())):
            if not any(k.startswith(prefix) for k in have):
                out.append((name, prefix + "*", sorted(read)))
    return out


def test_every_static_copy_read_has_a_data_key():
    gaps = _gaps()
    assert not gaps, "a script reads a word its page never hands over:\n" + "\n".join(
        f"  {mod}: copy.{key} — no data-{key} on any of {ids}" for mod, key, ids in gaps)


def test_the_scan_is_not_vacuous():
    modules = _modules()
    reading = {m for m, js in modules.items() if _keys_read_by(js)}
    reads = sum(len(_keys_read_by(js)) for js in modules.values())
    assert len(reading) >= 8, reading
    assert reads >= 150, reads
    assert "workpane-copy" in _nodes_read_by("workpane.js", modules)


def test_a_missing_attribute_is_caught():
    """The ratchet's own teeth: a read with no attribute is a gap."""
    nodes = {"x-copy": {"present"}}
    assert "absent" not in _data_keys({"x-copy"}, nodes)
    assert _keys_read_by("const a = copy.absent; const b = copy?.other; "
                         "const c = copy['third']; copy.fn(1);") == {"absent", "other", "third"}
    assert _camel("read-only") == "readOnly"


def test_the_known_gaps_are_empty():
    """070 E.3 closed the last one; a new read ships with its attribute."""
    assert not _KNOWN_GAPS, "every known gap must be closed by the end of wave 0"


def test_the_read_only_flag_resolves_through_the_dataset_rule():
    """``data-read-only`` is ``dataset.readOnly`` — the camel-case rule the scan
    applies, checked on a real template node."""
    nodes = _template_nodes()
    assert any("readOnly" in keys for keys in nodes.values())
