<!-- part: head -->
Web access — pick the lightest tool that does the job:
<!-- part: browser_read -->
- READ a page you have the URL for    -> web_fetch_fetch_url(url) returns the page as markdown (fast, no browser)
<!-- part: browser_search -->
- SEARCH / synthesize / known sources  -> {sources}
<!-- part: browser_search_perplexity -->
perplexity (web search)
<!-- part: browser_search_anysite -->
anysite (200+ structured sources)
<!-- part: browser_interact -->
- INTERACT (login, click, type, forms, paginate, JS-rendered apps) -> the browser actions below
<!-- part: browser_actions -->


Browser actions (only when interaction is required):
- browser_go_to_url(url): Navigate to URL
- browser_click_element(index): Click element by index
- browser_input_text(index, text): Type text into element
- browser_extract_page_content(): Get page content as text/markdown

Best Practices:

<!-- part: tip_fetch -->
Default to web_fetch_fetch_url for plain reading — it is much cheaper than launching a browser
<!-- part: tip_shell -->
If web_fetch_fetch_url reports the page is a JS-rendered shell, switch to the browser tool
<!-- part: tip_browser -->
With the browser: handle cookie popups first, wait for dynamic content, check indices before clicking
<!-- part: read -->
- READ a page you have the URL for -> web_fetch_fetch_url(url) returns the page as markdown (fast, no browser)
<!-- part: search -->
- SEARCH / synthesize across the web -> perplexity_search
<!-- part: anysite -->
- Structured data from known platforms -> anysite_api
<!-- part: interact_missing -->
- INTERACT (login, click, type, forms, JS-rendered apps) -> needs the browser tool, which is not loaded this session. If a task requires it, name that gap and use the remedy the tool-availability note lists for it, if any.
