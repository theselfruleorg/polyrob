"""A public blockchain address is not a credential (D2).

`KV_RE` claims the bare key name `token`, and a contract address is exactly the
value shape it redacts. So `  token: 0x…` — the line `launchpad_launch` returns
on success — became `token: <secret>redacted</secret>` in the persisted history
the agent reads back. In prod the agent recorded "address redacted in tool
output" for its own freshly deployed token and spent a later turn recovering the
address from a third-party indexer.

An address is public by construction: it is what you give people so they can pay
you. Redacting it protects nothing and loses the one fact the transaction
created.
"""
from core.secret_patterns import apply_ssot_shapes
from core.secret_scrub import scrub_secret_shapes

EVM_ADDR = "0xC371aB9d1f4bF3aE0a5c9C1d2E3f4A5b6C79aE6A"
SOL_MINT = "So11111111111111111111111111111111111111112"


def test_launch_success_line_keeps_the_token_address():
	line = f"  token: {EVM_ADDR}"
	assert apply_ssot_shapes(line) == line


def test_persisted_scrub_keeps_the_token_address():
	line = f"  token: {EVM_ADDR}"
	assert scrub_secret_shapes(line) == line


def test_token_equals_address_is_kept_too():
	line = f"token={EVM_ADDR}"
	assert apply_ssot_shapes(line) == line


def test_token_keeps_a_solana_mint():
	line = f"token: {SOL_MINT}"
	assert apply_ssot_shapes(line) == line


# --- the exemption must NOT widen the hole -------------------------------

def test_a_real_bearer_style_token_is_still_redacted():
	out = apply_ssot_shapes("token: ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345")
	assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in out


def test_access_token_with_an_address_shaped_value_is_still_redacted():
	# `access_token` is never a contract address; only the bare key `token` is
	# ambiguous in a crypto context.
	out = apply_ssot_shapes(f"access_token: {EVM_ADDR}")
	assert EVM_ADDR not in out


def test_api_key_with_an_address_shaped_value_is_still_redacted():
	out = apply_ssot_shapes(f"api_key: {EVM_ADDR}")
	assert EVM_ADDR not in out


def test_prefixed_token_key_is_still_redacted():
	out = apply_ssot_shapes(f"BOT_TOKEN: {EVM_ADDR}")
	assert EVM_ADDR not in out


def test_a_private_key_shaped_value_is_still_redacted():
	# 0x + 64 hex is an EVM PRIVATE KEY, not an address. Never exempt it.
	priv = "0x" + "a1b2c3d4" * 8
	out = apply_ssot_shapes(f"token: {priv}")
	assert priv not in out


def test_a_short_hex_blob_is_still_redacted():
	blob = "0x" + "ab" * 10  # 20 hex chars — not an address
	out = apply_ssot_shapes(f"token: {blob}")
	assert blob not in out
