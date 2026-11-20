import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import warnings

from py2cpp_bench import credentials
from py2cpp_bench.benchmark import write_json
from py2cpp_bench.cli import main, parser
from py2cpp_bench.providers import Model, generate
from py2cpp_bench.runner import clean_environment
from py2cpp_bench.setup import guided_setup, hidden_key, prompt_missing_keys


class CredentialTests(unittest.TestCase):
    def setUp(self):
        credentials.clear_credentials()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.backend = Mock()
        self.backend.get_password.return_value = None
        self.backend_patch = patch("py2cpp_bench.credentials.native_backend", return_value=self.backend)
        self.backend_patch.start()

    def tearDown(self):
        self.backend_patch.stop()
        self.environment.stop()
        credentials.clear_credentials()

    def test_environment_takes_precedence_without_keychain_access(self):
        os.environ["OPENAI_API_KEY"] = "env-secret"
        credentials.set_session_key("openai", "session-secret")
        self.assertEqual(Model("openai", "x").key(), "env-secret")
        self.backend.get_password.assert_not_called()

    def test_google_alias_and_session_credentials(self):
        os.environ["GOOGLE_API_KEY"] = "alias-secret"
        self.assertEqual(credentials.get_key("google"), "alias-secret")
        credentials.set_session_key("anthropic", "session-secret")
        self.assertEqual(credentials.get_key("anthropic"), "session-secret")
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        self.backend.get_password.assert_not_called()

    def test_keychain_retrieval_is_cached_and_never_exported(self):
        self.backend.get_password.return_value = "saved-secret"
        self.assertEqual(credentials.get_key("openai"), "saved-secret")
        self.assertEqual(credentials.get_key("openai"), "saved-secret")
        self.backend.get_password.assert_called_once_with(credentials.SERVICE, "openai")
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        self.assertNotIn("saved-secret", json.dumps(clean_environment(Path("/tmp"))))

    def test_loaded_secrets_redacted_in_artifacts_and_backend_errors(self):
        self.backend.get_password.return_value = "saved-secret"
        credentials.get_key("openai")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            write_json(output, {"error": "provider echoed saved-secret"})
            self.assertNotIn("saved-secret", output.read_text())
            self.assertIn("[REDACTED]", output.read_text())
        self.backend.set_password.side_effect = RuntimeError("entered-secret")
        with self.assertRaises(credentials.CredentialError) as error:
            credentials.save_key("xai", "entered-secret")
        self.assertNotIn("entered-secret", str(error.exception))

    def test_json_escaped_secrets_are_redacted(self):
        secret = 'secret-with"quote\\slash'
        credentials.set_session_key("openai", secret)
        serialized = credentials.redact(json.dumps({"error": secret}))
        self.assertEqual(json.loads(serialized)["error"], "[REDACTED]")

    def test_saved_key_authenticates_request_without_being_in_body(self):
        self.backend.get_password.return_value = "saved-secret"
        with patch("py2cpp_bench.providers.post_json", return_value={}) as post:
            generate(Model("openai", "x"), "system", "source", 10, 10)
        url, body, headers, _ = post.call_args.args
        self.assertEqual(headers["Authorization"], "Bearer saved-secret")
        self.assertNotIn("saved-secret", url + json.dumps(body))

    def test_locked_keychain_has_actionable_secret_free_error(self):
        self.backend.get_password.side_effect = RuntimeError("accidental-secret")
        with self.assertRaises(credentials.CredentialError) as error:
            credentials.get_key("openai")
        self.assertIn("OPENAI_API_KEY", str(error.exception))
        self.assertNotIn("accidental-secret", str(error.exception))

    def test_save_and_delete_are_scoped_to_one_app_and_provider(self):
        credentials.save_key("google", "new-secret")
        self.backend.set_password.assert_called_once_with(credentials.SERVICE, "google", "new-secret")
        credentials.delete_key("google")
        self.backend.delete_password.assert_called_once_with(credentials.SERVICE, "google")

    def test_session_clear_removes_cached_values(self):
        credentials.set_session_key("openai", "session-secret")
        credentials.clear_credentials()
        self.assertIsNone(credentials.get_key("openai"))


class NativeBackendTests(unittest.TestCase):
    def test_only_native_module_is_selected(self):
        backend = Mock(priority=5)
        module = Mock()
        module.Keyring.return_value = backend
        for system, expected in (("Darwin", "keyring.backends.macOS"), ("Linux", "keyring.backends.SecretService")):
            with patch("py2cpp_bench.credentials.platform.system", return_value=system), \
                 patch("py2cpp_bench.credentials.importlib.import_module", return_value=module) as load, \
                 patch.dict(os.environ, {"PYTHON_KEYRING_BACKEND": "keyrings.alt.file.PlaintextKeyring"}):
                self.assertIs(credentials.native_backend(required=True), backend)
                load.assert_called_once_with(expected)

    def test_missing_dependency_and_unavailable_backend_never_use_plaintext(self):
        for failure in (ImportError("absent"), RuntimeError("no desktop service")):
            with patch("py2cpp_bench.credentials.platform.system", return_value="Darwin"), \
                 patch("py2cpp_bench.credentials.importlib.import_module", side_effect=failure) as load:
                self.assertIsNone(credentials.native_backend())
                with self.assertRaises(credentials.CredentialError):
                    credentials.native_backend(required=True)
                self.assertEqual(load.call_count, 2)


class SetupTests(unittest.TestCase):
    def setUp(self):
        credentials.clear_credentials()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.terminal = patch("py2cpp_bench.setup.require_terminal")
        self.terminal.start()

    def tearDown(self):
        self.terminal.stop()
        self.environment.stop()
        credentials.clear_credentials()

    def setup_args(self, *extra):
        return parser().parse_args(["setup", "--provider", "openai", *extra])

    def test_masked_entry_confirm_and_save_without_printing_secret(self):
        output = io.StringIO()
        with patch("py2cpp_bench.setup.native_backend"), \
             patch("py2cpp_bench.setup.read_saved_key", return_value=None), \
             patch("py2cpp_bench.setup.getpass.getpass", side_effect=["entered-secret", "entered-secret"]), \
             patch("py2cpp_bench.setup.save_key") as save, contextlib.redirect_stdout(output):
            self.assertEqual(guided_setup(self.setup_args("--storage", "keychain")), 0)
        save.assert_called_once_with("openai", "entered-secret")
        self.assertNotIn("entered-secret", output.getvalue())
        self.assertIn("API access has not been validated", output.getvalue())

    def test_mismatch_and_blank_skip_do_not_save(self):
        with patch("py2cpp_bench.setup.native_backend"), \
             patch("py2cpp_bench.setup.read_saved_key", return_value=None), \
             patch("py2cpp_bench.setup.getpass.getpass", side_effect=["first-secret", "different-secret", ""]), \
             patch("py2cpp_bench.setup.save_key") as save, contextlib.redirect_stdout(io.StringIO()):
            guided_setup(self.setup_args("--storage", "keychain"))
        save.assert_not_called()

    def test_existing_key_kept_by_default_without_requesting_new_secret(self):
        with patch("py2cpp_bench.setup.native_backend"), \
             patch("py2cpp_bench.setup.read_saved_key", return_value="existing-secret"), \
             patch("builtins.input", return_value=""), \
             patch("py2cpp_bench.setup.getpass.getpass") as entry, \
             patch("py2cpp_bench.setup.save_key") as save, contextlib.redirect_stdout(io.StringIO()):
            guided_setup(self.setup_args("--storage", "keychain"))
        save.assert_not_called()
        entry.assert_not_called()

    def test_prompt_and_environment_modes_do_not_read_or_store_secrets(self):
        for mode in ("prompt", "environment"):
            output = io.StringIO()
            with patch("py2cpp_bench.setup.native_backend") as backend, \
                 patch("py2cpp_bench.setup.getpass.getpass") as entry, contextlib.redirect_stdout(output):
                guided_setup(self.setup_args("--storage", mode))
            backend.assert_not_called()
            entry.assert_not_called()
            self.assertIn("--ask-keys" if mode == "prompt" else "export OPENAI_API_KEY", output.getvalue())

    def test_hidden_entry_refuses_getpass_echo_fallback(self):
        def warning(*args, **kwargs):
            warnings.warn("Cannot control echo", __import__("getpass").GetPassWarning)
        with patch("py2cpp_bench.setup.getpass.getpass", side_effect=warning):
            with self.assertRaisesRegex(credentials.CredentialError, "cannot hide input"):
                hidden_key("Key: ")

    def test_missing_keys_prompt_once_per_provider_and_stay_in_memory(self):
        output = io.StringIO()
        with patch("py2cpp_bench.credentials.native_backend", return_value=None), \
             patch("py2cpp_bench.setup.getpass.getpass", return_value="session-secret") as entry, \
             contextlib.redirect_stdout(output):
            prompt_missing_keys([Model("openai", "a"), Model("openai", "b")])
            self.assertEqual(credentials.get_key("openai"), "session-secret")
        self.assertEqual(entry.call_count, 1)
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        self.assertNotIn("session-secret", output.getvalue())

    def test_keychain_unavailable_fails_before_key_entry(self):
        with patch("py2cpp_bench.setup.native_backend", side_effect=credentials.CredentialError("unavailable")), \
             patch("py2cpp_bench.setup.getpass.getpass") as entry, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(credentials.CredentialError):
                guided_setup(self.setup_args("--storage", "keychain"))
        entry.assert_not_called()

    def test_removal_needs_confirmation_and_only_deletes_selected_provider(self):
        args = parser().parse_args(["setup", "--remove", "google"])
        with patch("py2cpp_bench.setup.read_saved_key", return_value="saved-secret"), \
             patch("builtins.input", side_effect=["", "yes"]), \
             patch("py2cpp_bench.setup.delete_key") as delete, contextlib.redirect_stdout(io.StringIO()):
            guided_setup(args)
            delete.assert_not_called()
            guided_setup(args)
            delete.assert_called_once_with("google")


class NoninteractiveTests(unittest.TestCase):
    def test_setup_refuses_noninteractive_input(self):
        with patch("sys.stdin.isatty", return_value=False), \
             patch("py2cpp_bench.setup.getpass.getpass") as entry, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["setup", "--provider", "openai", "--storage", "keychain"]), 2)
        entry.assert_not_called()

    def test_dry_run_with_ask_keys_never_reads_keychain_or_prompts(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("py2cpp_bench.credentials.native_backend", side_effect=AssertionError("unexpected keychain read")), \
             patch("py2cpp_bench.setup.getpass.getpass", side_effect=AssertionError("unexpected prompt")), \
             patch("py2cpp_bench.benchmark.shutil.which", return_value="/usr/bin/clang++"), \
             patch("py2cpp_bench.benchmark.machine_info", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["run", "--dry-run", "--ask-keys", "--isolation", "none", "--output", directory]), 0)


if __name__ == "__main__":
    unittest.main()
