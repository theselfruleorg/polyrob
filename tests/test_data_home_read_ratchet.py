"""A-16: a production read of ``POLYROB_DATA_DIR`` goes through the data-home seam
(``core.runtime_paths``: ``pinned_data_home`` / ``effective_data_home`` /
``data_dir_or_home``), never a raw ``os.environ`` / ``os.getenv`` read.

Two readers that each read the env and then apply their OWN fallback disagree about
the home as soon as the env is unset (the console avatar read a second home). The
rows below are the reads that remain; the table may only shrink. A writer
(``os.environ["POLYROB_DATA_DIR"] = …``) is not a read and is not counted.
"""
import re

from tests.support.repo_index import REPO_ROOT, repo_index

#: The seam itself.
SEAM = "core/runtime_paths.py"

_TREES = ("core", "agents", "modules", "tools", "cron", "api", "cli", "surfaces",
          "webview", "polyrob", "utils", "migrations", "packs")

_READ = re.compile(
    r"""(?:getenv|environ\.get|env\.get|\)\.get)\(\s*["']POLYROB_DATA_DIR["']"""
    r"""|environ\[\s*["']POLYROB_DATA_DIR["']\s*\](?!\s*=[^=])""")

#: file -> raw reads it may still hold (shrink-only). Each is a reason a reader
#: needs the RAW value (a doctor/diagnostic that reports the env as written, a
#: mapping handed in, a process that has no core config yet), not a home.
ALLOWED = {
    "cli/commands/apps_install.py": 1,  # another service user's env file (a mapping)
    "cli/commands/doctor.py": 1,
    "cli/commands/doctor_env_copies.py": 1,
    "core/agent_activity.py": 1,
    "core/health_slot.py": 1,
    "core/pairing.py": 1,
    "core/profiles.py": 1,
    "core/status_types.py": 1,
    "core/wallet/derivation.py": 1,
    "packs/x/polyrob_x/commands/x_account.py": 1,
    "webview/posture_guard.py": 1,
}


def _counts():
    out = {}
    for tree in _TREES:
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            rel = py.relative_to(REPO_ROOT).as_posix()
            if "__pycache__" in rel or rel == SEAM:
                continue
            n = len(_READ.findall(repo_index().text(py)))
            if n:
                out[rel] = n
    return out


def test_no_new_raw_data_home_read():
    grown = {f: n for f, n in _counts().items() if n > ALLOWED.get(f, 0)}
    assert not grown, (
        "A raw POLYROB_DATA_DIR read outside the seam: read the home through "
        "core.runtime_paths (pinned_data_home / effective_data_home / data_dir_or_home). "
        f"{grown}")


def test_allowed_rows_track_actual():
    counts = _counts()
    stale = {f: (c, counts.get(f, 0)) for f, c in ALLOWED.items() if counts.get(f, 0) < c}
    assert not stale, f"A row shrank — lower it (shrink-only): {stale}"
