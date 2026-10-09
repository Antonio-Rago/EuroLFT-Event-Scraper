"""Credential setup diagnostics using fake values only; no live credentials."""

from contextlib import redirect_stdout
import getpass
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings

from tools.event_collector.credentials import check_key, load_key, main, store_key


class CredentialTests(unittest.TestCase):
    def invoke(self, value=None, read_error=None, save_error=None):
        output = io.StringIO()
        with patch("tools.event_collector.credentials.getpass.getpass", return_value=value,
                   side_effect=read_error), patch("tools.event_collector.credentials.store_key",
                                                 side_effect=save_error) as save:
            with redirect_stdout(output):
                code = main()
        return code, output.getvalue(), save

    def test_dotted_long_auth_key_roundtrip(self):
        fake = "AQ." + "fake-test-data_" * 40 + ".not-a-real-key"
        self.assertEqual(check_key(fake), fake)
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {}, clear=True):
            root = Path(temporary)
            store_key(fake, root)
            self.assertEqual(load_key(root), fake)

    def test_success_does_not_echo_secret(self):
        fake = "AQ.fake-test-value.not-a-real-key"
        code, output, save = self.invoke("  " + fake + "  ")
        self.assertEqual(code, 0)
        save.assert_called_once_with(fake)
        self.assertNotIn(fake, output)

    def test_invalid_inputs_have_distinct_safe_diagnostics(self):
        for value, message in (("", "No key was entered"),
                               ("'AQ.fake-not-a-real-key'", "quotation marks"),
                               ("AQ.fake\u200b-not-a-real-key", "non-printing"),
                               ("AQ.fake\nnot-a-real-key", "whitespace"),
                               ("x" * 4096, "size limit")):
            with self.subTest(message=message):
                code, output, save = self.invoke(value)
                self.assertEqual(code, 1)
                self.assertIn(message, output)
                save.assert_not_called()
                if value:
                    self.assertNotIn(value, output)

    def test_terminal_errors_distinguished_without_saving(self):
        for error, message in ((EOFError(), "input ended"),
                               (getpass.GetPassWarning(), "hidden input is unavailable"),
                               (KeyboardInterrupt(), "cancelled"),
                               (OSError(5, "do not echo"), "could not read")):
            with self.subTest(message=message):
                code, output, save = self.invoke(read_error=error)
                self.assertEqual(code, 1)
                self.assertIn(message, output)
                save.assert_not_called()
                self.assertNotIn("do not echo", output)

    def test_non_tty_warning_fails_before_echo_fallback(self):
        def warn_then_echo(prompt):
            warnings.warn("terminal unavailable", getpass.GetPassWarning)
            self.fail("Would have fallen back to visible input")
        output = io.StringIO()
        with patch("tools.event_collector.credentials.getpass.getpass", side_effect=warn_then_echo), \
                patch("tools.event_collector.credentials.store_key") as save, redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertIn("hidden input is unavailable", output.getvalue())
        save.assert_not_called()

    def test_write_failure_does_not_echo_secret_or_exception(self):
        fake = "AQ.fake-test-value.not-a-real-key"
        code, output, save = self.invoke(fake, save_error=PermissionError(13, fake))
        self.assertEqual(code, 1)
        self.assertIn("OS error 13", output)
        self.assertNotIn(fake, output)


if __name__ == "__main__":
    unittest.main()
