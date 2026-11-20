"""The deployment-environment names are read in ONE place: ``core/env.py``.

The repo has five names for one fact (``ENVIRONMENT``, ``POLYROB_ENV``,
``CONFIG_ENV``, ``ENV`` and the ``PRODUCTION`` bool). Each module that read
"its" name with "its" default disagreed with the next: a box with only
``PRODUCTION=true`` got a generated MCP dev key, and a box with
``ENVIRONMENT=development`` skipped the access-tier gate (proposal 125 S34).

Refusals ask ``core.env.is_production_env()``; relaxations ask
``core.env.is_development_env()`` (an EXPLICIT development name, never "not
production"). No other module may ``getenv``/``environ.get``/``environ[...]``
one of the names. ``polyrob/app/bootstrap.py`` resolves the env-file name and
stays allowlisted.
"""
import pathlib
import re
import subprocess

_NAMES = r"(?:ENVIRONMENT|POLYROB_ENV|CONFIG_ENV|ENV|PRODUCTION)"
_PAT = re.compile(
    r"""(?:getenv\s*\(|environ\.get\s*\(|environ\s*\[)\s*(?:"|')""" + _NAMES + r"""(?:"|')""")
_SOURCE_DIRS = ("core", "polyrob", "modules", "tools", "agents", "cli", "webview",
                "surfaces", "cron", "api", "utils", "packs", "migrations", "main.py")
_ALLOW = {"core/env.py", "polyrob/app/bootstrap.py"}


def _sites():
    repo = pathlib.Path(__file__).resolve().parents[1]
    out = subprocess.run(["git", "ls-files", "--", *_SOURCE_DIRS], cwd=repo,
                         capture_output=True, text=True, timeout=30).stdout
    sites = []
    for rel in out.splitlines():
        path = repo / rel
        if (not rel.endswith(".py") or "/tests/" in rel or rel in _ALLOW
                or not path.is_file()):
            continue
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if _PAT.search(line):
                sites.append(f"{rel}:{n}")
    return sites


def test_environment_names_are_read_only_in_core_env():
    sites = _sites()
    assert not sites, (
        "a deployment-environment name is read outside core/env.py; use "
        "core.env.is_production_env() for a refusal or core.env.is_development_env() "
        f"for a relaxation: {sites}")


def test_the_pattern_sees_each_read_shape():
    for line in ('os.getenv("ENVIRONMENT", "x")', "os.environ.get('PRODUCTION')",
                 'os.environ["CONFIG_ENV"]', "getenv('ENV')", "os.getenv('POLYROB_ENV')"):
        assert _PAT.search(line), line
    assert not _PAT.search('os.getenv("ENVIRONMENT_NAME")')
    assert not _PAT.search('os.getenv("POLYROB_ENVELOPE")')
