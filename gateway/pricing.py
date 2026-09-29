"""Equivalent standard text-token cost, using each request's context length."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone


def price_period(timestamp: float) -> int:
    local = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
    return int(local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18))


def price_usage(pricing: dict, model: str, context_tokens: int, inputs: int, outputs: int, period: int = -1) -> dict:
    result = {"equivalent_cny": 0.0, "unpriced_input_tokens": 0, "unpriced_output_tokens": 0}
    configured = pricing["models"].get(model, {})
    tiers = configured.get("tiers", [])
    if configured.get("peak"):
        rates = configured["peak"] if period == 1 else configured if period == 0 else {}
    elif tiers:
        # Historical daily totals have no trustworthy individual context length.
        rates = next((tier for tier in tiers if 0 <= context_tokens <= tier["max_input_tokens"]), {})
    else:
        rates = {**pricing["default"], **configured}
    for side, tokens in (("input", inputs), ("output", outputs)):
        rate = rates.get(side)
        if rate is None:
            result[f"unpriced_{side}_tokens"] += tokens
        else:
            result["equivalent_cny"] += tokens * rate / 1_000_000
    return result
