"""WebFetchTool: stateless web page reader (URL -> markdown). No browser/Chromium."""

import os
from typing import Any, Dict, Optional

from core.config import BotConfig
from tools.base_tool import BaseTool
from tools.controller.views import WebFetchAction
from tools.web_fetch.fetcher import safe_fetch, WebFetchError
from tools.web_fetch.render import render_html_to_markdown, render_text, classify_content


def _allow_private_urls() -> bool:
	from core.env import bool_env
	return bool_env("WEB_FETCH_ALLOW_PRIVATE_URLS", False)


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
			"Fetch a single URL and return its content: an HTML page as markdown, and a "
			"JSON / CSV / XML / plain-text answer verbatim. Use this to READ a page or to "
			"call a keyless read-only HTTP API you have the URL for. "
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
			text = result.body.decode("utf-8", errors="replace")
		except Exception:
			text = result.body.decode("latin-1", errors="replace")
		if kind == "text":
			# JSON/CSV/XML/plain: hand back what the endpoint said, verbatim.
			return render_text(text, max_chars=params.max_chars)
		return render_html_to_markdown(text, max_chars=params.max_chars)
