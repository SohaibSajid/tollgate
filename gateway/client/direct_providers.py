"""Direct provider calls for the emergency tier (pure functions; no network, no third-party imports).

Used only when Tollgate and the standby relay are unreachable. Each function builds the request
for one provider's public API and normalises its answer to the same usage shape Tollgate bills:
prompt_tokens (all input), completion_tokens (all output, incl. thinking), cached_tokens, cache_write_tokens.
"""

import math

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MICRO_PER_USD = 1_000_000


def _split(messages):
    system = "\n".join(m["content"] for m in messages if m["role"] in ("system", "developer"))
    turns = [m for m in messages if m["role"] in ("user", "assistant")]
    return system, turns


def build_request(provider, upstream_model, messages, max_tokens, api_key):
    """Return (url, headers, json_body) for one non-streaming generation."""
    system, turns = _split(messages)
    if provider == "openai":
        return (
            OPENAI_URL,
            {"Authorization": f"Bearer {api_key}"},
            {"model": upstream_model, "messages": messages, "max_completion_tokens": max_tokens},
        )
    if provider == "anthropic":
        body = {
            "model": upstream_model,
            "max_tokens": max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in turns],
        }
        if system:
            body["system"] = system
        return ANTHROPIC_URL, {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, body
    if provider == "gemini":
        body = {
            "contents": [
                {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in turns
            ],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return GEMINI_URL.format(model=upstream_model), {"x-goog-api-key": api_key}, body
    raise ValueError(f"No direct support for provider {provider!r}.")


def parse_response(provider, data):
    """Return (text, usage). Raise ValueError if the answer or its usage is missing."""
    try:
        if provider == "openai":
            u = data["usage"]
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            return data["choices"][0]["message"]["content"] or "", {
                "prompt_tokens": u["prompt_tokens"],
                "completion_tokens": u["completion_tokens"],
                "cached_tokens": cached,
                "cache_write_tokens": 0,
            }
        if provider == "anthropic":
            u = data["usage"]
            cached = u.get("cache_read_input_tokens", 0) or 0
            written = u.get("cache_creation_input_tokens", 0) or 0
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            return text, {
                "prompt_tokens": u["input_tokens"] + cached + written,
                "completion_tokens": u["output_tokens"],
                "cached_tokens": cached,
                "cache_write_tokens": written,
            }
        if provider == "gemini":
            u = data["usageMetadata"]
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            return text, {
                "prompt_tokens": u["promptTokenCount"],
                "completion_tokens": (u.get("candidatesTokenCount", 0) or 0) + (u.get("thoughtsTokenCount", 0) or 0),
                "cached_tokens": u.get("cachedContentTokenCount", 0) or 0,
                "cache_write_tokens": 0,
            }
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("The provider answer had no usable text or usage.") from exc
    raise ValueError(f"No direct support for provider {provider!r}.")


def cost_micro(usage, price):
    """Same integer, round-up-once formula the Tollgate ledger uses."""
    cached, written = usage["cached_tokens"], usage["cache_write_tokens"]
    numerator = (
        (usage["prompt_tokens"] - cached - written) * price["input_rate"]
        + cached * price["cached_rate"]
        + written * price["cache_write_rate"]
        + usage["completion_tokens"] * price["output_rate"]
    )
    return (numerator + MICRO_PER_USD - 1) // MICRO_PER_USD


def worst_case_micro(messages, max_tokens, price):
    """Conservative pre-call estimate so the allowance cannot be exceeded."""
    estimated_input = math.ceil(sum(len(m["content"]) for m in messages) / 2) + 8 * len(messages) + 16
    rate = max(price["input_rate"], price["cache_write_rate"])
    return (estimated_input * rate + max_tokens * price["output_rate"] + MICRO_PER_USD - 1) // MICRO_PER_USD
