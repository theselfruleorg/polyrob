"""WebFetchTool: stateless web page reader (URL -> markdown). No browser/Chromium."""

import os
from typing import Any, Dict, Optional

from core.config import BotConfig
from tools.base_tool import BaseTool
from tools.controller.views import WebFetchAction
from tools.web_fetch.fetcher import safe_fetch, WebFetchError
from tools.web_fetch.render import render_html_to_markdown, classify_content


def _allow_private_urls() -> bool:
	return os.getenv("WEB_FETCH_ALLOW_PRIVATE_URLS", "false").strip().lower() in ("1", "true", "yes", "on")


class WebFetchTool(BaseTool):
	"""Fetch a URL and return clean markdown. The lightweight default web reader."""

	def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
		super().__init__(name=name, config=config, container=container)
		self._enabled = True

	@property
	def required_config(self) -> Dict[str, str]:
		return {}

	@BaseTool.action(
		description=(
			"Fetch a single web page by URL and return its main content as markdown. "
			"Use this to READ a page you have the URL for (articles, docs, API pages). "
			"Lightweight and fast — no browser. For SEARCH use perplexity/anysite; for "
			"pages needing login/clicks/forms use the browser tool."
		),
		param_model=WebFetchAction,
	)
	async def fetch_url(self, params: WebFetchAction) -> str:
		# No services/state to set up — fetch is self-contained.
		url = (params.url or "").strip()
		if not url:
			return "Please provide a url to fetch."
		try:
			result = await safe_fetch(url, validate=not _allow_private_urls())
		except WebFetchError as e:
			return f"Could not fetch the page (blocked or unreachable): {e}"
		except Exception as e:  # network/timeout — fail soft, agent can escalate to browser
			return f"Could not fetch the page: {e}"

		kind = classify_content(result.content_type, result.body)
		if kind == "pdf":
			return (f"[web_fetch: {result.final_url} is a PDF, not an HTML page. "
			        f"Save it and use the filesystem document-processing tools to extract text.]")
		if kind == "binary":
			return (f"[web_fetch: {result.final_url} returned non-HTML content "
			        f"({result.content_type}); cannot render as markdown.]")
		try:
			html = result.body.decode("utf-8", errors="replace")
		except Exception:
			html = result.body.decode("latin-1", errors="replace")
		return render_html_to_markdown(html, max_chars=params.max_chars)
