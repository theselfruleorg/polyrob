"""Shared template theme resolution; public pages never read owner preferences."""
import logging

logger = logging.getLogger(__name__)


def theme_preference(request) -> str:
    from core.prefs import resolve
    from webview import webgate
    from utils.auth_utils import is_authenticated
    if webgate.posture() != "local" and not is_authenticated(request):
        return "auto"
    try:
        from webview.pages import _effective_user_id
        value = resolve("ui.theme", _effective_user_id(request), webgate.data_dir(), default="auto")
        return value if value in ("auto", "dark", "light") else "auto"
    except Exception:
        logger.warning("Console theme preference unavailable", exc_info=True)
        return "auto"


def show_avatar(request) -> bool:
    """Consume the existing avatar preference in the current console shell."""
    from core.prefs import resolve
    from webview import webgate
    from utils.auth_utils import is_authenticated
    if webgate.posture() != 'local' and not is_authenticated(request):
        return True
    try:
        from webview.pages import _effective_user_id
        return bool(resolve('ui.show_avatar', _effective_user_id(request), webgate.data_dir(), default=True))
    except Exception:
        logger.warning('Console avatar preference unavailable', exc_info=True)
        return True
