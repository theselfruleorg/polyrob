"""Browser-tool env flags — ONE accessor per flag so semantics can't fork.

`allow_private_urls()` deliberately does NOT use ``core.env.bool_env``: that is a
falsey-set parser (any unrecognized value -> True), which for this default-OFF
security gate would mean a typo like ``BROWSER_ALLOW_PRIVATE_URLS=maybe`` silently
DISABLES the SSRF guard. A security bypass must be a strict truthy-allowlist:
only an explicit 1/true/yes/on opens it; everything else keeps the guard active.
Both historical call sites (browser.py initial-URL guard, context.py redirect-hop
route guard) used exactly this predicate — this just makes it single-sourced.
"""

def allow_private_urls() -> bool:
    """True only when BROWSER_ALLOW_PRIVATE_URLS is explicitly on (local/dev).

    Read at call time (not import time) so tests and runtime toggles see the
    current environment; parsed by the repo's ONE flag parser (core.env).
    """
    from core.env import bool_env
    return bool_env('BROWSER_ALLOW_PRIVATE_URLS', False)
