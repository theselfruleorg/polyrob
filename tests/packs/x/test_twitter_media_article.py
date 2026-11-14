"""X Articles, media upload v2 + alt text, and complete tweet reads."""
import asyncio
from types import SimpleNamespace

import pytest

from polyrob_x import twitter_media as tm
from polyrob_x.twitter_media import (TwitterArticleAction, TwitterMediaArticleMixin,
                                     article_content_state, full_read_kwargs, tweet_extras)


def test_content_state_blocks_and_styles():
    state = article_content_state(
        "# Title\n\nFirst line\nsame para **bold** and *it*.\n\n## Sub\n- a\n- b\n1. one\n> quote")
    kinds = [(b["type"], b["text"]) for b in state["blocks"]]
    assert kinds == [("header-one", "Title"),
                     ("unstyled", "First line same para bold and it."),
                     ("header-two", "Sub"), ("unordered-list-item", "a"),
                     ("unordered-list-item", "b"), ("ordered-list-item", "one"),
                     ("blockquote", "quote")]
    assert state["entities"] == []
    para = state["blocks"][1]
    styles = {r["style"]: para["text"][r["offset"]:r["offset"] + r["length"]]
              for r in para["inline_style_ranges"]}
    assert styles == {"bold": "bold", "italic": "it"}


def test_snake_case_is_not_italic():
    state = article_content_state("call foo_bar_baz now")
    assert "inline_style_ranges" not in state["blocks"][0]


def test_tweet_extras_full_text_article_media():
    tweet = SimpleNamespace(data={
        "note_tweet": {"text": "the full long text"},
        "article": {"title": "T", "plain_text": "body"},
        "attachments": {"media_keys": ["3_1", "7_2"]}})
    includes = {"media": [
        {"media_key": "3_1", "type": "photo", "url": "https://x/p.jpg", "alt_text": "a cat"},
        {"media_key": "7_2", "type": "video", "preview_image_url": "https://x/v.jpg",
         "variants": [{"content_type": "video/mp4", "bit_rate": 100, "url": "lo"},
                      {"content_type": "video/mp4", "bit_rate": 900, "url": "hi"},
                      {"content_type": "application/x-mpegURL", "url": "m3u8"}]}]}
    out = tweet_extras(tweet, includes)
    assert out["text"] == "the full long text" and out["long_post"]
    assert out["article"] == {"title": "T", "plain_text": "body"}
    assert out["media"][0] == {"type": "photo", "url": "https://x/p.jpg", "alt_text": "a cat"}
    assert out["media"][1]["video_url"] == "hi"
    assert tweet_extras(SimpleNamespace(data={}), None) == {}


def test_full_read_kwargs_adds_fields_once():
    kw = full_read_kwargs(["text", "attachments"], ["author_id"])
    assert kw["tweet_fields"].count("attachments") == 1
    assert {"note_tweet", "article"} <= set(kw["tweet_fields"])
    assert "attachments.media_keys" in kw["expansions"] and "variants" in kw["media_fields"]


class _Tool(TwitterMediaArticleMixin):
    def __init__(self, tmp_path):
        self.calls = []
        self.api_v1 = None
        self.logger = SimpleNamespace(warning=lambda *a, **k: None)
        self.tmp = tmp_path

    def _resolve_media_paths(self, paths, ctx):
        return [str(self.tmp / p) for p in paths]

    async def _make_request(self, func, endpoint_type, **kw):
        method, route = kw["method"], kw["route"]
        self.calls.append((method, route, kw.get("json"), kw.get("data")))
        if route == "/2/media/upload" and method == "POST":
            return {"data": {"id": "111"}}
        if route.endswith("/initialize"):
            return {"data": {"id": "222"}}
        if route.endswith("/finalize"):
            return {"data": {"id": "222", "processing_info": {"state": "pending",
                                                              "check_after_secs": 0}}}
        if method == "GET":
            return {"data": {"processing_info": {"state": "succeeded"}}}
        if route == "/2/articles/draft":
            return {"data": {"id": "555", "title": kw["json"]["title"]}}
        if route.endswith("/publish"):
            return {"data": {"post_id": "999"}}
        return {"data": {}}

    def _check_ready(self):
        return None

    async def _precheck_write(self, name, params, ctx):
        self.gated = name
        return None

    def _ok(self, m):
        return SimpleNamespace(extracted_content=m, error=None)

    def _err(self, m):
        return SimpleNamespace(extracted_content=None, error=m)

    async def _published(self, text, last_id):
        return self._ok(f"{text} [{last_id}]")


def test_upload_image_with_alt_and_chunked_video(tmp_path):
    (tmp_path / "a.png").write_bytes(b"png")
    (tmp_path / "v.mp4").write_bytes(b"x" * (tm._CHUNK + 10))
    tool = _Tool(tmp_path)
    ids = asyncio.run(tool._upload_media(["a.png", "v.mp4"], None, ["a cat", ""]))
    assert ids == ["111", "222"]
    routes = [(m, r) for m, r, _, _ in tool.calls]
    assert ("POST", "/2/media/metadata") in routes
    meta = next(j for m, r, j, _ in tool.calls if r == "/2/media/metadata")
    assert meta == {"id": "111", "metadata": {"alt_text": {"text": "a cat"}}}
    appends = [d for m, r, _, d in tool.calls if r.endswith("/append")]
    assert [a["segment_index"] for a in appends] == ["0", "1"]
    init = next(j for m, r, j, _ in tool.calls if r.endswith("/initialize"))
    assert init["media_category"] == "tweet_video" and init["media_type"] == "video/mp4"
    assert ("GET", "/2/media/upload") in routes


def test_upload_failure_without_v1_names_the_file(tmp_path):
    (tmp_path / "a.png").write_bytes(b"png")
    tool = _Tool(tmp_path)

    async def boom(**kw):
        raise RuntimeError("X API 403")
    tool._make_request = lambda func, endpoint_type, **kw: boom(**kw)
    with pytest.raises(RuntimeError, match="a.png"):
        asyncio.run(tool._upload_media(["a.png"], None))


def test_article_draft_then_publish_with_cover(tmp_path):
    (tmp_path / "c.jpg").write_bytes(b"jpg")
    tool = _Tool(tmp_path)
    res = asyncio.run(tool.twitter_article(TwitterArticleAction(
        title="Hello", body="# H\n\ntext", cover_image_path="c.jpg")))
    assert res.error is None and "999" in res.extracted_content
    assert tool.gated == "twitter_article"
    draft = next(j for m, r, j, _ in tool.calls if r == "/2/articles/draft")
    assert draft["cover_media"] == {"media_id": "111", "media_category": "tweet_image"}
    assert draft["content_state"]["blocks"][0] == {"text": "H", "type": "header-one"}
    assert ("POST", "/2/articles/555/publish", None, None) in tool.calls


def test_article_draft_only(tmp_path):
    tool = _Tool(tmp_path)
    res = asyncio.run(tool.twitter_article(TwitterArticleAction(
        title="Hello", body="text", publish=False)))
    assert "draft 555" in res.extracted_content
    assert not any(r.endswith("/publish") for _, r, _, _ in tool.calls)
