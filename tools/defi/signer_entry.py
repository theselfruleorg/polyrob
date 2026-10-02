"""``python -I -m tools.defi.signer_entry`` — the ``polyrob-signer`` executable (066 P2).

The service itself is :mod:`core.signer`; it lives in the core tier and takes
its price oracle as an argument, so this entry point exists only to hand it the
SAME two price rules the money verbs use (``DefiTradeTool._price`` and
``_fallback_price``: DexScreener, a trustworthy price only), which live in the
tools tier. Nothing else is imported here.

Subcommands:

* ``serve --config /etc/polyrob/signer.toml`` — the unit's ``ExecStart``.
* ``check --config …`` — load the policy, derive the wallet, run the continuity
  check and print the public identity (provisioning runs it before enabling
  the unit). Never prints a key.
"""
import argparse
import json
import logging
import signal
import sys


def guard_price(chain, addr):
    """``DefiTradeTool._price``: only a HIGH-confidence price bounds a cap."""
    from tools.defi.price_sources import spend_price
    return spend_price(chain, addr)


def guard_fallback_price(chain, addr):
    """``DefiTradeTool._fallback_price``: a price with measured liquidity."""
    from tools.defi.price_sources import exit_price
    return exit_price(chain, addr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="polyrob-signer")
    parser.add_argument("command", choices=("serve", "check"))
    parser.add_argument("--config", default="/etc/polyrob/signer.toml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from core.signer.caps import SignerConfigError, load_signer_config
    from core.signer import runtime
    try:
        if args.command == "check":
            cfg = load_signer_config(args.config)
            runtime.prepare_process(cfg)
            service = runtime.build_service(cfg, price_fn=guard_price,
                                            fallback_price_fn=guard_fallback_price)
            print(json.dumps({"ok": True, "identity": service.identity(),
                              "caps": cfg.summary()}, indent=2))
            return 0
        service, server = runtime.start(args.config, price_fn=guard_price,
                                        fallback_price_fn=guard_fallback_price)
    except (SignerConfigError, runtime.SignerStartupError) as exc:
        print(f"polyrob-signer: refusing to start: {exc}", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    server.bind()
    logging.getLogger("polyrob-signer").info(
        "listening on %s; treasury %s", server.path, runtime.identity_summary(service)["treasury"])
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
