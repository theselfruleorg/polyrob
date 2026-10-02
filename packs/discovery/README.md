# The discovery pack

A first-party POLYROB pack: the `anysite` tool (structured data from 200+
sources through the AnySite CLI) and the `perplexity` tool (web search through
the Perplexity API).

It ships inside the `polyrob` distribution: there is nothing separate to
install. Its SDKs (anysite-cli) are the `anysite` extra:

```bash
pip install 'polyrob[anysite]'
polyrob pack list                  # discovery ... loaded
```

Without the extra the pack still loads; the `anysite` tool installs it on first
use where lazy installs are on (`LAZY_DEPS_MODE`), and is otherwise withheld while
`polyrob pack list` names the remedy (`needs pip install 'polyrob[anysite]'`).
From a source tree, `install.sh --packs discovery` installs the extra hash-checked.

Configuration flags (`ANYSITE_TOOL_ENABLED`, `ANYSITE_API_KEY`,
`PERPLEXITY_API_KEY`) are documented in the core `docs/CONFIGURATION.md`.
