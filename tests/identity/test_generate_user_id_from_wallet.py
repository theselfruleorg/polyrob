from core.identity import generate_user_id_from_wallet


def test_golden_value_known_address():
    # Locked golden: usr_ + first 12 hex of sha256(lowercased address).
    # 0x0 address sha256 of "0x0000000000000000000000000000000000000000".
    addr = "0x0000000000000000000000000000000000000000"
    result = generate_user_id_from_wallet(addr)
    assert result == "usr_0fff9ee671b0"  # frozen golden: sha256("0x000...0")[:12]


def test_deterministic_and_case_insensitive():
    a = generate_user_id_from_wallet("0xAbCdEf0000000000000000000000000000000001")
    b = generate_user_id_from_wallet("0xabcdef0000000000000000000000000000000001")
    assert a == b
    assert a.startswith("usr_")
    assert len(a) == len("usr_") + 12


def test_backcompat_reexport_matches_core():
    from modules.x402.x402_integration import generate_user_id_from_wallet as legacy
    addr = "0x1234567890123456789012345678901234567890"
    assert legacy(addr) == generate_user_id_from_wallet(addr)
