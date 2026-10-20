<!-- part: off -->
MCP tools are not currently configured. If you need external data sources, use:
<!-- part: alt_browser -->
- Browser actions for web scraping
<!-- part: alt_perplexity -->
- perplexity_search for web research
<!-- part: alt_web_fetch -->
- web_fetch_fetch_url(url) to read a web page as markdown
<!-- part: alt_files -->
- Available file operations for local data
<!-- part: no_servers_listed -->
- Use mcp_list_tools() to discover available tools
<!-- part: retry_alternatives -->
Continue with available data or try alternative tools ({alternatives}).
<!-- part: retry_none -->
Continue with the data you already have.
<!-- part: on -->
MCP TOOLS - DIRECT CALLING (PREFERRED)

MCP tools are available as DIRECT actions with flat parameters.
Call them directly by their namespaced name: {{server}}_{{tool}}

EXAMPLES (use these patterns):
{examples}

Available MCP Tools (call directly):
{tools}

MCP Performance:
- MCP tools are SLOW (30-180 seconds each)
- Limit to 2-3 MCP actions per step
- Actions execute SEQUENTIALLY (6 searches = 3-18 minutes!)

MCP Parameter Types:
- Integers: count=10 (NOT "10")
- Booleans: with_skills=true (NOT "true")
- Arrays: companies=["company:123"] (NOT "company:123")

Strategy:
1. 2-3 targeted searches with specific query params
2. Scrape 2-3 best URLs
3. Process, then iterate

If you don't know what params a tool needs:
1. Use mcp_list_tools(server_name="{{server}}") to see the schema
2. Read the error message - it shows required params

MCP Timeout Handling:
Timeouts are usually CONFIGURATION issues, not retry-able.
{retry}
