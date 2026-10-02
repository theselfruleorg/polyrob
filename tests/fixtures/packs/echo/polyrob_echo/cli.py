"""``polyrob echo <text>`` — the fixture pack's CLI command."""
import click


@click.command("echo")
@click.argument("text")
def echo(text):
    """Echo TEXT (fixture pack)."""
    click.echo(f"echo: {text}")
