"""X media + Articles for the ``twitter`` tool (a mixin of ``TwitterTool``).

Docs (docs.x.com, read 2026-11-12):

* Media upload v2 — ``POST /2/media/upload`` (simple, images/GIF) and the chunked
  ``POST /2/media/upload/initialize`` → ``/{id}/append`` → ``/{id}/finalize`` +
  ``GET /2/media/upload?command=STATUS`` (required for video). The command-style
  ``command=INIT`` protocol is the PREVIOUS one; v1.1 ``media/upload`` stays only
  as a fallback when the v2 call fails.
* Alt text — ``POST /2/media/metadata`` ``{"id", "metadata": {"alt_text": {"text"}}}``
  (≤1000 chars).
* Articles — ``POST /2/articles/draft`` ``{"title", "content_state": {"blocks",
  "entities"}, "cover_media"?}`` then ``POST /2/articles/{id}/publish`` →
  ``data.post_id``. Block types: unstyled, header-one, header-two,
  unordered-list-item, ordered-list-item, blockquote, atomic.
* Reads — ``tweet.fields=note_tweet`` holds the FULL text of a post over 280
  chars (``text`` is truncated); ``article`` holds an Article's metadata/text;
  media rides ``expansions=attachments.media_keys`` + ``media.fields``.

Every write here is user-context OAuth 1.0a, the same credential ``create_tweet``
uses (the OAuth 2.0 login carries no ``tweet.write``/``media.write`` scope).
⚠️ NO ``from __future__ import annotations`` — the Registry introspects the
action's first-param annotation.
"""

import asyncio
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool

X_API = "https://api.x.com"

#: The read fields that make a tweet read complete: full long-post text, Article
#: body, and the attached media.
FULL_TWEET_FIELDS = ["note_tweet", "article", "attachments"]
MEDIA_EXPANSION = "attachments.media_keys"
MEDIA_FIELDS = ["media_key", "type", "url", "preview_image_url", "alt_text",
                "duration_ms", "width", "height", "variants"]

_VIDEO_EXT = frozenset({".mp4", ".mov", ".m4v", ".webm"})
_GIF_EXT = frozenset({".gif"})
_CHUNK = 4 * 1024 * 1024  # docs: keep each segment ≤ 5 MB
_STATUS_MAX_WAIT_SEC = 600
ALT_TEXT_MAX = 1000

_LIVE_NOTE = (" This is the LIVE public account — verify every path exists before "
              "attaching it; never publish test text.")


class TwitterArticleAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., min_length=1, max_length=200, description="Article title.")
    body: str = Field(..., min_length=1, max_length=100000, description=(
        "Article body in light markdown: blank line = new paragraph; '# ' / '## ' "
        "= heading; '- ' or '* ' = bullet; '1. ' = numbered item; '> ' = quote; "
        "**bold**, *italic*, ~~strike~~ inline."))
    cover_image_path: Optional[str] = Field(None, description=(
        "Local image file path (session workspace) for the cover." + _LIVE_NOTE))
    publish: bool = Field(True, description=(
        "true publishes the Article as a post; false leaves it a DRAFT on X."))


# --- Article body → content_state ---------------------------------------------

_BLOCK_PREFIXES = (
    (re.compile(r"^#{2,6}\s+"), "header-two"),
    (re.compile(r"^#\s+"), "header-one"),
    (re.compile(r"^[-*]\s+"), "unordered-list-item"),
    (re.compile(r"^\d+[.)]\s+"), "ordered-list-item"),
    (re.compile(r"^>\s?"), "blockquote"),
)
_INLINE = (("**", "bold"), ("~~", "strikethrough"), ("*", "italic"))


def _inline_styles(text: str):
    """Strip ``**b**`` / ``*i*`` / ``~~s~~`` markers; return (plain, style ranges)."""
    ranges: List[Dict[str, Any]] = []
    for marker, style in _INLINE:
        pat = re.compile(re.escape(marker) + r"(?=\S)(.+?)(?<=\S)" + re.escape(marker))
        while True:
            m = pat.search(text)
            if not m:
                break
            inner = m.group(1)
            start = m.start()
            removed = len(marker)
            # earlier ranges after this point shift left by the removed markers
            for r in ranges:
                if r["offset"] >= m.end():
                    r["offset"] -= 2 * removed
                elif r["offset"] > start:
                    r["offset"] -= removed
            ranges.append({"offset": start, "length": len(inner), "style": style})
            text = text[:start] + inner + text[m.end():]
    return text, sorted(ranges, key=lambda r: r["offset"])


def article_content_state(body: str) -> Dict[str, Any]:
    """Light markdown → the Articles ``content_state`` (``blocks`` + ``entities``)."""
    blocks: List[Dict[str, Any]] = []
    para: List[str] = []

    def flush():
        if para:
            add(" ".join(s.strip() for s in para), "unstyled")
            para.clear()

    def add(text: str, kind: str):
        plain, styles = _inline_styles(text.strip())
        if not plain:
            return
        block: Dict[str, Any] = {"text": plain, "type": kind}
        if styles:
            block["inline_style_ranges"] = styles
        blocks.append(block)

    for raw in (body or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        for pat, kind in _BLOCK_PREFIXES:
            if pat.match(line.lstrip()):
                flush()
                add(pat.sub("", line.lstrip(), count=1), kind)
                break
        else:
            para.append(line)
    flush()
    return {"blocks": blocks, "entities": []}


# --- read side: full text, Article, media ---------------------------------------

def _field(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    val = getattr(obj, name, None)
    if val is None:
        data = getattr(obj, "data", None)
        if isinstance(data, dict):
            val = data.get(name)
    return val


def _media_entry(m) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"type": _field(m, "type")}
    for k in ("url", "preview_image_url", "alt_text", "duration_ms", "width", "height"):
        v = _field(m, k)
        if v not in (None, ""):
            entry[k] = v
    variants = [v for v in (_field(m, "variants") or []) if isinstance(v, dict)
                and v.get("content_type") == "video/mp4"]
    if variants:
        entry["video_url"] = max(variants, key=lambda v: v.get("bit_rate") or 0).get("url")
    return entry


def tweet_extras(tweet, includes=None) -> Dict[str, Any]:
    """The fields a plain tweet read loses: ``text`` (FULL, from ``note_tweet``
    when the post is longer than 280 chars), ``article`` and ``media``."""
    out: Dict[str, Any] = {}
    note = _field(tweet, "note_tweet")
    if isinstance(note, dict) and note.get("text"):
        out["text"] = note["text"]
        out["long_post"] = True
    article = _field(tweet, "article")
    if isinstance(article, dict) and article:
        out["article"] = {k: v for k, v in article.items()
                          if k in ("title", "preview_text", "plain_text", "text")
                          and v not in (None, "")} or article
    keys = (_field(tweet, "attachments") or {}).get("media_keys") or []
    if keys:
        by_key = {str(_field(m, "media_key")): m
                  for m in ((includes or {}).get("media") or [])}
        out["media"] = [_media_entry(by_key[k]) if k in by_key else {"media_key": k}
                        for k in map(str, keys)]
    return out


def full_read_kwargs(tweet_fields: List[str], expansions: List[str]) -> Dict[str, Any]:
    """``tweet_fields`` + ``expansions`` + ``media_fields`` for a complete read."""
    return {"tweet_fields": list(dict.fromkeys([*tweet_fields, *FULL_TWEET_FIELDS])),
            "expansions": list(dict.fromkeys([*expansions, MEDIA_EXPANSION])),
            "media_fields": MEDIA_FIELDS}


# --- the mixin --------------------------------------------------------------------

def _media_category(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _VIDEO_EXT:
        return "tweet_video"
    if ext in _GIF_EXT:
        return "tweet_gif"
    return "tweet_image"


class TwitterMediaArticleMixin:
    """Media upload v2 (+ alt text) and Articles. Mixed into ``TwitterTool``."""

    def _oauth1(self):
        if not getattr(self, "_oauth1_available", False):
            raise RuntimeError(
                "X media/Article writes need the OAuth 1.0a user keys (TWITTER_API_KEY/"
                "_SECRET_KEY + TWITTER_ACCESS_TOKEN/_SECRET); they are not all set")
        from polyrob_x._vendor import tweepy  # type: ignore
        return tweepy.OAuth1UserHandler(self.api_key, self.api_secret,
                                        self.access_token, self.access_token_secret).apply_auth()

    def _x_call(self, method: str, route: str, *, json=None, data=None, files=None,
                params=None) -> Dict[str, Any]:
        import requests
        resp = requests.request(method, X_API + route, json=json, data=data, files=files,
                                params=params, auth=self._oauth1(), timeout=120)
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f"X API {resp.status_code} on {method} {route}: "
                               f"{resp.text[:300]}")
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    async def _x(self, endpoint_type: str, method: str, route: str, **kw) -> Dict[str, Any]:
        # kwargs only: the rate limiter takes its own keywords before *args.
        return await self._make_request(func=self._x_call, endpoint_type=endpoint_type,
                                        method=method, route=route, **kw)

    async def _upload_one_v2(self, path: str) -> str:
        category = _media_category(path)
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if category == "tweet_image":
            with open(path, "rb") as fh:
                body = await self._x("media", "POST", "/2/media/upload",
                                     data={"media_category": category},
                                     files={"media": (os.path.basename(path), fh, mime)})
            return str((body.get("data") or {}).get("id"))
        size = os.path.getsize(path)
        init = await self._x("media", "POST", "/2/media/upload/initialize", json={
            "media_type": mime, "total_bytes": size, "media_category": category})
        mid = str((init.get("data") or {}).get("id"))
        with open(path, "rb") as fh:
            index = 0
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                await self._x("media", "POST", f"/2/media/upload/{mid}/append",
                              data={"segment_index": str(index)},
                              files={"media": ("chunk", chunk, "application/octet-stream")})
                index += 1
        fin = await self._x("media", "POST", f"/2/media/upload/{mid}/finalize")
        info = (fin.get("data") or {}).get("processing_info")
        waited = 0.0
        while info and info.get("state") in ("pending", "in_progress"):
            delay = float(info.get("check_after_secs") or 2)
            if waited + delay > _STATUS_MAX_WAIT_SEC:
                raise RuntimeError(f"X media {mid} still processing after {int(waited)}s")
            await asyncio.sleep(delay)
            waited += delay
            st = await self._x("media", "GET", "/2/media/upload",
                               params={"command": "STATUS", "media_id": mid})
            info = (st.get("data") or {}).get("processing_info")
        if info and info.get("state") == "failed":
            raise RuntimeError(f"X media {mid} processing failed: {info.get('error') or info}")
        return mid

    async def _upload_one_v1(self, path: str) -> str:
        kwargs: Dict[str, Any] = {"filename": path, "media_category": _media_category(path)}
        if kwargs["media_category"] != "tweet_image":
            kwargs["chunked"] = True
        media = await self._make_request(func=self.api_v1.media_upload,
                                         endpoint_type="media", **kwargs)
        return str(getattr(media, "media_id", None) or getattr(media, "media_id_string", None))

    async def _upload_media(self, media_paths: List[str], execution_context=None,
                            alt_texts: Optional[List[str]] = None) -> List[str]:
        """Upload local media (v2; v1.1 only as a fallback) → media_id strings.
        ``alt_texts[i]`` (≤1000 chars) is set on the i-th file via /2/media/metadata."""
        paths = self._resolve_media_paths(media_paths, execution_context)
        ids: List[str] = []
        for i, path in enumerate(paths):
            try:
                mid = await self._upload_one_v2(path)
            except Exception as e:
                if not getattr(self, "api_v1", None):
                    raise RuntimeError(f"media upload failed for {os.path.basename(path)}: {e}")
                self.logger.warning("twitter: v2 media upload failed (%s); trying v1.1", e)
                mid = await self._upload_one_v1(path)
            alt = (alt_texts[i] if alt_texts and i < len(alt_texts) else None) or ""
            if alt.strip():
                await self._x("media", "POST", "/2/media/metadata", json={
                    "id": mid, "metadata": {"alt_text": {"text": alt.strip()[:ALT_TEXT_MAX]}}})
            ids.append(mid)
        return ids

    @BaseTool.action("Write and publish an X Article (long-form post with title, "
                     "headings, lists, quotes and an optional cover image). Use this for "
                     "long text instead of a thread.", param_model=TwitterArticleAction)
    async def twitter_article(self, params: TwitterArticleAction, execution_context=None):
        notready = self._check_ready()
        if notready:
            return notready
        block = await self._precheck_write("twitter_article", params, execution_context)
        if block:
            return block
        try:
            state = article_content_state(params.body)
            if not state["blocks"]:
                return self._err("Article body has no text")
            draft: Dict[str, Any] = {"title": params.title, "content_state": state}
            if params.cover_image_path:
                cover = await self._upload_media([params.cover_image_path], execution_context)
                draft["cover_media"] = {"media_id": cover[0], "media_category": "tweet_image"}
            body = await self._x("tweets", "POST", "/2/articles/draft", json=draft)
            article_id = str((body.get("data") or {}).get("id") or "")
            if not article_id:
                return self._err(f"X returned no Article id: {body}")
            if not params.publish:
                return self._ok(f"📝 Article draft {article_id} saved on X (not published)")
            pub = await self._x("tweets", "POST", f"/2/articles/{article_id}/publish")
            post_id = str((pub.get("data") or {}).get("post_id") or "")
            return await self._published(
                f"📝 Published Article {article_id} as post {post_id}", post_id or None)
        except Exception as e:
            return self._err(f"Error publishing Article: {e}")
