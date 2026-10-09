"""Same-origin mutation checks shared by API and console cookie authentication."""
from urllib.parse import urlsplit


def origin_of(value):
    try:
        parsed = urlsplit((value or '').strip())
        scheme = parsed.scheme.lower()
        if scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None:
            return None
        port = parsed.port if parsed.port is not None else (443 if scheme == 'https' else 80)
        return scheme, parsed.hostname.lower(), port
    except (TypeError, ValueError):
        return None


def mutation_origin_refusal(request):
    """Missing origins are allowed only for clients without ambient cookies."""
    if request.method.upper() not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    stated = request.headers.get('origin') or request.headers.get('referer')
    if not stated and not request.headers.get('cookie'):
        return None
    host = request.headers.get('host')
    expected = origin_of(f'{request.url.scheme}://{host}') if host else origin_of(str(request.url))
    actual = origin_of(stated)
    if expected is None or actual is None or actual != expected:
        return ('Cross-origin request refused: a cookie-bearing request must state its Origin'
                if not stated else 'Cross-origin request refused: the Origin does not match this host')
    return None
