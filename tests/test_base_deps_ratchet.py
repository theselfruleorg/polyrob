"""058 T1.1 — the base-dependency ratchet.

``pip install polyrob`` installs exactly the ``[project].dependencies`` list.
Every name here is weight a user carries before they have chosen a single
provider, surface or tool. This freezes the list so a reduction is visible as
shrinkage and a new heavy dependency needs a deliberate edit of BOTH files —
mirroring ``test_file_size_ratchet.py``, with two guards: no new member, and
no stale member (a removal that forgets this file is caught too).

⚠️ Ships publicly (``tests/`` and ``pyproject.toml`` are in the public
manifest). It reads ``pyproject.toml`` and NOTHING under ``scripts/`` or
``deployment/`` — neither ships, and a shipped test asserting over them
breaks public CI (the 0.13.0 landmine).
"""
import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

#: Today's base, frozen. A name leaves when 058 T1.x moves it to an extra (edit
#: this set in the SAME commit); a name enters only with a written reason.
BASE = frozenset({
    "aiofiles", "aiohttp", "aiosqlite", "certifi", "click", "colorama",
    "cryptography", "h2", "httplib2", "markdown", "markdownify", "openai",
    "packaging", "pillow", "prompt_toolkit", "pyasn1", "pydantic",
    "pydantic-settings", "pyjwt", "python-dotenv", "pyyaml", "requests", "rich",
    "tiktoken",
    "multidict",  # Already required by aiohttp; explicit security floor for every install.
    "httpx",  # Was transitive via openai until openai 3.x moved to httpx2; core and tools import it.
})


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _declared_base() -> set[str]:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text())
    return {_norm(_NAME_RE.match(s.strip()).group(0)) for s in data["project"]["dependencies"]}


def test_no_new_base_dependency():
    new = _declared_base() - {_norm(n) for n in BASE}
    assert not new, (
        f"new base dependency: {sorted(new)}. Every base dep is installed for every "
        f"user before they choose a provider or a tool. Put it behind the extra "
        f"that owns it (+ a core/lazy_deps row) or add it to BASE here with a reason."
    )


def test_no_stale_base_member():
    stale = {_norm(n) for n in BASE} - _declared_base()
    assert not stale, (
        f"BASE lists a dependency pyproject no longer carries: {sorted(stale)}. "
        f"Remove it from this ratchet in the same commit that moved it."
    )
