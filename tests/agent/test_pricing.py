import pytest

from standingproof.agent.pricing import UnknownModelError, cost_usd
from standingproof.agent.types import Usage


def test_cost_counts_input_output_and_both_cache_kinds() -> None:
    used = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )

    # Opus 5.5: $4 input + $20 output + $5 cache write (1.25x input) + $0.20 cache read.
    assert cost_usd("claude-opus-5-5", used) == pytest.approx(29.20)


def test_unknown_model_is_an_error_not_a_free_call() -> None:
    with pytest.raises(UnknownModelError):
        cost_usd("some-future-model", Usage(input_tokens=1))
