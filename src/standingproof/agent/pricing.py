from dataclasses import dataclass

from standingproof.agent.types import Usage


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write_multiplier: float = 1.25


# List prices as of 2026-11-03. A model missing from this table is an error, so a new
# model can never run unmetered.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.10),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01),
}

_PER_MILLION = 1_000_000


class UnknownModelError(LookupError):
    pass


def cost_usd(model: str, usage: Usage) -> float:
    try:
        price = PRICES[model]
    except KeyError:
        raise UnknownModelError(f"no price recorded for model {model!r}") from None
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_creation_input_tokens * price.input * price.cache_write_multiplier
        + usage.cache_read_input_tokens * price.cache_read
    ) / _PER_MILLION
