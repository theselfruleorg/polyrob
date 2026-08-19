"""core/optional_extras.py — one map from missing module → pip extra remedy
(proposal 027 WP1/WP3). Every surface that reports a missing optional
dependency routes through this so the user always sees the same actionable
command."""

import pytest

from core.optional_extras import (
    extra_for_module,
    missing_extra_hint,
    require_extra,
)


@pytest.mark.parametrize(
    "module,extra",
    [
        ("playwright", "browser"),
        ("playwright.async_api", "browser"),
        ("fastapi", "server"),
        ("uvicorn", "server"),
        ("socketio", "server"),
        ("argon2", "server"),
        ("aiogram", "telegram"),
        ("web3", "crypto"),
        ("sentence_transformers", "memory-vector"),
        ("faster_whisper", "voice"),
        ("tweepy", "twitter"),
    ],
)
def test_extra_for_module_maps_known_modules(module, extra):
    assert extra_for_module(module) == extra


def test_extra_for_module_unknown_returns_none():
    assert extra_for_module("definitely_not_a_dep") is None


def test_missing_extra_hint_formats_pip_command():
    hint = missing_extra_hint("No module named 'aiogram'")
    assert "pip install 'polyrob[telegram]'" in hint


def test_missing_extra_hint_none_for_unknown_module():
    assert missing_extra_hint("No module named 'left_pad'") is None


def test_require_extra_present_is_quiet():
    # os is always importable — require_extra checks specs, not extras.
    require_extra("server", modules=("os",))


def test_require_extra_missing_raises_with_remedy():
    with pytest.raises(ImportError) as exc:
        require_extra("telegram", modules=("definitely_not_a_dep",))
    assert "pip install 'polyrob[telegram]'" in str(exc.value)


def test_chromium_missing_hint_on_playwright_launch_error():
    from core.optional_extras import chromium_missing_hint

    err = (
        "BrowserType.launch: Executable doesn't exist at "
        "/Users/x/Library/Caches/ms-playwright/chromium-1148/chrome"
    )
    assert "python -m playwright install chromium" in chromium_missing_hint(err)


def test_chromium_missing_hint_none_for_other_errors():
    from core.optional_extras import chromium_missing_hint

    assert chromium_missing_hint("net::ERR_CONNECTION_REFUSED") is None
