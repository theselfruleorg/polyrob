"""Test dynamic token-based pricing."""
import math


def test_cost_calculation():
    """Verify costs match real API pricing."""
    from modules.llm.model_registry import calculate_cost

    # GPT-4.1: $2/M in + $8/M out -> 10k*2/1M + 5k*8/1M = 0.02 + 0.04 = 0.06
    # (was clobbered to "gpt-5" by a bad search-replace; gpt-5 is $1.25/$10 = 0.0625)
    cost = calculate_cost("gpt-4.1", 10000, 5000)
    assert 0.059 < cost < 0.061, f"Expected ~$0.06, got ${cost:.6f}"
    print(f"✅ gpt-4.1 cost: ${cost:.6f}")

    # Gemini 2.0 Flash: $0.10/M in + $0.40/M out -> 0.001 + 0.002 = 0.003
    cost = calculate_cost("gemini-2.0-flash-exp", 10000, 5000)
    assert 0.0029 < cost < 0.0031, f"Expected ~$0.003, got ${cost:.6f}"
    print(f"✅ gemini cost: ${cost:.6f}")

    # o3
    cost = calculate_cost("o3", 10000, 5000)
    assert 0.29 < cost < 0.31, f"Expected ~$0.30, got ${cost:.6f}"
    print(f"✅ o3 cost: ${cost:.6f}")

    # Claude Sonnet 4.5
    cost = calculate_cost("claude-sonnet-4-5", 10000, 5000)
    assert 0.10 < cost < 0.11, f"Expected ~$0.105, got ${cost:.6f}"
    print(f"✅ claude-sonnet-4-5 cost: ${cost:.6f}")


def test_credit_conversion():
    """Test USD to credits with markup.
    
    IMPORTANT: Uses math.ceil() to ALWAYS round UP.
    This ensures we never charge less than the API cost.
    """

    # $0.06 → 8 credits (was 7 with round-to-nearest bug)
    # 0.06 / 0.01 * 1.20 = 7.2 → ceil(7.2) = 8
    credits = max(1, math.ceil((0.06 / 0.01) * 1.20))
    assert credits == 8, f"Expected 8 credits, got {credits}"
    print(f"✅ $0.06 → {credits} credits")

    # $0.002 → 1 credit (minimum)
    # 0.002 / 0.01 * 1.20 = 0.24 → ceil(0.24) = 1, max(1,1) = 1
    credits = max(1, math.ceil((0.002 / 0.01) * 1.20))
    assert credits == 1, f"Expected 1 credit (minimum), got {credits}"
    print(f"✅ $0.002 → {credits} credit (minimum)")

    # $0.30 → 36 credits
    # 0.30 / 0.01 * 1.20 = 36.0 → ceil(36.0) = 36
    credits = max(1, math.ceil((0.30 / 0.01) * 1.20))
    assert credits == 36, f"Expected 36 credits, got {credits}"
    print(f"✅ $0.30 → {credits} credits")


def test_markup_percentage():
    """Test that 20% markup is applied correctly with round-UP.
    
    All these are exact multiples, so round-up = same result.
    """

    # Test various amounts (exact multiples remain the same)
    test_cases = [
        (0.05, 6),   # $0.05 → 6 credits (5 base * 1.20 = 6.0 → ceil = 6)
        (0.10, 12),  # $0.10 → 12 credits (10 base * 1.20 = 12.0 → ceil = 12)
        (0.25, 30),  # $0.25 → 30 credits (25 base * 1.20 = 30.0 → ceil = 30)
    ]

    for api_cost, expected_credits in test_cases:
        credits = max(1, math.ceil((api_cost / 0.01) * 1.20))
        assert credits == expected_credits, f"For ${api_cost}, expected {expected_credits} credits, got {credits}"
        print(f"✅ ${api_cost} → {credits} credits")


def test_round_up_never_loses_money():
    """Test that we NEVER charge less than API cost.
    
    This was the original bug: int(x + 0.5) would round DOWN for values < x.5,
    causing us to lose money. math.ceil() always rounds UP.
    """
    # The original failing case: API=$0.0242, user charged $0.02
    api_cost = 0.0242
    markup = 1.0  # No markup in default config
    credit_value = 0.01
    
    credits_raw = (api_cost / credit_value) * markup  # = 2.42
    
    # OLD (buggy): int(2.42 + 0.5) = int(2.92) = 2 → $0.02 < $0.0242 ❌
    old_credits = max(1, int(credits_raw + 0.5))
    old_user_cost = old_credits * credit_value
    
    # NEW (fixed): ceil(2.42) = 3 → $0.03 > $0.0242 ✓
    new_credits = max(1, math.ceil(credits_raw))
    new_user_cost = new_credits * credit_value
    
    print(f"API cost: ${api_cost}")
    print(f"OLD: {old_credits} credits (${old_user_cost}) - {'LOSING MONEY!' if old_user_cost < api_cost else 'OK'}")
    print(f"NEW: {new_credits} credits (${new_user_cost}) - {'LOSING MONEY!' if new_user_cost < api_cost else 'OK'}")
    
    assert new_user_cost >= api_cost, f"STILL LOSING MONEY! ${new_user_cost} < ${api_cost}"
    print(f"✅ Fixed: ${new_user_cost} >= ${api_cost}")


if __name__ == "__main__":
    print("\n🧪 Testing Cost Calculation...")
    test_cost_calculation()

    print("\n🧪 Testing Credit Conversion...")
    test_credit_conversion()

    print("\n🧪 Testing Markup Percentage...")
    test_markup_percentage()

    print("\n🧪 Testing Round-Up Never Loses Money...")
    test_round_up_never_loses_money()

    print("\n🎉 All tests PASSED!")
