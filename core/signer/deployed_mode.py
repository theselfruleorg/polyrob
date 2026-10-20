"""``python -I -m core.signer.deployed_mode [SHARED AGENT]`` — print the agent
units' ``WALLET_SIGNER`` as the deployment declares it, or nothing (085 W8).

The reader is :func:`core.signer.declared_mode` (one reader of
``/etc/polyrob/polyrob.env`` + ``/etc/polyrob/agent-signer.env``); this module is
only the deploy's entry point to it.
"""
import sys


def main(argv=None) -> int:
    from core.signer import declared_mode
    args = list(sys.argv[1:] if argv is None else argv)
    shared = args[0] if len(args) > 0 else None
    agent = args[1] if len(args) > 1 else None
    print(declared_mode(shared, agent) or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
