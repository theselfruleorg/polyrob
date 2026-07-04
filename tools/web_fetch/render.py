"""Pure HTML->markdown rendering + content-type classification for web_fetch."""

from bs4 import BeautifulSoup
from markdownify import markdownify

SPA_SHELL_SIGNAL = (
	"[web_fetch: this page returned almost no readable text — it is likely a "
	"JavaScript-rendered single-page app. Use the 'browser' tool (tool_ids=['browser']) "
	"if you need its rendered content or interaction.]"
)

_BOILERPLATE_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript")
_MIN_TEXT_CHARS = 200


def classify_content(content_type: str, body: bytes) -> str:
	"""Return ``"html"``, ``"pdf"``, or ``"binary"`` (content-type first, magic-byte fallback)."""
	ct = (content_type or "").lower()
	if "html" in ct or "xhtml" in ct:
		return "html"
	if "pdf" in ct or body[:5] == b"%PDF-":
		return "pdf"
	if "text/plain" in ct:
		return "html"  # treat plain text as renderable passthrough
	return "binary"


def render_html_to_markdown(html: str, max_chars: int = 40_000) -> str:
	"""Strip boilerplate, convert to markdown, head-truncate with a marker.

	Returns ``SPA_SHELL_SIGNAL`` when the page has almost no readable text (likely a
	client-rendered SPA), so the agent's escalation cue is explicit.
	"""
	soup = BeautifulSoup(html, "html.parser")
	for tag in soup(_BOILERPLATE_TAGS):
		tag.decompose()
	if len(soup.get_text(strip=True)) < _MIN_TEXT_CHARS:
		return SPA_SHELL_SIGNAL
	root = soup.body or soup
	md = markdownify(str(root)).strip()
	if len(md) > max_chars:
		dropped = len(md) - max_chars
		md = md[:max_chars] + f"\n\n[... {dropped} chars truncated ...]"
	return md
