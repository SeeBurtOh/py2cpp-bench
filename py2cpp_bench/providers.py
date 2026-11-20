"""Small HTTP adapters. One request, no automatic retries or model substitution."""

from dataclasses import dataclass
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .credentials import KEY_NAMES, get_key, redact
DEFAULT_MODELS = (
    "openai:gpt-6.1-sol",
    "anthropic:claude-sonnet-5-5",
    "xai:grok-4.3",
    "google:gemini-4-argon",
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Model:
    provider: str
    name: str

    @classmethod
    def parse(cls, value):
        provider, sep, name = value.partition(":")
        if not sep or provider not in KEY_NAMES or not re.fullmatch(r"[A-Za-z0-9._/-]+", name):
            raise ValueError("model must be PROVIDER:MODEL_ID; providers: " + ", ".join(KEY_NAMES))
        return cls(provider, name)

    @property
    def label(self):
        return f"{self.provider}:{self.name}"

    def key(self):
        return get_key(self.provider)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials to a redirected endpoint.
        return None


def request_spec(model, system, user, max_tokens):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if model.provider == "openai":
        return "https://api.openai.com/v1/responses", {
            "model": model.name, "input": messages,
            "max_output_tokens": max_tokens, "store": False,
        }
    if model.provider == "xai":
        return "https://api.x.ai/v1/chat/completions", {
            "model": model.name, "messages": messages,
            "max_tokens": max_tokens, "stream": False,
        }
    if model.provider == "anthropic":
        return "https://api.anthropic.com/v1/messages", {
            "model": model.name, "system": system,
            "messages": messages[1:], "max_tokens": max_tokens,
        }
    model_id = urllib.parse.quote(model.name.removeprefix("models/"), safe="")
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent", {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "candidateCount": 1},
    }


def post_json(url, body, headers, timeout):
    request = urllib.request.Request(url, json.dumps(body).encode(), {
        "Content-Type": "application/json", **headers,
    }, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ProviderError("API response exceeded 8 MiB")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ProviderError("API returned an unexpected JSON shape")
        return result
    except urllib.error.HTTPError as exc:
        # Include actionable provider errors, but never include request headers.
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise ProviderError(redact(f"HTTP {exc.code}: {detail}")) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProviderError(redact(f"API request failed: {exc}")) from None


def extract_text(provider, response):
    try:
        if provider == "openai":
            if response.get("status") != "completed":
                raise ProviderError(f"Incomplete response: {response.get('incomplete_details') or response.get('error')}")
            parts = [part["text"] for item in response.get("output", [])
                     if item.get("type") == "message" for part in item.get("content", [])
                     if part.get("type") == "output_text"]
        elif provider == "anthropic":
            if response.get("stop_reason") != "end_turn":
                raise ProviderError(f"Generation stopped: {response.get('stop_reason')}")
            parts = [part["text"] for part in response["content"] if part.get("type") == "text"]
        elif provider == "xai":
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ProviderError(f"Generation stopped: {choice.get('finish_reason')}")
            parts = [choice["message"]["content"]]
        else:
            candidate = response.get("candidates", [{}])[0]
            if candidate.get("finishReason") != "STOP":
                raise ProviderError(f"Generation stopped: {candidate.get('finishReason') or response.get('promptFeedback')}")
            parts = [part["text"] for part in candidate["content"]["parts"]
                     if "text" in part and not part.get("thought")]
        text = "\n".join(parts).strip()
        if not text:
            raise ProviderError("Model returned no C++ text (possibly a refusal)")
        return text
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("API returned an unexpected response shape") from None


def strip_fences(text):
    """Accept plain source or one enclosing fence, not a mixture of prose and blocks."""
    text = text.strip()
    match = re.fullmatch(r"```(?:cpp|c\+\+|cc|cxx)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()
    if "```" in text:
        raise ProviderError("Response contains unrecognized Markdown fences")
    return text + "\n"


def generate(model, system, user, max_tokens, timeout):
    key = model.key()
    if not key:
        raise ProviderError("Missing " + " or ".join(KEY_NAMES[model.provider]))
    headers = {"Authorization": f"Bearer {key}"}
    if model.provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif model.provider == "google":
        headers = {"x-goog-api-key": key}
    url, body = request_spec(model, system, user, max_tokens)
    return post_json(url, body, headers, timeout)
