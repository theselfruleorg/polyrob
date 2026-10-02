"""Push the avatar to the agent's surfaces.

- **Twitter/X — live** via the v1.1 ``account/update_profile_image`` endpoint
  (OAuth1.0a). Flag-gated (``PFP_PUSH_TWITTER``) + hash-idempotent at the caller.
  Decoupled from the agent ``TWITTER_ENABLED`` write-gate: setting the avatar is an
  operator action, not an agent tweet. Fail-open — a 403 (Free-tier apps lack v1.1
  account endpoints) is surfaced as an actionable manual path, never a crash.
- **Discord — live** via ``PATCH /users/@me`` (bot token, ``DISCORD_BOT_TOKEN`` — the
  same env the Discord surface uses). Flag-gated (``PFP_PUSH_DISCORD``) +
  hash-idempotent at the caller. Stdlib urllib — no new dependency.
- **Telegram — live** via ``setMyProfilePhoto`` (the bot's own photo, a static JPEG —
  the PNG is re-encoded) and ``setChatPhoto`` (a group's photo; the bot must be an
  admin with the change-info right). ``TELEGRAM_BOT_TOKEN``, flag-gated
  (``PFP_PUSH_TELEGRAM``), hash-idempotent at the caller; on failure the caller prints
  the BotFather ``/setuserpic`` steps as the manual path.

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


class NotRaster(ValueError):
    """The avatar is an SVG; X and Discord take only a raster image."""


def require_raster(png_path) -> None:
    """Raise :class:`NotRaster` for an SVG avatar (checked by its leading bytes)."""
    from core.avatar import RASTER_EXTS, sniff_image
    with open(png_path, "rb") as fh:
        head = fh.read(1024)
    if sniff_image(head) not in RASTER_EXTS:
        raise NotRaster(
            "the avatar is an SVG image, and X/Discord/Telegram take only PNG, "
            "JPEG, GIF or WebP — set a raster image first (`polyrob avatar set <png>`)")


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
    require_raster(png_path)
    api = api if api is not None else build_twitter_api(env)
    api.update_profile_image(filename=str(png_path))


class DiscordCredsMissing(RuntimeError):
    """``DISCORD_BOT_TOKEN`` is not configured."""


def push_discord(png_path, *, env: Optional[Mapping[str, str]] = None,
                 opener: Any = None) -> None:
    """Set the Discord bot avatar via ``PATCH /users/@me``. ``opener`` (a callable
    ``(urllib.request.Request) -> response``) is injectable for tests."""
    import base64
    import json as _json
    import urllib.request

    env = os.environ if env is None else env
    token = (env.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        raise DiscordCredsMissing("missing DISCORD_BOT_TOKEN")
    require_raster(png_path)

    from core.avatar import CONTENT_TYPES, sniff_image
    raw = Path(png_path).read_bytes()
    ctype = CONTENT_TYPES.get(sniff_image(raw) or "png", "image/png")
    b64 = base64.b64encode(raw).decode("ascii")
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        data=_json.dumps({"avatar": f"data:{ctype};base64,{b64}"}).encode("utf-8"),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "polyrob-avatar-push"},
        method="PATCH",
    )
    open_fn = opener if opener is not None else (
        lambda r: urllib.request.urlopen(r, timeout=30))
    with open_fn(req):
        pass  # 2xx = success; HTTPError propagates to the fail-open caller


class TelegramCredsMissing(RuntimeError):
    """``TELEGRAM_BOT_TOKEN`` is not configured."""


def to_jpeg(png_path) -> bytes:
    """Re-encode the avatar as a JPEG (Telegram's static profile photo is JPG-only).
    Transparency is flattened onto black."""
    import io

    from PIL import Image

    with Image.open(png_path) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (0, 0, 0))
            flat.paste(rgba, mask=rgba.split()[-1])
            im = flat
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=95)
        return buf.getvalue()


def _multipart(fields: Mapping[str, str], files: Mapping[str, tuple]) -> tuple:
    import uuid
    boundary = f"polyrob{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("utf-8")
    for name, (filename, data, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _telegram_call(method: str, fields: Mapping[str, str], files: Mapping[str, tuple], *,
                   env: Optional[Mapping[str, str]], opener: Any) -> None:
    import json as _json
    import urllib.error
    import urllib.request

    env = os.environ if env is None else env
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise TelegramCredsMissing("missing TELEGRAM_BOT_TOKEN")
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=body,
        headers={"Content-Type": ctype, "User-Agent": "polyrob-avatar-push"}, method="POST")
    open_fn = opener if opener is not None else (
        lambda r: urllib.request.urlopen(r, timeout=30))
    try:
        with open_fn(req) as resp:
            payload = _json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:  # Telegram puts the reason in the error body
        try:
            payload = _json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        raise RuntimeError(f"{method}: {payload.get('description') or f'HTTP {e.code}'}") from None
    if not payload.get("ok"):
        raise RuntimeError(f"{method}: {payload.get('description') or 'not ok'}")


def push_telegram_bot(png_path, *, env: Optional[Mapping[str, str]] = None,
                      opener: Any = None) -> None:
    """Set the bot's own profile photo (``setMyProfilePhoto``, static JPEG)."""
    import json as _json
    require_raster(png_path)
    _telegram_call(
        "setMyProfilePhoto",
        {"photo": _json.dumps({"type": "static", "photo": "attach://avatar"})},
        {"avatar": ("avatar.jpg", to_jpeg(png_path), "image/jpeg")},
        env=env, opener=opener)


def push_telegram_chat(png_path, chat_id: str, *, env: Optional[Mapping[str, str]] = None,
                       opener: Any = None) -> None:
    """Set a group's photo (``setChatPhoto``; the bot must be an admin there)."""
    require_raster(png_path)
    _telegram_call(
        "setChatPhoto", {"chat_id": str(chat_id)},
        {"photo": ("avatar.jpg", to_jpeg(png_path), "image/jpeg")},
        env=env, opener=opener)


def telegram_instructions(png_path, bot_name: Optional[str] = None) -> str:
    who = f" for {bot_name}" if bot_name else ""
    return (
        "The Bot API did not set the bot photo — set it by hand with @BotFather:\n"
        f"  1. open a chat with @BotFather\n"
        f"  2. send /setuserpic and choose your bot{who}\n"
        f"  3. upload this image: {png_path}"
    )
