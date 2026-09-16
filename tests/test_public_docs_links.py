"""Every relative link in the PUBLIC docs set must resolve.

The published guide is the only POLYROB documentation a reader outside this
tree can see, so a link that 404s there is not a cosmetic defect — it is the
reader's dead end. Two failure shapes are guarded:

* the target FILE does not exist (a page renamed or a path typed from memory);
* the target file exists but the ``#anchor`` names no heading in it. This is the
  quieter one: GitHub renders the link, the reader clicks it, and lands at the
  top of a long page with no idea what they were meant to read. It is exactly
  how ``api.md`` came to point at ``#mcp-server-management`` months after that
  heading became ``## MCP server management (outbound)``.

Anchors are compared using GitHub's own slug rule (lowercase, drop everything
that is not alphanumeric / space / ``-`` / ``_``, then every remaining space
becomes one ``-``) — note that runs of spaces are NOT collapsed, which is why
``### Crypto / wallet / payment features`` is ``#crypto--wallet--payment-features``.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The files published to the public repository / the docs portal.
PUBLIC_DOCS = ["README.md", "docs/comparison.md", "docs/examples.md",
               "docs/CONFIGURATION.md"]

_LINK_RE = re.compile(r"\]\(\s*([^)\s]+?)\s*\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")


def public_doc_paths():
    paths = [REPO_ROOT / rel for rel in PUBLIC_DOCS]
    paths += sorted((REPO_ROOT / "docs/guide").rglob("*.md"))
    return [p for p in paths if p.exists()]


def _github_slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^0-9a-z _\-]", "", text)
    return text.replace(" ", "-")


def _heading_slugs(path: Path) -> set:
    slugs, in_fence = set(), False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            slugs.add(_github_slug(match.group(1)))
    return slugs


def test_public_doc_relative_links_resolve():
    broken = []
    slug_cache = {}
    for doc in public_doc_paths():
        rel_doc = doc.relative_to(REPO_ROOT)
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            for target in _LINK_RE.findall(line):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path_part, _, anchor = target.partition("#")
                resolved = (doc.parent / path_part).resolve() if path_part else doc
                if not resolved.exists():
                    broken.append(f"{rel_doc}:{lineno} -> {target} (no such file)")
                    continue
                if not anchor or resolved.suffix != ".md":
                    continue
                if resolved not in slug_cache:
                    slug_cache[resolved] = _heading_slugs(resolved)
                if anchor.lower() not in slug_cache[resolved]:
                    broken.append(f"{rel_doc}:{lineno} -> {target} (no such heading)")
    assert not broken, (
        "Broken relative links in the public docs — a reader following one of "
        "these lands nowhere:\n  " + "\n  ".join(broken))
