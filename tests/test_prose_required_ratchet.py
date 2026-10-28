"""A parameter the description calls REQUIRED must be required in the schema the model sees.

Prod 2026-10-26: ``wallet_holdings(chain)`` was ``Optional`` in the schema and "REQUIRED for a 0x
address" only in its description. Given 191 tool definitions, the model sent no chain on 165 of 167
calls and the run burned its step budget. A conditional requirement written in prose is invisible to a
model; the schema is what it fills in.

A field that says REQUIRED but is not required must either list ``chain``-style requirement through
``json_schema_extra`` on its model (``tools.defi.data_params.chain_required_in_schema``) or be listed in
``REVIEWED`` with the reason it is safe. A NEW hit fails this test: extend the schema, do not extend the list.
"""
import ast
import glob
import re

ROOTS = ("tools", "modules", "core")

#: (file, class, field) -> why prose-only is acceptable. Reviewed 2026-10-26.
REVIEWED = {
    ("tools/x402/service.py", "FetchParams", "request_id"):
        "description wording only; the schema field is not the requirement",
    ("tools/controller/action_registration.py", "PreferencesAction", "key"):
        "required for get/explain/set, absent for list: operation-dependent; the handler returns a clear error",
    ("tools/controller/views.py", "MessageTargetAction", "media_paths"):
        "file delivery argument; the handler refuses a missing path with the remedy",
    ("tools/defi/data_params.py", "LpQuoteParams", "initial_price"):
        "needed only when the pool does not exist yet; the quote verb says so",
}


def _hits():
    out = []
    for root in ROOTS:
        for path in glob.glob(f"{root}/**/*.py", recursive=True):
            try:
                src = open(path, errors="replace").read()
                tree = ast.parse(src)
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
                if "BaseModel" not in bases:
                    continue
                body_src = ast.get_source_segment(src, node) or ""
                declared = "json_schema_extra" in body_src
                for st in node.body:
                    if not (isinstance(st, ast.AnnAssign) and isinstance(st.value, ast.Call)
                            and getattr(st.value.func, "id", "") == "Field"):
                        continue
                    first = st.value.args[0] if st.value.args else None
                    if isinstance(first, ast.Constant) and first.value is Ellipsis:
                        continue  # required in the schema
                    text = ""
                    for kw in st.value.keywords:
                        if kw.arg == "description":
                            text = ast.get_source_segment(src, kw.value) or ""
                    if re.search(r"\bREQUIRED\b", text) and not declared:
                        out.append((path.replace("\\", "/"), node.name, st.target.id))
    return out


def test_prose_required_fields_are_required_in_the_schema():
    new = [h for h in _hits() if h not in REVIEWED]
    assert not new, (
        "These fields say REQUIRED in their description but are optional in the schema the model "
        f"sees; add json_schema_extra (see data_params.chain_required_in_schema): {new}")
