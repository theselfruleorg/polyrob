from pathlib import Path


PUBLIC_DOCS = [
    Path("README.md"),
    Path("docs/guide/getting-started.md"),
    Path("docs/guide/cli.md"),
    Path("docs/guide/configuration.md"),
    Path("docs/guide/architecture.md"),
]

STALE_PATTERNS = [
    "`~/.rob/cli.json`",
    "`~/.rob/sessions/`",
    "`~/.rob/memory.db`",
    "`~/.rob/`",
    "config/.env.development",
]


def test_public_docs_do_not_advertise_stale_runtime_paths():
    offenders = []
    for path in PUBLIC_DOCS:
        text = path.read_text()
        for pattern in STALE_PATTERNS:
            if pattern in text:
                offenders.append(f"{path}: {pattern}")

    assert not offenders
