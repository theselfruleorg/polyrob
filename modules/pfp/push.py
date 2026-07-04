"""Push the avatar to the agent's surfaces.

- **Twitter/X — live** via the v1.1 ``account/update_profile_image`` endpoint
  (OAuth1.0a). Flag-gated (``PFP_PUSH_TWITTER``) + hash-idempotent at the caller.
  Decoupled from the agent ``TWITTER_ENABLED`` write-gate: setting the avatar is an
  operator action, not an agent tweet. Fail-open — a 403 (Free-tier apps lack v1.1
  account endpoints) is surfaced as an actionable manual path, never a crash.
- **Telegram — assisted.** The Bot API CANNOT set a bot's own avatar (BotFather only),
  so we save the PNG and print the exact ``/setuserpic`` steps. Stated plainly.

tweepy is imported lazily so this module (and its tests) import without the dep.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Optional

_OAUTH1_ENV = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET_KEY",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
)


class TwitterCredsMissing(RuntimeError):
    """OAuth1.0a credentials required for update_profile_image are not configured."""


def sha256_file(path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def build_twitter_api(env: Optional[Mapping[str, str]] = None):
    """Build a tweepy v1.1 API from OAuth1.0a env creds. Raises
    :class:`TwitterCredsMissing` if any are absent (checked BEFORE importing tweepy)."""
    env = os.environ if env is None else env
    creds = {k: (env.get(k) or "").strip() for k in _OAUTH1_ENV}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise TwitterCredsMissing(f"missing Twitter OAuth1 creds: {', '.join(missing)}")

    import tweepy  # lazy: not a hard dep of this module

    auth = tweepy.OAuth1UserHandler(
        creds["TWITTER_API_KEY"], creds["TWITTER_API_SECRET_KEY"],
        creds["TWITTER_ACCESS_TOKEN"], creds["TWITTER_ACCESS_TOKEN_SECRET"],
    )
    return tweepy.API(auth)


def push_twitter(png_path, *, api: Any = None, env: Optional[Mapping[str, str]] = None) -> None:
    """Set the X profile image to ``png_path``. ``api`` is injectable for tests."""
    api = api if api is not None else build_twitter_api(env)
    api.update_profile_image(filename=str(png_path))


def telegram_instructions(png_path, bot_name: Optional[str] = None) -> str:
    who = f" for {bot_name}" if bot_name else ""
    return (
        "Telegram bots can't set their own avatar via the Bot API — use @BotFather:\n"
        f"  1. open a chat with @BotFather\n"
        f"  2. send /setuserpic and choose your bot{who}\n"
        f"  3. upload this image: {png_path}"
    )
