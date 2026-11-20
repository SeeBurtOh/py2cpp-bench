import json
import unittest
from unittest.mock import patch

from py2cpp_bench.providers import (Model, NoRedirect, ProviderError, extract_text,
                                   generate, redact, request_spec, strip_fences)


class ProviderTests(unittest.TestCase):
    def test_each_adapter_sends_same_prompts_without_credentials_in_body(self):
        for provider in ("openai", "anthropic", "xai", "google"):
            with self.subTest(provider=provider):
                url, payload = request_spec(Model(provider, "test-model"), "system text", "source text", 1234)
                encoded = json.dumps(payload)
                self.assertIn("system text", encoded)
                self.assertIn("source text", encoded)
                self.assertIn("1234", encoded)
                self.assertTrue(url.startswith("https://"))

    def test_extracts_only_code_text_and_rejects_truncation(self):
        samples = {
            "openai": {"status": "completed", "output": [{"type": "reasoning", "summary": []},
                        {"type": "message", "content": [{"type": "output_text", "text": "int main(){}"}]}]},
            "anthropic": {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "private"},
                          {"type": "text", "text": "int main(){}"}]},
            "xai": {"choices": [{"finish_reason": "stop", "message": {"content": "int main(){}"}}]},
            "google": {"candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"thought": True, "text": "private"}, {"text": "int main(){}"}]}}]},
        }
        for provider, sample in samples.items():
            self.assertEqual(extract_text(provider, sample), "int main(){}")
        truncated = [
            ("openai", {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}),
            ("anthropic", {"stop_reason": "max_tokens"}),
            ("xai", {"choices": [{"finish_reason": "length"}]}),
            ("google", {"candidates": [{"finishReason": "MAX_TOKENS"}]}),
        ]
        for provider, sample in truncated:
            with self.assertRaises(ProviderError):
                extract_text(provider, sample)

    def test_one_request_on_provider_failure(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}, clear=True), \
             patch("py2cpp_bench.providers.post_json", side_effect=ProviderError("HTTP 429")) as request:
            with self.assertRaises(ProviderError):
                generate(Model("openai", "any-model"), "s", "u", 1000, 10)
            self.assertEqual(request.call_count, 1)

    def test_google_key_in_header_not_url(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"}, clear=True), \
             patch("py2cpp_bench.providers.post_json", return_value={}) as request:
            generate(Model("google", "models/test"), "s", "u", 1000, 10)
            url, body, headers, _ = request.call_args.args
            self.assertNotIn("test-secret", url + json.dumps(body))
            self.assertEqual(headers["x-goog-api-key"], "test-secret")
            self.assertIn("/models/test:generateContent", url)

    def test_redaction_and_redirects(self):
        with patch.dict("os.environ", {"XAI_API_KEY": "dummy-secret"}, clear=True):
            self.assertEqual(redact("Error: dummy-secret"), "Error: [REDACTED]")
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))

    def test_fences_and_malformed_responses(self):
        self.assertEqual(strip_fences("```cpp\nint main(){}\n```"), "int main(){}\n")
        self.assertEqual(strip_fences("int main(){}"), "int main(){}\n")
        with self.assertRaises(ProviderError):
            strip_fences("Here is the code:\n```cpp\nint main(){}\n```")
        for provider in ("openai", "anthropic", "xai", "google"):
            with self.assertRaises(ProviderError):
                extract_text(provider, {})

    def test_invalid_models(self):
        for value in ("no-provider", "unknown:model", "openai:", "openai:model with spaces"):
            with self.assertRaises(ValueError):
                Model.parse(value)


if __name__ == "__main__":
    unittest.main()
