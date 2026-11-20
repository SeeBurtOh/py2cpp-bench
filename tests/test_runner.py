import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

from py2cpp_bench.runner import OUTPUT_LIMIT, Runner, clean_environment


class RunnerTests(unittest.TestCase):
    def test_subprocess_has_no_credentials_or_loader_injection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "OPENAI_API_KEY": "fixture-secret", "AWS_SECRET_ACCESS_KEY": "other-secret", "DYLD_INSERT_LIBRARIES": "evil",
        }):
            environment = clean_environment(Path(directory))
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
            self.assertNotIn("DYLD_INSERT_LIBRARIES", environment)
            run = Runner("none").run([sys.executable, "-I", "-c", "import os;print(os.getenv('OPENAI_API_KEY'))"], directory, 5)
            self.assertTrue(run.ok)
            self.assertEqual(run.stdout.strip(), "None")

    def test_timeout_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Runner("none").run([sys.executable, "-I", "-c", "import time;time.sleep(20)"], directory, 0.15)
            self.assertTrue(run.timed_out)
            self.assertLess(run.wall_seconds, 3)

    def test_output_flood_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Runner("none").run([sys.executable, "-I", "-c", "import os\nwhile True: os.write(1,b'x'*65536)"], directory, 5)
            self.assertTrue(run.output_limited)
            self.assertLessEqual(len(run.stdout), OUTPUT_LIMIT)

    def test_nonzero_exit_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Runner("none").run([sys.executable, "-I", "-c", "raise SystemExit(7)"], directory, 5)
            self.assertEqual(run.returncode, 7)
            self.assertFalse(run.ok)

    @unittest.skipUnless(platform.system() == "Darwin" and os.getenv("PY2CPP_TEST_SANDBOX") == "1",
                         "set PY2CPP_TEST_SANDBOX=1 outside a nested sandbox to test native isolation")
    def test_macos_blocks_outside_reads_writes_network_and_forks(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside) / "private.txt"
            sentinel.write_text("private sentinel")
            code = f"""import os, socket
from pathlib import Path
for action in [lambda: Path({str(sentinel)!r}).read_text(),
               lambda: Path({str(sentinel)!r}).write_text('changed'),
               lambda: socket.socket().connect(('127.0.0.1', 9)),
               lambda: os.fork()]:
    try:
        action()
        raise AssertionError('sandbox allowed forbidden operation')
    except PermissionError:
        pass
print('blocked')
"""
            run = Runner().run([sys.executable, "-I", "-c", code], directory, 10)
            self.assertTrue(run.ok, run.stderr)
            self.assertEqual(run.stdout.strip(), "blocked")
            self.assertEqual(sentinel.read_text(), "private sentinel")


if __name__ == "__main__":
    unittest.main()
