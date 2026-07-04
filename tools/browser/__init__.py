# Browser/BrowserContext need playwright (an optional extra). Guard them so the
# package — and in particular `tools.browser.views`, which the core Agent imports —
# stays importable in a headless install without playwright. views/dom need no playwright.
try:
    from tools.browser.browser import Browser
    from tools.browser.context import BrowserContext, BrowserContextConfig
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    Browser = None
    BrowserContext = None
    BrowserContextConfig = None
    _PLAYWRIGHT_AVAILABLE = False
from tools.browser.views import BrowserState, BrowserError, URLNotAllowedError

__all__ = [
    'Browser',
    'BrowserContext',
    'BrowserContextConfig',
    'BrowserState',
    'BrowserError',
    'URLNotAllowedError'
]