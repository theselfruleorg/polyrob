"""Push the avatar to the agent's surfaces.

- **Twitter/X — live** via the v1.1 ``account/update_profile_image`` endpoint
  (OAuth1.0a). Hash-idempotent at the caller.
  Decoupled from the agent ``TWITTER_ENABLED`` write-gate: setting the avatar is an
  operator action, not an agent tweet. Fail-open — a 403 (Free-tier apps lack v1.1
  account endpoints) is surfaced as an actionable manual path, never a crash.
- **Discord — live** via ``PATCH /users/@me`` (bot token, ``DISCORD_BOT_TOKEN`` — the
  same env the Discord surface uses). Hash-idempotent at the caller. Stdlib
  urllib — no new dependency.
- **Telegram — live** via ``setMyProfilePhoto`` (the bot's own photo, a static JPEG —
  the PNG is re-encoded) and ``setChatPhoto`` (a group's photo; the bot must be an
  admin with the change-info right). ``TELEGRAM_BOT_TOKEN``, hash-idempotent at the
  caller; on failure the caller prints the BotFather ``/setuserpic`` steps as the
  manual path.

There is NO switch in front of a push. The act is the consent (a command, or the
agent on an owner turn) and the credentials are the ability: an account with no
credentials is "not connected", never an error, unless the caller named it.

requests-oauthlib is imported lazily so this module (and its tests) import without the dep.
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


_UPDATE_PROFILE_IMAGE_URL = "https://api.twitter.com/1.1/account/update_profile_image.json"


class _ProfileImageApi:
    """The one v1.1 call this module makes, signed OAuth1.0a by requests-oauthlib
    (the `twitter` extra). Same request as tweepy's ``API.update_profile_image``:
    a multipart POST with the file in the ``image`` field. A non-2xx raises."""

    def __init__(self, session):
        self._session = session

    def update_profile_image(self, filename=None):
        with open(filename, "rb") as fh:
            resp = self._session.post(_UPDATE_PROFILE_IMAGE_URL,
                                      files={"image": (Path(filename).name, fh)}, timeout=60)
        resp.raise_for_status()


def build_twitter_api(env: Optional[Mapping[str, str]] = None):
    """Build the v1.1 profile-image client from OAuth1.0a env creds. Raises
    :class:`TwitterCredsMissing` if any are absent (checked BEFORE importing the SDK)."""
    env = os.environ if env is None else env
    creds = {k: (env.get(k) or "").strip() for k in _OAUTH1_ENV}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise TwitterCredsMissing(f"missing Twitter OAuth1 creds: {', '.join(missing)}")

    from requests_oauthlib import OAuth1Session  # lazy: the `twitter` extra

    return _ProfileImageApi(OAuth1Session(
        creds["TWITTER_API_KEY"], client_secret=creds["TWITTER_API_SECRET_KEY"],
        resource_owner_key=creds["TWITTER_ACCESS_TOKEN"],
        resource_owner_secret=creds["TWITTER_ACCESS_TOKEN_SECRET"],
    ))


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
    from core.surfaces.telegram_api import telegram_api_base
    req = urllib.request.Request(
        f"{telegram_api_base(env)}/bot{token}/{method}", data=body,
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


# --------------------------------------------------------------------------- #
# The push procedure — ONE home for every caller (120 W1)
# --------------------------------------------------------------------------- #
# `polyrob avatar push`, the chat `/avatar push` and the agent's `agent_avatar`
# all call :func:`push_avatar`. It was written inside the click command, where
# nothing below the CLI could reach it, so the agent could set its face and
# could not make its own accounts show it.

import json as _json
import threading
from dataclasses import dataclass
from typing import List, Sequence

_ALIASES = {"x": "twitter"}


def push_surfaces() -> tuple:
    """The accounts a push can name, in the order they run: X (the ``x`` pack's
    account, not a chat surface) and every chat surface whose platform sets its
    own picture (``core.surfaces.catalog``, capability ``profile_photo``)."""
    from core.surfaces.catalog import capability_ids
    return ("twitter",) + capability_ids("profile_photo")

#: One writer at a time in this process; `_record_pushed` also takes the file
#: lock, so two processes (the CLI and the service) merge instead of overwriting.
_PUSHED_LOCK = threading.Lock()


@dataclass(frozen=True)
class PushResult:
    """One push target's outcome. ``status`` is ``updated`` | ``unchanged`` |
    ``not_connected`` | ``no_credentials`` | ``failed`` | ``refused``; ``text`` is the
    line(s) a seat prints as they are."""
    surface: str
    status: str
    text: str


def normalize_surfaces(names: Sequence[str]) -> tuple:
    """``(surfaces, unknown)`` from caller words (``x`` = ``twitter``), de-duplicated."""
    out, unknown = [], []
    for raw in names or ():
        name = _ALIASES.get(str(raw).strip().lower(), str(raw).strip().lower())
        if not name:
            continue
        (out if name in push_surfaces() else unknown).append(name)
    return tuple(dict.fromkeys(out)), tuple(dict.fromkeys(unknown))


def _pushed_path(home, instance_id):
    from core.avatar import avatar_dir
    return avatar_dir(home, instance_id) / "pushed.json"


def load_pushed(home, instance_id) -> dict:
    """``{target: {"hash": sha256}}`` — what each surface shows. ``{}`` when the
    record is absent or broken: the worst case is one repeated, harmless push."""
    try:
        data = _json.loads(_pushed_path(home, instance_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_pushed(home, instance_id, target: str, digest: str) -> None:
    from core.state.files import file_lock
    path = _pushed_path(home, instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PUSHED_LOCK, file_lock(path):                  # threads, then processes
        pushed = load_pushed(home, instance_id)          # re-read: merge, never overwrite
        pushed[target] = {"hash": digest}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(_json.dumps(pushed, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _push_one(home, instance_id, img, *, surface: str, target: str, label: str,
              named: bool, creds_missing: type, send, done: str, manual) -> PushResult:
    try:
        digest = sha256_file(img)
        if load_pushed(home, instance_id).get(target, {}).get("hash") == digest:
            return PushResult(target, "unchanged", f"{surface}: {label}unchanged, skipped")
        send()
        _record_pushed(home, instance_id, target, digest)
        return PushResult(target, "updated", f"{surface}: {done} ✓")
    except creds_missing as e:
        if not named:                  # "every account" skips one that does not exist
            return PushResult(target, "not_connected", f"{surface}: not connected, skipped")
        return PushResult(target, "no_credentials", f"{surface}: {e}")
    except Exception as e:  # fail-open: never crash, always give the manual path
        return PushResult(target, "failed", manual(e))


def push_avatar(home, instance_id, *, surfaces: Sequence[str] = (),
                telegram_chats: Sequence[str] = ()) -> List[PushResult]:
    """Push the instance's avatar to its own accounts. Never raises.

    ``surfaces`` empty = every account that has credentials (one with none says
    ``not connected``; a NAMED one with none says which credential is missing).
    A ``telegram_chats`` entry is that room's photo ONLY: the bot's own photo
    changes only when ``telegram`` is named too (or no surface is named). One :class:`PushResult` per
    target; a refusal that stops everything (no avatar, an unreadable slot, an
    SVG, an unknown surface word) is the single result, with ``status="refused"``.
    Hash-idempotent per target (``pushed.json`` beside the image).

    Blocking network calls: an async caller runs it in a thread.
    """
    from core.avatar import load_avatar
    chosen, unknown = normalize_surfaces(surfaces)
    if unknown:
        return [PushResult("avatar", "refused",
                           f"unknown surface: {', '.join(unknown)} "
                           f"(known: x, {', '.join(push_surfaces()[1:])})")]
    st = load_avatar(home, instance_id)
    if not st.is_set:
        return [PushResult("avatar", "refused",
                           "no avatar to push — set one first" if st.state == "none"
                           else f"the avatar is unreadable: {st.detail}")]
    img = st.path
    if not st.is_raster:
        return [PushResult("avatar", "refused",
                           "the avatar is an SVG image; X, Discord and Telegram take only a "
                           "raster image (PNG/JPEG/GIF/WebP) — set a raster image first")]
    chats = tuple(dict.fromkeys(str(c).strip() for c in telegram_chats or () if str(c).strip()))
    named = bool(chosen) or bool(chats)
    bot_photo = "telegram" in chosen or not named      # a room alone is the room only
    if chats and "telegram" not in chosen:
        chosen = chosen + ("telegram",)
    if not named:
        chosen = push_surfaces()
    out: List[PushResult] = []
    if "twitter" in chosen:
        out.append(_push_one(
            home, instance_id, img, surface="twitter", target="twitter", label="",
            named=named, creds_missing=TwitterCredsMissing,
            send=lambda: push_twitter(img), done="profile image updated",
            manual=lambda e: (f"twitter: could not set avatar ({e}). Set it manually in "
                              f"the X app; image: {img}")))
    if "discord" in chosen:
        out.append(_push_one(
            home, instance_id, img, surface="discord", target="discord", label="",
            named=named, creds_missing=DiscordCredsMissing,
            send=lambda: push_discord(img), done="bot avatar updated",
            manual=lambda e: (f"discord: could not set avatar ({e}). Set it manually in the "
                              f"Discord developer portal (Bot → Icon); image: {img}")))
    if "telegram" in chosen:
        targets = ([("telegram", None, "bot photo")] if bot_photo else []) + [
            (f"telegram:{c}", c, f"chat {c} photo") for c in chats]
        for target, chat_id, label in targets:
            if chat_id is None:
                def send():
                    push_telegram_bot(img)

                def manual(e, label=label):
                    return (f"telegram: could not set the {label} ({e})\n"
                            + telegram_instructions(img))
            else:
                def send(chat_id=chat_id):
                    push_telegram_chat(img, chat_id)

                def manual(e, label=label):
                    return (f"telegram: could not set the {label} ({e})\n"
                            f"  make the bot an admin with 'Change group info', or set "
                            f"it in the group settings; image: {img}")
            res = _push_one(
                home, instance_id, img, surface="telegram", target=target,
                label=f"{label} ", named=named,
                creds_missing=TelegramCredsMissing, send=send, done=f"{label} updated",
                manual=manual)
            out.append(res)
            if res.status in ("not_connected", "no_credentials"):
                break                      # one token: the rest would repeat it
    return out


def render_push(results: Sequence[PushResult]) -> str:
    """The results as the lines every seat prints."""
    return "\n".join(r.text for r in results)
