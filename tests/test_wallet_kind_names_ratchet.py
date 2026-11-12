"""Ratchet (proposal 111 D5): core's wallet model names a STANDARD, never the product.

The account kinds are ``tokenbound_v3`` and ``erc6551p`` (``core/wallet/account_kinds.py``).
"polyrob" is also the PLATFORM's name (its binary, its ``/etc/polyrob/`` config directory, its
``POLYROB_*`` env flags), so the rule is defined narrowly:

1. **Identifiers.** No identifier anywhere under ``core/wallet`` contains ``polyrob`` (any
   case) — a class, a function, a constant, an attribute (``is_polyrob``, ``KIND_POLYROB``,
   ``POLYROB_ACCOUNT_ERRORS`` …). Exception: an import of the ``polyrob`` package itself.
2. **The kind code** (:data:`KIND_FILES`): every string literal and comment that mentions
   ``polyrob`` must be EITHER a platform name (:data:`PLATFORM`) OR — in a docstring or a
   comment block — a citation of the first deployment (the block also says ``polyrob-desk`` or
   ``first deployment``). Anything else (view text, an error message, a kind label) fails.
"""
import ast
import io
import os
import re
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WALLET = os.path.join(ROOT, "core", "wallet")

#: The files that hold the account-kind model and every branch on it.
KIND_FILES = ("account_kinds.py", "collection_registry.py", "collection_errors.py",
              "custom_errors.py", "collection_trust.py", "erc6551.py", "tx_guard_account.py",
              "nft_holdings.py", "simulation.py")

#: The platform's own names (not the product): its config dir, env file, signer, CLI and flags.
PLATFORM = re.compile(r"/etc/polyrob/|polyrob\.env|polyrob-signer|``polyrob [a-z]|"
                      r"\bPOLYROB_[A-Z_]+\b", re.I)
CITATION = re.compile(r"polyrob-desk|first deployment", re.I)
_WORD = re.compile(r"polyrob", re.I)


def _py_files():
    for dirpath, _dirs, files in os.walk(WALLET):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _rest(text: str) -> str:
    """*text* with every platform name removed (what is left must not say polyrob)."""
    return PLATFORM.sub("", text)


def test_no_identifier_under_core_wallet_names_the_product():
    bad = []
    for path in _py_files():
        src = open(path, encoding="utf-8").read()
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME and _WORD.search(tok.string):
                line = tok.line.strip()
                if line.startswith(("import polyrob", "from polyrob")):
                    continue
                bad.append(f"{os.path.relpath(path, ROOT)}:{tok.start[0]}: {tok.string}")
    assert not bad, "a product name in core's wallet model (name the standard):\n" + "\n".join(bad)


def _docstring_lines(tree) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                out.add(body[0].lineno)
    return out


def _comment_blocks(tokens):
    """Contiguous comment lines, as ``(first line, text)``."""
    blocks, cur, last = [], [], None
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        if last is not None and tok.start[0] == last + 1 and cur:
            cur[1] += "\n" + tok.string
        else:
            if cur:
                blocks.append(tuple(cur))
            cur = [tok.start[0], tok.string]
        last = tok.start[0]
    if cur:
        blocks.append(tuple(cur))
    return blocks


def test_the_kind_code_says_polyrob_only_as_the_platform_or_a_citation():
    bad = []
    for name in KIND_FILES:
        path = os.path.join(WALLET, name)
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        docs = _docstring_lines(tree)
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        for tok in tokens:
            if tok.type != tokenize.STRING or not _WORD.search(_rest(tok.string)):
                continue
            if tok.start[0] in docs and CITATION.search(tok.string):
                continue
            bad.append(f"{name}:{tok.start[0]}: {tok.string[:70]!r}")
        for line, text in _comment_blocks(tokens):
            if _WORD.search(_rest(text)) and not CITATION.search(text):
                bad.append(f"{name}:{line}: {text[:70]!r}")
    assert not bad, ("the kind code names the product outside a citation of the first "
                     "deployment:\n" + "\n".join(bad))


def test_the_ratchet_bites():
    """The rule catches the names it was written for (so a broken scan cannot pass)."""
    assert _WORD.search(_rest("the PolyrobAccount refused it"))
    assert not _WORD.search(_rest("/etc/polyrob/agent_nft_collections.json"))
    assert not _WORD.search(_rest("POLYROB_DATA_DIR"))
    blocks = _comment_blocks(tokenize.generate_tokens(io.StringIO(
        "# a PolyrobAccount\n# and more\nx = 1\n# cites polyrob-desk\n").readline))
    assert [b[0] for b in blocks] == [1, 4]
