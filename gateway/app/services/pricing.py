"""Exact integer money arithmetic and explicitly simulated token counting."""

import re

MICRO_PER_USD = 1_000_000
MAX_WALLET_MICRO = 1_000_000 * MICRO_PER_USD


def usd_to_micro(value: str) -> int:
    """Convert a decimal USD string to micro-USD without using floating point."""
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,6})?", value):
        raise ValueError(
            "Use a non-negative USD string with at most six decimal places."
        )
    whole, _, fraction = value.partition(".")
    amount = int(whole) * MICRO_PER_USD + int(fraction.ljust(6, "0") or "0")
    if amount > MAX_WALLET_MICRO:
        raise ValueError("Amount exceeds the local wallet limit of 1,000,000 USD.")
    return amount


def micro_to_usd(value: int) -> str:
    """Return a six-decimal display string, preserving every stored micro-USD."""
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), MICRO_PER_USD)
    return f"{sign}{whole}.{fraction:06d}"


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    input_rate: int,
    output_rate: int,
    cached_tokens: int = 0,
    cached_rate: int = 0,
    cache_write_tokens: int = 0,
    cache_write_rate: int = 0,
) -> int:
    """Apply micro-USD-per-million rates and round up once per request."""
    if min(input_tokens, output_tokens, input_rate, output_rate) < 0:
        raise ValueError("Usage and rates cannot be negative.")
    if (
        min(cached_tokens, cache_write_tokens, cached_rate, cache_write_rate) < 0
        or cached_tokens + cache_write_tokens > input_tokens
    ):
        raise ValueError("Cached usage must be a non-negative subset of input usage.")
    numerator = (
        (input_tokens - cached_tokens - cache_write_tokens) * input_rate
        + cached_tokens * cached_rate
        + cache_write_tokens * cache_write_rate
        + output_tokens * output_rate
    )
    return (numerator + MICRO_PER_USD - 1) // MICRO_PER_USD


def simulated_input_tokens(messages) -> int:
    """Match our simulator's test counting rule; this is not a real-model tokenizer."""
    return 4 + sum(
        3 + (len(message.content.encode("utf-8")) + 3) // 4 for message in messages
    )
