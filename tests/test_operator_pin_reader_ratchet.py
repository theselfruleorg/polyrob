"""The operator's provider/model pin has ONE reader: ``core/runtime_config.py``.

``CHAT_PROVIDER``/``DEFAULT_PROVIDER``/``CHAT_MODEL``/``DEFAULT_MODEL`` were read by hand
in five modules, each with its own precedence, strip and alias handling, and the
model half reached chat only: live prod 2026-10-12 ran every cron/goal/planner run on
the registry default (``z-ai/glm-5.2``) while chat served the pinned DeepSeek model.
Read the pin through ``operator_provider_pin``/``operator_model_pin``/
``operator_model_for``, and a provider's default model through ``get_default_model``
(which applies the pin). ``CHAT_*`` is retired (a refusing alias of ``DEFAULT_*``);
the pattern still forbids a hand-written read of it. Writers (``polyrob init``, ``polyrob config set``) are fine —
this ratchet matches READS only.
"""
import pathlib
import re

from tests.support.repo_index import repo_index

_OWNER = pathlib.Path("core/runtime_config.py")
_SKIP = {"tests", ".git", "node_modules", ".venv", "venv", "__pycache__",
         "deployment", "docs", "scripts", "build", "dist", "ops"}
_VARS = r"(?:CHAT|DEFAULT)_(?:PROVIDER|MODEL)"
_READ = re.compile(
    rf"""(?:\.get|getenv)\(\s*["']{_VARS}["']|environ\[\s*["']{_VARS}["']\s*\]""")


def _offenders():
    repo = pathlib.Path(__file__).resolve().parents[1]
    hits = {}
    for p in repo.rglob("*.py"):
        rel = p.relative_to(repo)
        if rel == _OWNER or _SKIP.intersection(rel.parts):
            continue
        try:
            n = len(_READ.findall(repo_index().text(p, errors="ignore")))
        except OSError:
            continue
        if n:
            hits[str(rel)] = n
    return hits


def test_only_runtime_config_reads_the_operator_pin():
    assert not _offenders(), (
        "read the operator pin through core.runtime_config "
        "(operator_provider_pin / operator_model_pin / operator_model_for): "
        f"{_offenders()}")


def test_the_pattern_matches_each_read_shape():
    """Tightness: the shapes the five hand-written readers used still match."""
    for src in ('os.environ.get("CHAT_PROVIDER")', "os.getenv('DEFAULT_PROVIDER')",
                'env.get("CHAT_MODEL")', 'os.environ["DEFAULT_MODEL"]'):
        assert _READ.search(src), src
