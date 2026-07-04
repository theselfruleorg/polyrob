import inspect

import tools.filesystem_docproc as fd


def test_unsafe_fetchers_removed():
	src = inspect.getsource(fd)
	assert "def process_url" not in src
	assert "def process_web_content" not in src
	# the SSRF-unsafe pattern (follow arbitrary redirects, no per-hop validation) is gone
	assert "allow_redirects=True" not in src


def test_module_still_imports():
	# core document-processing actions remain intact
	assert hasattr(fd, "DocProcessingMixin")
	assert hasattr(fd.DocProcessingMixin, "process_document")
	assert hasattr(fd.DocProcessingMixin, "analyze_document")
