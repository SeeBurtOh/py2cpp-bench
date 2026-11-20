"""Interactive onboarding. No network requests and no keys in shell commands."""

import getpass
import shlex
import sys
import warnings

from .credentials import (CredentialError, KEY_NAMES, PROVIDER_NAMES, delete_key,
                          environment_key, get_key, native_backend, read_saved_key,
                          remember, save_key, set_session_key, validate_key)
from .providers import DEFAULT_MODELS


def require_terminal():
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CredentialError("Key setup requires an interactive terminal. Run this command in Terminal; do not pipe API keys into it.")


def hidden_key(prompt):
    require_terminal()
    try:
        # getpass otherwise falls back to echoing input on unsuitable terminals.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return remember(getpass.getpass(prompt))
    except getpass.GetPassWarning:
        raise CredentialError("This terminal cannot hide input. No key was accepted; use a terminal with echo control.") from None
    except EOFError:
        raise CredentialError("Key entry cancelled. No key was saved.") from None


def choose(prompt, choices):
    while True:
        value = input(prompt).strip().lower()
        if value in choices:
            return choices[value]
        print("Choose one of: " + ", ".join(choices))


def selected_providers():
    providers = list(KEY_NAMES)
    for number, provider in enumerate(providers, 1):
        print(f"  {number}. {PROVIDER_NAMES[provider]} ({provider})")
    aliases = {str(i): p for i, p in enumerate(providers, 1)} | {p: p for p in providers}
    while True:
        value = input("Providers (names/numbers separated by commas, 'all', or Enter to cancel): ").strip().lower()
        if not value:
            return []
        if value == "all":
            return providers
        values = value.replace(",", " ").split()
        if all(item in aliases for item in values):
            return list(dict.fromkeys(aliases[item] for item in values))
        print("Use the provider names or numbers listed above.")


def environment_instructions(providers):
    print("Run the following in Bash/Zsh. Each command asks privately for a key; the key itself is not in shell history.")
    for provider in providers:
        script = (
            "import getpass, warnings; "
            "warnings.simplefilter('error', getpass.GetPassWarning); "
            f"print(getpass.getpass('{PROVIDER_NAMES[provider]} API key: '))"
        )
        print(f"export {KEY_NAMES[provider][0]}=\"$(python3 -c {shlex.quote(script)})\"")
    print("These exports last for that shell session. They do not create a .env file or change your shell profile.")


def next_command(providers, *, prompt=False):
    models = {value.split(":", 1)[0]: value for value in DEFAULT_MODELS}
    command = "python -m py2cpp_bench run"
    if prompt:
        command += " --ask-keys"
    for provider in providers:
        command += " --model " + models[provider]
    print("Next command (check that these model IDs are available to your account):")
    print("  " + command)
    print("Running sends your selected source to those providers and can incur API charges. Setup made no API requests.")


def guided_setup(args):
    require_terminal()
    print("API key setup — key characters are hidden. No provider API requests are made here.")
    if args.remove:
        provider = args.remove
        if args.provider or args.storage:
            raise CredentialError("Use --remove on its own, without --provider or --storage.")
        if not read_saved_key(provider, required=True):
            print(f"No saved {PROVIDER_NAMES[provider]} key found.")
            return 0
        yes = choose(f"Remove this app's saved {PROVIDER_NAMES[provider]} key? [y/N]: ",
                     {"": False, "n": False, "no": False, "y": True, "yes": True})
        if yes:
            delete_key(provider)
            print("Saved key removed. Environment variables are unchanged; this does not revoke the key at the provider.")
        else:
            print("Saved key kept.")
        return 0

    providers = list(dict.fromkeys(args.provider or selected_providers()))
    if not providers:
        print("Setup cancelled. No changes made.")
        return 0
    storage = args.storage
    if not storage:
        print("1. Save in OS keychain (requires the optional keychain extra)")
        print("2. Enter privately for each run (nothing saved)")
        print("3. Use environment variables")
        storage = choose("Choose [1/2/3]: ", {"1": "keychain", "2": "prompt", "3": "environment"})
    if storage == "prompt":
        print("Keys will be requested when you run with --ask-keys and released when that command exits.")
        next_command(providers, prompt=True)
        return 0
    if storage == "environment":
        environment_instructions(providers)
        next_command(providers)
        return 0

    native_backend(required=True)  # Fail before asking for any secret if unavailable.
    print("The OS may ask you to unlock the keychain or allow access. Environment variables take precedence over saved keys.")
    configured = []
    for provider in providers:
        print(f"\n{PROVIDER_NAMES[provider]}")
        if environment_key(provider):
            print(f"{KEY_NAMES[provider][0]} (or its alias) is set and will override the saved key until unset.")
        existing = read_saved_key(provider, required=True)
        if existing:
            replace = choose("A saved key exists. Replace it? [y/N]: ",
                             {"": False, "n": False, "no": False, "y": True, "yes": True})
            if not replace:
                print("Saved key kept.")
                configured.append(provider)
                continue
        while True:
            value = hidden_key("API key (hidden; Enter to skip): ")
            if not value.strip():
                print("Skipped; any existing saved key was left unchanged.")
                if existing:
                    configured.append(provider)
                break
            try:
                value = validate_key(value)
            except CredentialError as exc:
                print(str(exc))
                continue
            confirmation = hidden_key("Enter the key again (hidden): ").strip()
            if confirmation != value:
                print("Entries did not match. Nothing saved; try again.")
                continue
            save_key(provider, value)
            print("Saved in OS keychain. API access has not been validated.")
            configured.append(provider)
            break
    if configured:
        next_command(configured)
    else:
        print("No keys saved. Run setup again when ready.")
    return 0


def prompt_missing_keys(models):
    for provider in dict.fromkeys(model.provider for model in models):
        if get_key(provider):
            continue
        while True:
            value = hidden_key(f"{PROVIDER_NAMES[provider]} API key for this run only (hidden; Enter to skip): ")
            if not value.strip():
                break
            try:
                set_session_key(provider, value)
                break
            except CredentialError as exc:
                print(str(exc))
