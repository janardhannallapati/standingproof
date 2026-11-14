from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """Hard ceilings for one run, enforced in code before every model call."""

    max_steps: int = 8
    max_cost_usd: float = 0.50
    max_seconds: float = 120.0
