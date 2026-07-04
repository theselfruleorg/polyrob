import pytest

from core.config import BotConfig
from tools.web_fetch.tool import WebFetchTool
from tools.controller.views import WebFetchAction
from tools.web_fetch.fetcher import FetchResult, WebFetchError


@pytest.mark.asyncio
async def test_fetch_url_returns_markdown(monkeypatch):
	tool = WebFetchTool(name="web_fetch", config=BotConfig())

	async def fake_safe_fetch(url, **kw):
		body = ("<html><body><h1>Hello</h1><p>This is real content about cats and dogs "
		        "and plenty more readable prose so it clears the shell threshold. " * 4 +
		        "</p></body></html>").encode()
		return FetchResult(final_url=url, status=200, content_type="text/html", body=body)

	monkeypatch.setattr("tools.web_fetch.tool.safe_fetch", fake_safe_fetch)
	out = await tool.fetch_url(WebFetchAction(url="http://example.com/"))
	assert "Hello" in out and "cats" in out


@pytest.mark.asyncio
async def test_fetch_url_pdf_rejected(monkeypatch):
	tool = WebFetchTool(name="web_fetch", config=BotConfig())

	async def fake_safe_fetch(url, **kw):
		return FetchResult(final_url=url, status=200, content_type="application/pdf", body=b"%PDF-1.7")

	monkeypatch.setattr("tools.web_fetch.tool.safe_fetch", fake_safe_fetch)
	out = await tool.fetch_url(WebFetchAction(url="http://example.com/x.pdf"))
	assert "pdf" in out.lower() and "filesystem" in out.lower()


@pytest.mark.asyncio
async def test_fetch_url_blocked_is_clean_message(monkeypatch):
	tool = WebFetchTool(name="web_fetch", config=BotConfig())

	async def fake_safe_fetch(url, **kw):
		raise WebFetchError("blocked URL: IP in blocked range")

	monkeypatch.setattr("tools.web_fetch.tool.safe_fetch", fake_safe_fetch)
	out = await tool.fetch_url(WebFetchAction(url="http://10.0.0.1/"))
	assert "could not fetch" in out.lower() or "blocked" in out.lower()
