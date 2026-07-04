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
