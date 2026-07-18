"""Rate-limiter fork ratchet (F-1, 2026-07-17).

Six rate-limiter forks (three algorithms) were consolidated onto the canonical
primitives in ``core/rate_limit.py`` (SlidingWindowLimiter / TokenBucket /
FixedWindowCounter). This ratchet keeps new forks from appearing: any NEW
limiter-shaped definition (a ``*RateLimit*``/``*TokenBucket*``/``*Throttle*``
class or a ``check_rate_limit``/``check_event_rate_limit`` function) outside the
canonical module fails the scan — configure a core primitive instead, or add a
thin wrapper that delegates to one and allowlist it here with a rationale.

The allowlist may only SHRINK. Current entries are all sanctioned:
- wrappers/shims that DELEGATE to core.rate_limit (api middleware, MCP shim,
  RateLimitManager, webview delegator functions);
- non-limiter types the name pattern also catches (RateLimitInfo model,
  RateLimitError exceptions, XRateLimited marker);
- the ONE deliberate non-consolidation: TelegramRateLimiter, which is not a
  request-budget limiter — it only replays penalties Telegram already issued
  via RetryAfter (grammY philosophy: never pre-throttle).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ["agents", "api", "cli", "core", "cron", "modules", "surfaces",
             "tools", "utils", "webview"]
CANONICAL = "core/rate_limit.py"

_DEF_RE = re.compile(
    r"^\s*(?:class\s+(\w*(?:Rate_?Limit(?:er)?|Token_?Bucket|Throttle)\w*)"
    r"|(?:async\s+)?def\s+(check_rate_limit|check_event_rate_limit))\b",
    re.MULTILINE | re.IGNORECASE,
)

ALLOWLISTED_RATE_LIMITER_DEFS = frozenset({
    ('api/middleware.py', 'RateLimiter'),            # wrapper: TokenBucket + 2x FixedWindowCounter
    ('api/middleware.py', 'check_rate_limit'),       # method of that wrapper
    ('api/middleware.py', 'RateLimitMiddleware'),    # HTTP middleware consuming the wrapper
    ('api/models.py', 'RateLimitInfo'),              # response model, not a limiter
    ('core/exceptions.py', 'LLMRateLimitError'),     # exception, not a limiter
    ('core/exceptions.py', 'RateLimitError'),        # exception, not a limiter
    ('surfaces/telegram/rate_limit.py', 'TelegramRateLimiter'),  # penalty tracker (documented exception)
    ('surfaces/x/client.py', 'XRateLimited'),        # marker/exception, not a limiter
    ('tools/mcp/rate_limit.py', 'MCPExecRateLimiter'),  # back-compat subclass shim
    ('utils/rate_limit_manager.py', 'RateLimitManager'),  # component wrapper over SlidingWindowLimiter
    ('utils/rate_limit_manager.py', 'check_rate_limit'),  # method of that wrapper
    ('webview/server.py', 'check_rate_limit'),       # thin delegator to SlidingWindowLimiter
    ('webview/server.py', 'check_event_rate_limit'), # thin delegator to SlidingWindowLimiter
})


def _findings():
    found = set()
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(ROOT).as_posix()
            if rel == CANONICAL:
                continue
            for m in _DEF_RE.finditer(py.read_text(errors="ignore")):
                found.add((rel, m.group(1) or m.group(2)))
    return found


def test_no_new_rate_limiter_forks():
    new = _findings() - ALLOWLISTED_RATE_LIMITER_DEFS
    assert not new, (
        "NEW rate-limiter-shaped definition(s) outside core/rate_limit.py:\n  "
        + "\n  ".join(f"{f}: {n}" for f, n in sorted(new))
        + "\nConfigure a core.rate_limit primitive instead (SlidingWindowLimiter/"
        "TokenBucket/FixedWindowCounter), or delegate to one and allowlist the "
        "wrapper here with a rationale."
    )


def test_allowlist_entries_still_exist():
    gone = ALLOWLISTED_RATE_LIMITER_DEFS - _findings()
    assert not gone, (
        "Stale allowlist row(s) — the definition no longer exists; delete the "
        "row(s) so the ratchet tightens:\n  "
        + "\n  ".join(f"{f}: {n}" for f, n in sorted(gone))
    )
