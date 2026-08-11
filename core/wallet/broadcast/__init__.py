"""Transaction broadcast rails. Never called directly by a tool — every
state-changing transaction goes through core.wallet.tx_guard.authorize() first.
"""
