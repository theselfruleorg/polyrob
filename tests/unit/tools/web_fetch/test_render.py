from tools.web_fetch.render import render_html_to_markdown, classify_content, SPA_SHELL_SIGNAL


def test_strips_script_and_converts():
	# Body must clear the SPA-shell text threshold (>=200 readable chars).
	para = "Body text here. " + ("This article has real readable content about cats and dogs. " * 6)
	html = (f"<html><head><style>x{{}}</style></head><body><h1>Title</h1>"
	        f"<script>evil()</script><p>{para}</p></body></html>")
	md = render_html_to_markdown(html)
	assert "Title" in md and "Body text here." in md
	assert "evil" not in md and "x{}" not in md


def test_spa_shell_detected():
	html = "<html><body><div id='root'></div><script>var a=1;</script></body></html>"
	assert render_html_to_markdown(html) == SPA_SHELL_SIGNAL


def test_head_truncation_marker():
	body = "<p>" + ("word " * 20000) + "</p>"
	md = render_html_to_markdown(f"<html><body>{body}</body></html>", max_chars=500)
	assert "truncated" in md and len(md) <= 600


def test_classify_pdf_by_content_type():
	assert classify_content("application/pdf", b"%PDF-1.7 ...") == "pdf"


def test_classify_pdf_by_magic_bytes():
	assert classify_content("application/octet-stream", b"%PDF-1.4 ...") == "pdf"


def test_classify_html():
	assert classify_content("text/html; charset=utf-8", b"<html>") == "html"


def test_classify_binary():
	assert classify_content("image/png", b"\x89PNG\r\n") == "binary"


# --- D1: a JSON API answer is DATA, not "binary". -------------------------
# Before this, classify_content mapped application/json to "binary" and the tool
# answered "cannot render as markdown", so every keyless JSON API had to be
# reached through a third-party text proxy (r.jina.ai) or through code_execution.

def test_classify_json_is_text():
	assert classify_content("application/json", b'{"a":1}') == "text"


def test_classify_json_with_charset_is_text():
	assert classify_content("application/json; charset=utf-8", b'{"a":1}') == "text"


def test_classify_json_api_subtype_is_text():
	assert classify_content("application/vnd.api+json", b'{"a":1}') == "text"


def test_classify_plain_text_is_text_not_html():
	# Plain text used to be routed through markdownify, which mangles it.
	assert classify_content("text/plain; charset=utf-8", b"hello") == "text"


def test_classify_csv_is_text():
	assert classify_content("text/csv", b"a,b\n1,2") == "text"


def test_classify_xml_is_text():
	assert classify_content("application/xml", b"<rss/>") == "text"


def test_classify_image_is_still_binary():
	assert classify_content("image/png", b"\x89PNG\r\n") == "binary"


def test_render_text_passes_body_through_verbatim():
	from tools.web_fetch.render import render_text
	body = '{"pairs":[{"liquidity":{"usd":123.45}}]}'
	assert render_text(body) == body


def test_render_text_truncates_with_the_same_marker():
	from tools.web_fetch.render import render_text
	out = render_text("x" * 1000, max_chars=100)
	assert out.startswith("x" * 100)
	assert "900 chars truncated" in out
