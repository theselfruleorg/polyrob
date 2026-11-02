# Research with the discovery tools

The core `web-research` and `market-research-brief` skills say "use the search
tool you hold". With this pack loaded, that is:

1. **perplexity** — synthesis of recent reports, analyst quotes and statistics
   (`perplexity_search`). Use it first when the key is set; if it is absent,
   say so and go on with the next tool.
2. **anysite** — structured sources (company databases such as Crunchbase, YC,
   LinkedIn company search; social and code platforms). Discover before you
   query: `anysite_describe(search=<keyword>)` lists endpoints,
   `anysite_describe(endpoint=<path>)` gives its params, then
   `anysite_api(endpoint=<path>, params={...})`. Never guess an endpoint path.
3. Then `web_fetch` for a known URL and `browser` only for a page that needs
   login, clicks or JS rendering.

For a market brief with no `perplexity`, combine `anysite` (company discovery for
the competitive landscape) with `web_fetch` (public reports and Wikipedia).
