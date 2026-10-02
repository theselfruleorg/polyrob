import subprocess
import sys


def test_web_fetch_imports_when_playwright_absent():
	# Simulate a lean install (no Playwright/Chromium): block `import playwright`, then
	# import the web_fetch tool. tools/__init__ fail-opens the optional browser tool, so
	# web_fetch must still import and be usable. Subprocess keeps this process clean.
	code = (
		"import sys\n"
		"sys.modules['playwright'] = None\n"  # makes any `import playwright*` raise ImportError
		"import tools.web_fetch\n"
		"assert tools.web_fetch.WebFetchTool is not None\n"
		"print('OK')\n"
	)
	result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
	assert result.returncode == 0 and "OK" in result.stdout, result.stderr


def test_web_fetch_source_is_playwright_free():
	# The web_fetch package must never depend on playwright in its own code.
	import os
	import tools.web_fetch as pkg
	pkg_dir = os.path.dirname(pkg.__file__)
	for fname in os.listdir(pkg_dir):
		if fname.endswith(".py"):
			with open(os.path.join(pkg_dir, fname), encoding="utf-8") as fh:
				assert "playwright" not in fh.read().lower(), f"{fname} references playwright"


def test_requirements_has_no_top_level_playwright():
	# 066 P1: requirements.txt is the GENERATED hashed closure of PROD_EXTRAS.
	# Playwright may appear there only because the server set names `browser`;
	# the base install (pyproject dependencies) never carries it.
	import re
	import tomllib
	with open("requirements.txt", "r", encoding="utf-8") as fh:
		text = fh.read()
	extras = re.search(r"^# extras: ([a-z0-9,\-]+)", text, re.M).group(1).split(",")
	lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
	if any(l.lower().startswith("playwright") for l in lines):
		assert "browser" in extras
	with open("pyproject.toml", "rb") as fh:
		base = tomllib.load(fh)["project"]["dependencies"]
	assert not any(d.lower().startswith("playwright") for d in base)
