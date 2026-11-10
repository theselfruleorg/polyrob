"""``sudo polyrob wallet signer-reconcile <chain>`` (098 W1 CO-22).

A send whose outcome was UNKNOWN leaves its nonce ``unknown`` in the signer's
journal; while it stands, every later EVM send on that chain is refused (the
nonce is either reused or ahead of the chain). This verb asks the signer (as
root, on the box) to decide each one from the chain: taken → ``sent``; the chain
still waits on it → ``released``, so the next send may use it.

⚠️ Run it with the owner present; it reads the chain and changes the signer's
journal. It never signs or sends anything.
"""
import json

import click

from cli.commands._options import json_option


@click.command("signer-reconcile")
@click.argument("chain")
@json_option
def signer_reconcile(chain, as_json):
    """Settle the signer's unknown-outcome nonces on CHAIN from the chain (root)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.signer.client import SignerClient, SignerRefused, SignerUnavailable
    try:
        out = SignerClient(timeout=60.0).call("nonce.reconcile", {"chain": str(chain)})
    except SignerRefused as exc:
        hint = " — run it as root on the box: sudo polyrob wallet signer-reconcile" \
            if exc.code == "not_root" else ""
        raise click.ClickException(f"the signer refused: {exc.reason}{hint}")
    except SignerUnavailable as exc:
        raise click.ClickException(f"polyrob-signer did not answer: {exc}")
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"{out['chain']}: chain next nonce {out['chain_next_nonce']}")
    if not out["nonces"]:
        click.echo("  no unknown nonce — nothing to reconcile")
    for row in out["nonces"]:
        tail = f"  (left: {row.get('why')})" if row.get("left") else ""
        who = f" ({row['address']})" if row.get("address") else ""
        click.echo(f"  nonce {row['nonce']}{who}: {row['state']}{tail}")
