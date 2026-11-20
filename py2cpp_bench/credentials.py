"""Credential lookup and optional native OS keychains; never plaintext storage."""

import importlib
import json
import os
import platform


KEY_NAMES = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic", "xai": "xAI", "google": "Google Gemini"}
SERVICE = "py2cpp-bench.api-keys"
INSTALL_HINT = 'Install keychain support in your virtual environment: python -m pip install ".[keychain]"'
_session_keys = {}
_saved_keys = {}
_known_secrets = set()


class CredentialError(ValueError):
    pass


def clear_credentials():
    """Drop references between CLI invocations. Python cannot guarantee memory zeroization."""
    _session_keys.clear()
    _saved_keys.clear()
    _known_secrets.clear()


def remember(value):
    if value:
        _known_secrets.add(value)
    return value


def environment_key(provider):
    return next((remember(os.environ[name].strip()) for name in KEY_NAMES[provider]
                 if os.environ.get(name, "").strip()), None)


def native_backend(required=False):
    # Select known native implementations directly. Do not load user-configured,
    # third-party, plaintext, null, or chained keyring backends.
    module_name = {
        "Darwin": "keyring.backends.macOS",
        "Linux": "keyring.backends.SecretService",
    }.get(platform.system())
    if module_name is None:
        if required:
            raise CredentialError("Keychain storage currently supports macOS Keychain and Linux Secret Service.")
        return None
    try:
        backend = importlib.import_module(module_name).Keyring()
        if backend.priority <= 0:
            raise RuntimeError("Unavailable backend")
        return backend
    except ImportError:
        if required:
            raise CredentialError(INSTALL_HINT) from None
    except Exception:
        if required:
            raise CredentialError("OS keychain unavailable. Unlock your desktop keychain, or choose per-run entry/environment variables.") from None
    return None


def read_saved_key(provider, *, required=False):
    backend = native_backend(required=required)
    if backend is None:
        return None
    try:
        value = backend.get_password(SERVICE, provider)
    except Exception:
        # Backend exception strings are not trusted to exclude secret material.
        raise CredentialError(f"Cannot read the saved {PROVIDER_NAMES[provider]} key. Unlock/allow OS keychain access, or set {KEY_NAMES[provider][0]}.") from None
    if value is not None and not isinstance(value, str):
        raise CredentialError("OS keychain returned an invalid credential")
    return remember(value.strip()) if value else None


def get_key(provider):
    value = environment_key(provider)
    if value:
        return value
    if provider in _session_keys:
        return _session_keys[provider]
    if provider not in _saved_keys:
        _saved_keys[provider] = read_saved_key(provider)
    return _saved_keys[provider]


def validate_key(value):
    value = value.strip()
    if not value or not value.isascii() or any(c.isspace() or not c.isprintable() for c in value):
        raise CredentialError("Enter a nonempty API key without whitespace or control characters.")
    return value


def set_session_key(provider, value):
    _session_keys[provider] = remember(validate_key(value))


def save_key(provider, value):
    value = remember(validate_key(value))
    backend = native_backend(required=True)
    try:
        backend.set_password(SERVICE, provider, value)
    except Exception:
        raise CredentialError("Could not save the key in the OS keychain. Nothing was saved to a plaintext file. Check keychain access and try again.") from None
    _saved_keys[provider] = value


def delete_key(provider):
    backend = native_backend(required=True)
    try:
        backend.delete_password(SERVICE, provider)
    except Exception:
        raise CredentialError("Could not remove the saved key. Check that the item exists and keychain access is allowed.") from None
    _saved_keys.pop(provider, None)
    # Keep already-loaded values in the redaction registry until this invocation exits.


def redact(text):
    secrets = set(_known_secrets)
    for names in KEY_NAMES.values():
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                secrets.add(value)
    # Longest first prevents a shorter key from revealing the suffix of another key.
    # Persistence redacts after JSON serialization; cover escaped representations too.
    secrets.update(json.dumps(value, ensure_ascii=False)[1:-1] for value in tuple(secrets))
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text
