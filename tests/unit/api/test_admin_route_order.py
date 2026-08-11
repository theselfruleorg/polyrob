"""Regression: /admin/users/search must resolve to search_users, not get_user.

Starlette matches routes in registration order, so a static segment
(``/users/search``) registered AFTER the path-param route (``/users/{user_id}``)
is unreachable — every request lands in ``get_user(user_id="search")``.
That exact shadowing bug shipped once (search registered at the bottom of
api/admin_endpoints.py); these tests pin the fix.
"""
from starlette.routing import Match

from api.admin_endpoints import router


def _get_route_paths():
    """Paths of GET routes in registration order."""
    return [
        r.path
        for r in router.routes
        if "GET" in (getattr(r, "methods", None) or set())
    ]


def test_users_search_registered_before_user_id_route():
    paths = _get_route_paths()
    assert "/admin/users/search" in paths, "search route missing"
    assert "/admin/users/{user_id}" in paths, "get_user route missing"
    assert paths.index("/admin/users/search") < paths.index("/admin/users/{user_id}"), (
        "/admin/users/search registers after /admin/users/{user_id} — "
        "the search endpoint is shadowed and unreachable"
    )


def test_users_search_resolves_to_search_users_not_get_user():
    """First full route match for GET /admin/users/search is search_users."""
    scope = {"type": "http", "method": "GET", "path": "/admin/users/search"}
    for route in router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            assert route.endpoint.__name__ == "search_users", (
                f"GET /admin/users/search resolved to {route.endpoint.__name__} "
                "(route shadowing regression)"
            )
            return
    raise AssertionError("no route matched GET /admin/users/search")
