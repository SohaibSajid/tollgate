"""Direct provider request/response mapping (pure functions, no network)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
import direct_providers as d  # noqa: E402

MSGS = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hello"},
    {"role": "user", "content": "Explain holds"},
]
PRICE = {"input_rate": 750_000, "output_rate": 4_500_000, "cached_rate": 75_000, "cache_write_rate": 0}


class DirectProviderTests(unittest.TestCase):
    def test_openai_request_and_usage(self):
        url, headers, body = d.build_request("openai", "gpt-up", MSGS, 64, "KEY")
        self.assertTrue(url.endswith("/v1/chat/completions"))
        self.assertEqual((headers["Authorization"], body["max_completion_tokens"], body["model"]), ("Bearer KEY", 64, "gpt-up"))
        text, usage = d.parse_response("openai", {"choices": [{"message": {"content": "ok"}}], "usage": {
            "prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 40}}})
        self.assertEqual((text, usage["cached_tokens"], usage["prompt_tokens"]), ("ok", 40, 100))

    def test_anthropic_system_roles_and_input_total(self):
        _url, headers, body = d.build_request("anthropic", "claude-up", MSGS, 64, "KEY")
        self.assertEqual(body["system"], "Be brief.")
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        text, usage = d.parse_response("anthropic", {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 20}})
        self.assertEqual(text, "ab")
        self.assertEqual((usage["prompt_tokens"], usage["cached_tokens"], usage["cache_write_tokens"]), (60, 30, 20))

    def test_gemini_roles_and_thinking_counted_as_output(self):
        url, headers, body = d.build_request("gemini", "gem-up", MSGS, 64, "KEY")
        self.assertIn("gem-up:generateContent", url)
        self.assertEqual(headers, {"x-goog-api-key": "KEY"})
        self.assertEqual([c["role"] for c in body["contents"]], ["user", "model", "user"])
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "Be brief.")
        text, usage = d.parse_response("gemini", {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": "shown"}]}}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7, "thoughtsTokenCount": 30}})
        self.assertEqual((text, usage["completion_tokens"]), ("shown", 37))

    def test_missing_usage_raises(self):
        with self.assertRaises(ValueError):
            d.parse_response("openai", {"choices": [{"message": {"content": "x"}}]})
        with self.assertRaises(ValueError):
            d.build_request("simulator", "m", MSGS, 8, "K")

    def test_cost_matches_ledger_formula_and_worst_case_is_an_upper_bound(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 180, "cached_tokens": 0, "cache_write_tokens": 0}
        self.assertEqual(d.cost_micro(usage, PRICE), 1560)
        self.assertGreaterEqual(d.worst_case_micro([{"role": "user", "content": "x" * 2000}], 256, PRICE), 1560 // 2)
        small = d.worst_case_micro([{"role": "user", "content": "hello"}], 64, PRICE)
        self.assertGreater(small, d.cost_micro({"prompt_tokens": 5, "completion_tokens": 64, "cached_tokens": 0, "cache_write_tokens": 0}, PRICE) - 1)


if __name__ == "__main__":
    unittest.main()
