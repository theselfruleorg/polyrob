"""Public release prose must not expose local-tree or website-operations details."""
from pathlib import Path
import re

from tests.test_public_docs_links import public_doc_paths


ROOT = Path(__file__).resolve().parents[1]


def test_public_prose_does_not_name_the_private_development_tree():
    paths = [*public_doc_paths(), ROOT / "CHANGELOG.md"]
    private_tree_name = "rob" + "_dev"
    hits = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if re.search(rf"\b{re.escape(private_tree_name)}\b", text, flags=re.IGNORECASE):
            hits.append(path.relative_to(ROOT).as_posix())
    assert not hits, f"private development-tree name appears in public prose: {hits}"


def test_changelog_does_not_describe_public_website_operations():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    patterns = (
        r"\bpublic website\b",
        r"\bmarketing site\b",
        r"\bwebsite deploy(?:ment)?\b",
        r"\bportal workflow\b",
        r"\bweb/portal\b",
        r"\bCloudflare Pages\b",
        r"\bpolyrob\.dev\b",
    )
    hits = [pattern for pattern in patterns if re.search(pattern, text, re.IGNORECASE)]
    assert not hits, f"CHANGELOG describes public website operations: {hits}"
