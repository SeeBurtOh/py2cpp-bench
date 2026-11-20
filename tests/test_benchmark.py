import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from py2cpp_bench.benchmark import prepare, result_output, run_benchmark, table
from py2cpp_bench.cli import parser
from py2cpp_bench.runner import Execution


class BenchmarkTests(unittest.TestCase):
    def test_exact_result_and_separate_timing(self):
        value, seconds = result_output("Result: 3.141592656089\nExecution Time: 11.100000 seconds\n", True)
        self.assertEqual(value, "Result: 3.141592656089")
        self.assertEqual(seconds, 11.1)
        self.assertEqual(result_output("hello\r\n", False), ("hello", None))
        for text in ("Result: 1\nNaN\n", "Result: 1\n-1\n", "Result: 1\n1e999\n", "only one line"):
            with self.assertRaises(ValueError):
                result_output(text, True)

    def test_pi_preserves_original_arithmetic_and_changes_inputs(self):
        args = parser().parse_args(["run", "--iterations", "27"])
        source, main, checks, models, timing, builtin = prepare(args)
        namespace = {"__name__": "test"}
        exec(source, namespace)
        expected = 1.0
        for i in range(1, 28):
            expected -= 1 / (i * 4 - 1)
            expected += 1 / (i * 4 + 1)
        self.assertEqual(namespace["calculate"](27, 4, 1), expected)
        self.assertEqual(main, ["27", "4", "1"])
        self.assertTrue(timing and builtin)
        self.assertEqual(len(models), 4)
        self.assertGreaterEqual(len(checks), 7)
        self.assertTrue(any(check[1:] != ["4", "1"] for check in checks))
        self.assertEqual(checks, prepare(args)[2])

    def test_rejects_duplicate_attempts_and_bad_cases(self):
        for extra in (["--model", "openai:x", "--model", "openai:x"],
                      ["--check-args", '[1, 2]'], ["--demo", "--model", "openai:x"]):
            with self.assertRaises(ValueError):
                prepare(parser().parse_args(["run", *extra]))

    def test_table_never_ranks_wrong_answers_or_prints_terminal_escapes(self):
        text = table({"models": [
            {"model": "bad", "status": "wrong_answer", "speedup": 1000, "result": "\x1b[31mbad"},
            {"model": "good", "status": "ok", "speedup": 2, "best_seconds": 1, "median_seconds": 2, "faster": True},
        ]})
        self.assertLess(text.index("good"), text.index("bad"))
        self.assertNotIn("1000", text)
        self.assertNotIn("\x1b", text)

    def pipeline(self, cpp, *, wrong=False, fail_compile=False, runtime=False, truncate=False):
        """Exercise orchestration and artifact persistence, mocking only external boundaries."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "program.py"
            source.write_text("print(42)\n")
            args = parser().parse_args(["run", str(source), "--arg", "7", "--check-args", '["11"]',
                                       "--model", "openai:fixture", "--isolation", "none",
                                       "--output", str(root / "runs")])

            def fake_compile(self, command, cwd, timeout, **kwargs):
                if not fail_compile:
                    (Path(cwd) / "main").write_text("fixture")
                return Execution(command, 1 if fail_compile else 0, 0.3, "", "syntax error" if fail_compile else "")

            def fake_run(runner, program, argv, timeout, timing, *, python=False):
                is_wrong = wrong and not python
                failed = runtime and not python
                return {"args": argv, "status": "timeout" if failed else "ok", "wall_seconds": 2 if python else 0.5,
                        "result": "43" if is_wrong else "42", "stdout": "42\n", "stderr": "",
                        "self_reported_seconds": None}

            response = {"status": "incomplete" if truncate else "completed", "model": "fixture",
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": cpp}]}]}
            with patch.dict("os.environ", {"OPENAI_API_KEY": "fixture-secret"}, clear=True), \
                 patch("py2cpp_bench.benchmark.shutil.which", return_value="/usr/bin/clang++"), \
                 patch("py2cpp_bench.benchmark.machine_info", return_value={"architecture": "test"}), \
                 patch("py2cpp_bench.benchmark.probe", return_value={}), \
                 patch("py2cpp_bench.benchmark.generate", return_value=response) as generation, \
                 patch("py2cpp_bench.benchmark.timed_run", side_effect=fake_run), \
                 patch("py2cpp_bench.benchmark.Runner.run", new=fake_compile), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = run_benchmark(args)
            report = json.loads(next((root / "runs").glob("*/report.json")).read_text())
            self.assertEqual(generation.call_count, 1)
            self.assertNotIn("fixture-secret", json.dumps(report))
            return code, report

    def test_pipeline_success_and_speedup(self):
        code, report = self.pipeline("int main(){}")
        item = report["models"][0]
        self.assertEqual(code, 0)
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["speedup"], 4)
        self.assertEqual(len(item["runs"]), 3)
        self.assertEqual(item["generation_requests"], 1)

    def test_failures_never_receive_speedup(self):
        for options, status in (({"wrong": True}, "wrong_answer"),
                                ({"fail_compile": True}, "compile_failed"),
                                ({"runtime": True}, "timeout"),
                                ({"truncate": True}, "generation_failed")):
            with self.subTest(status=status):
                code, report = self.pipeline("int main(){}", **options)
                self.assertEqual(code, 1)
                item = report["models"][0]
                self.assertEqual(item["status"], status)
                self.assertNotIn("speedup", item)


if __name__ == "__main__":
    unittest.main()
