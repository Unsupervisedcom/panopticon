"""Unit tests for the cost-weighted token pricing module."""

from panopticon.container import pricing


def test_tier_weights_returns_sonnet_weights_for_exact_match() -> None:
    w = pricing.tier_weights("claude-sonnet-4-6")
    assert w["input_tokens"] == 1.0
    assert w["output_tokens"] == 5.0
    assert w["cache_creation_input_tokens"] == 1.25
    assert w["cache_read_input_tokens"] == 0.1


def test_tier_weights_matches_by_prefix() -> None:
    assert pricing.tier_weights("claude-opus-4-8") is pricing._WEIGHTS["claude-opus"]
    assert pricing.tier_weights("claude-haiku-4-5-20251001") is pricing._WEIGHTS["claude-haiku"]
    assert pricing.tier_weights("claude-fable-5") is pricing._WEIGHTS["claude-fable"]


def test_tier_weights_falls_back_to_default_for_unknown_model() -> None:
    assert pricing.tier_weights("gpt-4o") is pricing._DEFAULT_WEIGHTS
    assert pricing.tier_weights(None) is pricing._DEFAULT_WEIGHTS
    assert pricing.tier_weights("") is pricing._DEFAULT_WEIGHTS


def test_cost_weighted_tokens_applies_weights() -> None:
    # line 1 from test_hooks.py fixture: input=100, output=50, cache_write=10, cache_read=5
    # 100×1.0 + 50×5.0 + 10×1.25 + 5×0.1 = 100 + 250 + 12.5 + 0.5 = 363
    assert (
        pricing.cost_weighted_tokens(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 5,
            }
        )
        == 363
    )


def test_cost_weighted_tokens_second_fixture_line() -> None:
    # line 2: input=200, output=20, cache_read=300
    # 200×1.0 + 20×5.0 + 300×0.1 = 200 + 100 + 30 = 330
    assert (
        pricing.cost_weighted_tokens(
            {"input_tokens": 200, "output_tokens": 20, "cache_read_input_tokens": 300}
        )
        == 330
    )


def test_cost_weighted_tokens_missing_keys_are_zero() -> None:
    assert pricing.cost_weighted_tokens({}) == 0
    assert pricing.cost_weighted_tokens({"output_tokens": 10}) == 50


def test_cost_weighted_tokens_uses_model_weights() -> None:
    usage = {"input_tokens": 100, "output_tokens": 10}
    # With default (Sonnet) weights: 100×1 + 10×5 = 150
    assert pricing.cost_weighted_tokens(usage) == 150
    assert pricing.cost_weighted_tokens(usage, "claude-sonnet-4-6") == 150
