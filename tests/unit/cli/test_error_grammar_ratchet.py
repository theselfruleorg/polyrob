"""CLI error-grammar ratchet (proposal 030 WS-C5 / D5, 2026-08-27).

The CLI speaks two error grammars: the legacy ad-hoc ``[polyrob] ERROR:``
echo + ``sys.exit(1)`` pattern, and click's ``ClickException`` /
``UsageError`` taxonomy (consistent formatting, correct exit codes, stderr).
The house style is ClickException; the legacy sites are pinned here per file
and may only SHRINK (mirrors ``tests/test_rate_limiter_ratchet.py``).

Replacing a site::

    click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"...: {e}")
    raise SystemExit(1)

becomes::

    raise click.ClickException(f"...: {e}")

When you remove a site, tighten (or delete) that file's pin in the same
change so the ratchet keeps closing.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NEEDLE = "[polyrob] ERROR"

#: Frozen per-file budget of legacy error-grammar sites (counted 2026-08-27).
#: Shrink-only: a file may never EXCEED its pin, and an unlisted file has a
#: budget of zero.
PINNED_ERROR_SITES = {
    "cli/commands/_errors.py": 5,
    "cli/commands/_surface_runner.py": 3,
    "cli/commands/chat.py": 5,
    "cli/commands/email.py": 1,
    "cli/commands/goals.py": 20,
    "cli/commands/run.py": 5,
    "cli/commands/skills.py": 4,
    "cli/commands/todos.py": 2,
    "cli/commands/tools.py": 1,
    "cli/commands/whatsapp.py": 1,
    "cli/commands/x_account.py": 2,
}


def _counts():
    found = {}
    for py in (ROOT / "cli").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        n = py.read_text(errors="ignore").count(NEEDLE)
        if n:
            found[py.relative_to(ROOT).as_posix()] = n
    return found


def test_no_new_polyrob_error_sites():
    grown = {
        rel: (PINNED_ERROR_SITES.get(rel, 0), n)
        for rel, n in _counts().items()
        if n > PINNED_ERROR_SITES.get(rel, 0)
    }
    assert not grown, (
        "NEW '[polyrob] ERROR' site(s) — this legacy grammar is shrink-only:\n  "
        + "\n  ".join(f"{rel}: pinned {p}, found {n}"
                      for rel, (p, n) in sorted(grown.items()))
        + "\nUse the house error grammar instead: raise "
        "click.ClickException(msg) (or click.UsageError for bad invocations) "
        "— it formats consistently, writes to stderr, and exits non-zero."
    )


def test_pinned_error_counts_are_not_stale():
    counts = _counts()
    stale = {
        rel: (pinned, counts.get(rel, 0))
        for rel, pinned in PINNED_ERROR_SITES.items()
        if counts.get(rel, 0) < pinned
    }
    assert not stale, (
        "Stale pin(s) — the file now has fewer '[polyrob] ERROR' sites; "
        "tighten (or delete) the row(s) so the ratchet keeps closing:\n  "
        + "\n  ".join(f"{rel}: pinned {p}, found {n}"
                      for rel, (p, n) in sorted(stale.items()))
    )
