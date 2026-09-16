# Migrating to POLYROB

Coming from another agent framework. Each guide maps that project's concepts onto
POLYROB's and names what does and does not carry over.

| From | Guide |
|---|---|
| Hermes Agent | [from-hermes.md](from-hermes.md) |
| OpenClaw | [from-openclaw.md](from-openclaw.md) |

Still deciding? [comparison.md](../../comparison.md) is the feature-by-feature
comparison.

**Moving between POLYROB versions is a different job** — see
[upgrading.md](../upgrading.md).

## Before you switch

1. Install and run `polyrob doctor` — it names anything missing before you commit.
2. Write down what you actually rely on today: skills, scheduled jobs, connected
   channels, credentials.
3. Migrate one non-critical task first and compare the output.
4. Keep the old agent running until POLYROB has answered for a week.

Help: [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues) ·
[Discussions](https://github.com/theselfruleorg/polyrob/discussions)
