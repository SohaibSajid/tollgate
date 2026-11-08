"""Allow-listed models, price snapshots, and explicit operational limits."""

import json
import re
from dataclasses import dataclass
from datetime import date
from app.services.pricing import micro_to_usd, usd_to_micro


@dataclass(frozen=True)
class ModelSpec:
    id: str
    input_rate: int
    output_rate: int
    price_version: str = "sim-test-v1"
    max_input_tokens: int = 40_000
    max_output_tokens: int = 4_096
    provider: str = "simulator"
    upstream_model: str = ""
    cached_rate: int = 0
    cache_write_rate: int = 0
    reviewed_on: str = ""
    context_tokens: int = 200_000
    usage_output_limit: int = 0

    def output_bound(self, requested: int) -> int:
        """Allow Gemini's reported thinking tokens within its model output ceiling."""
        return self.usage_output_limit or requested

    def public(self) -> dict:
        return {
            "id": self.id,
            "object": "model",
            "owned_by": self.provider,
            "pricing_source": (
                "test_prices"
                if self.provider == "simulator"
                else (
                    "free_tier_assumed"
                    if self.provider == "gemini"
                    else "configured_provider_rates"
                )
            ),
            "price_version": self.price_version,
            "reviewed_on": self.reviewed_on,
            "input_usd_per_million": micro_to_usd(self.input_rate),
            "output_usd_per_million": micro_to_usd(self.output_rate),
            "cached_usd_per_million": micro_to_usd(self.cached_rate),
            "max_output_tokens": self.max_output_tokens,
            "max_input_tokens": self.max_input_tokens,
        }


MODELS = {
    "sim-basic": ModelSpec("sim-basic", 2_000_000, 8_000_000),
    "sim-deep": ModelSpec("sim-deep", 10_000_000, 40_000_000),
}


def load_models(settings) -> dict[str, ModelSpec]:
    """Load reviewed live rates; expose only providers with configured credentials."""
    if settings.mode == "simulator":
        return MODELS
    rows = json.loads(settings.model_catalog_path.read_text(encoding="utf-8"))
    result = {}
    for row in rows:
        if row["provider"] not in ("openai", "anthropic", "gemini"):
            raise ValueError("Unsupported provider in model catalog")
        if row["provider"] not in settings.enabled_providers:
            continue
        if not getattr(settings, row["provider"] + "_api_key").get_secret_value():
            continue
        spec = ModelSpec(
            id=row["id"],
            provider=row["provider"],
            upstream_model=row["upstream_model"],
            input_rate=usd_to_micro(row["input_usd_per_million"]),
            output_rate=usd_to_micro(row["output_usd_per_million"]),
            cached_rate=usd_to_micro(row["cached_usd_per_million"]),
            cache_write_rate=usd_to_micro(row.get("cache_write_usd_per_million", "0")),
            price_version=row["price_version"],
            reviewed_on=row["reviewed_on"],
            max_input_tokens=row["max_input_tokens"],
            max_output_tokens=row["max_output_tokens"],
            context_tokens=row["context_tokens"],
            usage_output_limit=row.get("usage_output_limit", 0),
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", spec.upstream_model):
            raise ValueError("Invalid upstream model identifier")
        if spec.provider == "gemini" and (
            spec.upstream_model != "gemini-3.5-flash-lite"
            or any(
                (
                    spec.input_rate,
                    spec.output_rate,
                    spec.cached_rate,
                    spec.cache_write_rate,
                )
            )
            or spec.usage_output_limit != 65536
        ):
            raise ValueError(
                "This Gemini adapter supports only reviewed Flash-Lite free-tier rates."
            )
        if (
            spec.id in result
            or not 1 <= spec.max_input_tokens <= 100_000
            or not 16 <= spec.max_output_tokens <= 16_384
        ):
            raise ValueError("Invalid or duplicate model limits")
        if (
            spec.cached_rate > spec.input_rate
            or spec.context_tokens < spec.max_input_tokens + spec.max_output_tokens
            or spec.usage_output_limit < 0
            or (
                spec.usage_output_limit
                and not spec.max_output_tokens <= spec.usage_output_limit <= 100_000
            )
        ):
            raise ValueError("Invalid price or context limits")
        date.fromisoformat(spec.reviewed_on)
        result[spec.id] = spec
    if not result:
        raise ValueError("Live mode requires at least one provider API key and model")
    return result


def price_is_current(spec: ModelSpec, max_age: int) -> bool:
    if spec.provider == "simulator":
        return True
    age = (date.today() - date.fromisoformat(spec.reviewed_on)).days
    return 0 <= age <= max_age
