# Packs

A **pack** is an optional extension that adds tools, skills, CLI commands or a
chat surface to POLYROB. It is an entry point in the `polyrob.packs` group with a
`pack.toml` manifest that declares what it contributes. Core never imports a pack
by name.

There are two tiers:

| Tier | What it is | Loads by default |
|---|---|---|
| **first-party** | Reviewed and released with core, and shipped INSIDE the `polyrob` distribution: `discovery` (AnySite, Perplexity), `x` (X/Twitter) and `markets` (Polymarket, Hyperliquid). There is nothing separate to install. | Yes |
| **third-party** | Its own distribution. | Only when you name it in `POLYROB_PACKS` |

### A first-party pack's SDKs

The packs ship with polyrob; their SDKs are optional extras:

| Pack | Extra | Without it |
|---|---|---|
| `x` | `pip install 'polyrob[twitter]'` | the `twitter` tool is withheld (`x_browser` needs the `browser` extra) |
| `discovery` | `pip install 'polyrob[anysite]'` | the `anysite` tool installs it on first use where lazy installs are on, else it is withheld |
| `markets` | `pip install 'polyrob[crypto]'` | the `polymarket` and `hyperliquid` trade tools are withheld; the `*_data` read tools work |

A withheld tool is never offered to the agent, and every surface names the remedy:

```text
x 1.2.0 (first-party): loaded — needs `pip install 'polyrob[twitter]'` (tools withheld: twitter)
```

From a source tree, `install.sh --packs x,markets` installs the extras
hash-checked; `polyrob pack install <id>` names the extra (or installs it through
the trusted lazy installer when one ships for it).

## See what is installed

```bash
polyrob pack list            # every installed pack and its state
polyrob pack info x          # what a pack declares: capabilities, tools, actions, CLI
polyrob pack doctor          # refusals with their reasons; exits 1 when a pack is refused
```

A pack is `loaded`, `disabled` or `refused`. A refused pack is never silently
absent: `list`, `doctor` and the status report name it with the reason.

## Enable and disable

```bash
polyrob pack disable x       # writes POLYROB_PACKS_DISABLED
polyrob pack enable x        # removes it again (a third-party pack is added to POLYROB_PACKS)
```

The setting applies to a new process. A running process keeps its pack state.

## The pack index

`polyrob pack install <id>` resolves only ids in the curated pack index. The
index ships with core and is read locally; search needs no network:

```bash
polyrob pack search              # every indexed pack
polyrob pack search twitter      # match on id, distribution, summary or capability
```

Each row names the pack id, distribution, pinned version, tier, summary,
declared capabilities, the core versions it needs, and a homepage. First-party
rows are generated from the packs in the POLYROB repository. A third-party row
is a reviewed pull request with a pinned version and a wheel sha256; a new
version is a new reviewed row.

## Install

```bash
polyrob pack install discovery
```

A first-party pack is already installed with polyrob; this command installs, or
names, its SDK extra. A third-party index row installs its exact wheel with
`--require-hashes`.

⚠️ The separate distribution names the first-party packs had in 1.1.0 are retired
and were never published: a package index may give them to anybody. POLYROB never
loads a distribution with one of those names, `polyrob pack install` refuses a
third-party source that uses one, and `polyrob update` (like `install.sh` and the
production deploy) retires a leftover one: its install metadata goes, never a pack
file. To do it by hand: `python -m core.packs.retire`. Do not `pip uninstall` it —
that would delete files polyrob now owns.

### Third-party packs from git or a path

```bash
polyrob pack install git+https://github.com/<owner>/<repo>@<40-hex-commit>
polyrob pack install ./my-pack
```

- A git install must pin a full 40-character commit. A branch or tag is refused
  because it can move.
- The clone uses the same hardened path as a git skill install: a scrubbed
  environment, no hooks, no credential prompt, no submodules, no symlinks, and
  size and file caps.
- The command shows the pack's declared capabilities and tools. You must accept
  them: answer the prompt, or pass `--accept-capabilities`.
- The pack installs with `pip --no-deps`. Its declared dependencies are printed,
  not installed. Install them yourself after you read them.
- A source that declares `tier = "first-party"` is refused. First-party packs
  install from the index.
- Load it with `polyrob pack enable <id>`.

⚠️ **Custody rule.** A third-party pack runs inside the agent process and can
read everything the agent can read. While this process holds wallet custody
(a wallet is enabled and `WALLET_SIGNER` is not `remote`), POLYROB refuses to
install or load a third-party code pack. The remedy is to move signing to
`polyrob-signer` (`WALLET_SIGNER=remote`), or to run the pack on an instance
without signing credentials. Third-party skills (markdown) and MCP servers are
not code packs and stay allowed.

## The kill list

The kill list names pack versions that must not run (for example a release that
turned out to be malicious or broken). Each row has the pack id (and optionally
the distribution), a version range, a reason and a date. It ships with core, is
read locally, and is enforced:

- when the pack loads — before any of its code is imported, even for a pack you
  installed with plain `pip`;
- by `polyrob pack enable`;
- by `polyrob pack install`, for index ids, git and path sources.
- by `polyrob update`, which skips a killed pack by name instead of reinstalling it.

A killed pack shows as `refused` with the reason. An unreadable kill list refuses
packs rather than allowing them.

## Remove

```bash
polyrob pack remove acme     # prints the pip uninstall command of a third-party pack
```

Removal is never automatic. A first-party pack ships inside polyrob and cannot be
uninstalled on its own: disable it (`polyrob pack disable x`).
