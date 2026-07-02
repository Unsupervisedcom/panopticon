"""Cost-weighted token accounting — convert raw per-tier counts to input-equivalent tokens.

Anthropic bills each tier at a different rate relative to uncached input. A raw four-tier sum
(what claude reports in ``usage``) is dominated by cheap cache-read tokens and is therefore a
poor cost signal and an even worse estimate target. This module provides the weight table and
the conversion function used by the Stop hook to report a **cost-weighted** total.

Weights are relative to uncached input = 1×. The structure is keyed by model-name prefix so
future per-model differentiation requires only a new entry, not a change to callers.
"""

from __future__ import annotations

#: Cost weights per token tier, keyed by model-name prefix, relative to uncached input = 1×.
#: Based on Anthropic published pricing ratios; cache-write depends on TTL but ~1.25× is a
#: reasonable default. All current model families share the same ratios; the structure allows
#: future per-model differentiation without touching callers.
_WEIGHTS: dict[str, dict[str, float]] = {
    "claude-sonnet": {
        "input_tokens": 1.0,
        "output_tokens": 5.0,
        "cache_creation_input_tokens": 1.25,
        "cache_read_input_tokens": 0.1,
    },
    "claude-haiku": {
        "input_tokens": 1.0,
        "output_tokens": 5.0,
        "cache_creation_input_tokens": 1.25,
        "cache_read_input_tokens": 0.1,
    },
    "claude-opus": {
        "input_tokens": 1.0,
        "output_tokens": 5.0,
        "cache_creation_input_tokens": 1.25,
        "cache_read_input_tokens": 0.1,
    },
    "claude-fable": {
        "input_tokens": 1.0,
        "output_tokens": 5.0,
        "cache_creation_input_tokens": 1.25,
        "cache_read_input_tokens": 0.1,
    },
}

_DEFAULT_WEIGHTS = _WEIGHTS["claude-sonnet"]

# TODO(non-claude-agents): _WEIGHTS is Anthropic-specific. The planning prompts that reference
# these ratios (PlannedWorkflow.TOKEN_ESTIMATED and orchestrator._SPAWN_TASK_INSTRUCTIONS step 4)
# will need to be generalised — or made backend-aware — when non-Claude LLM agents are supported.


def tier_weights(model: str | None) -> dict[str, float]:
    """Return the cost-weight table for ``model`` (longest prefix match; Sonnet as fallback)."""
    if model:
        for prefix, weights in _WEIGHTS.items():
            if model.startswith(prefix):
                return weights
    return _DEFAULT_WEIGHTS


def cost_weighted_tokens(usage: dict[str, int], model: str | None = None) -> int:
    """Convert raw per-tier usage counts to cost-weighted input-equivalent tokens.

    Each tier is multiplied by its weight relative to uncached input (1×), then summed and
    rounded to the nearest integer. Missing keys are treated as 0."""
    weights = tier_weights(model)
    return round(sum(usage.get(k, 0) * w for k, w in weights.items()))
