"""Sectioned ``--help`` for click subcommand groups (proposal 030 WS-C5 / D7).

``GroupedGroup`` mirrors the top-level grouped help in ``cli/polyrob.py``
(``_LazyGroup.format_commands``, 027 WP6): commands render under named
sections instead of one flat alphabetical wall (`halt` sorted mid-list).
Anything not named in a section lands in a computed "Other" section, so a
new subcommand can never silently vanish from ``--help``. Hidden commands
stay hidden. Import cost: click only — safe for lazy-loaded command modules.
"""
import click


class GroupedGroup(click.Group):
    """Click group whose ``--help`` lists commands in named sections.

    ``help_sections`` is a list of ``(title, [command_name, ...])`` pairs,
    rendered in order. Commands absent from every section render under a
    trailing computed "Other" section (alphabetical).
    """

    def __init__(self, *args, help_sections=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.help_sections = list(help_sections or [])

    def format_commands(self, ctx, formatter):
        available = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            available[name] = cmd
        if not available:
            return

        listed = set()
        sections = list(self.help_sections)
        leftover = [n for n in sorted(available)
                    if not any(n in names for _, names in sections)]
        if leftover:
            sections.append(("Other", leftover))

        limit = formatter.width - 6 - max(len(n) for n in available)
        for title, names in sections:
            rows = []
            for name in names:
                if name not in available or name in listed:
                    continue
                listed.add(name)
                rows.append((name, available[name].get_short_help_str(limit=limit)))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)
