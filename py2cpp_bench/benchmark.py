"""Generate once, compile once, validate, then measure serially."""

from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import math
from pathlib import Path
import random
import re
import shlex
import shutil
import statistics
import sys
import tempfile
import time

from . import __version__
from .providers import (DEFAULT_MODELS, KEY_NAMES, Model, ProviderError, extract_text,
                        generate, redact, request_spec, strip_fences)
from .runner import Runner, compiler_flags, machine_info


SYSTEM_PROMPT = """Convert the supplied Python program into the fastest correct C++ program
for the supplied machine and compiler command. Return only one complete C++ translation
unit, with occasional comments, and no Markdown or explanation. You have one attempt.
You may freely choose the algorithm, use multiple CPU cores, SIMD intrinsics, and change
memory layout. Preserve the runtime input contract and exact result formatting for all
valid inputs, including inputs not shown in this prompt. Compute from the runtime inputs;
do not hardcode answers or detect benchmark cases. Timing output must measure actual work.
There are no tools or repair rounds. Do not access network services, other programs, secrets,
or benchmark artifacts. Use only libraries available to the supplied compiler command."""


def write_json(path, value):
    # Apply redaction at the final persistence boundary, including provider errors.
    text = redact(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def example(name):
    return files("py2cpp_bench").joinpath("examples", name).read_text(encoding="utf-8")


def result_output(stdout, timing_line):
    text = stdout.replace("\r\n", "\n").removesuffix("\n")
    if not timing_line:
        return text, None
    lines = text.split("\n")
    if len(lines) < 2:
        raise ValueError("Expected result output followed by an elapsed-time line")
    match = re.fullmatch(
        r"(?:Execution Time: )?([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?: seconds)?",
        lines[-1],
    )
    if not match:
        raise ValueError("Malformed elapsed-time line: " + repr(lines[-1][:120]))
    elapsed = float(match.group(1))
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("Elapsed-time line must contain a finite nonnegative number")
    return "\n".join(lines[:-1]), elapsed


def execution_status(execution):
    if execution.timed_out:
        return "timeout"
    if execution.output_limited:
        return "output_limit"
    if execution.returncode != 0:
        return "runtime_error"
    return "ok"


def timed_run(runner, program, args, timeout, timing_line, *, python=False):
    # Each invocation receives only its own program, not reference outputs or reports.
    with tempfile.TemporaryDirectory(prefix="py2cpp-exec-") as directory:
        work = Path(directory)
        target = work / ("source.py" if python else "main")
        if python:
            target.write_text(program, encoding="utf-8")
            command = [sys.executable, "-I", str(target), *args]
        else:
            # Reuse the compiled file's inode so macOS code-signature verification
            # and filesystem caching can warm up naturally across repetitions.
            command = [str(program), *args]
        execution = runner.run(command, work, timeout, read_paths=() if python else (program,))
    record = execution.to_dict()
    record["args"] = args
    record["status"] = execution_status(execution)
    if execution.ok:
        try:
            record["result"], record["self_reported_seconds"] = result_output(execution.stdout, timing_line)
        except ValueError as exc:
            record["status"] = "invalid_output"
            record["error"] = str(exc)
    return record


def summarize(records):
    times = [r["wall_seconds"] for r in records]
    return {"best_seconds": min(times), "median_seconds": statistics.median(times)}


def make_prompt(source, machine, compile_command, args, timing_line, timeout, builtin):
    contract = (
        "argv: iterations [param1 [param2]]. Valid inputs have iterations >= 0, "
        "param1 >= 2 and 0 <= param2 < param1. Defaults are 200000000, 4, 1. "
        "Print 'Result: ' with 12 digits after the decimal, then "
        "'Execution Time: ' with six decimal places and ' seconds'."
        if builtin else "Preserve the Python script's command-line interface and stdout."
    )
    comparison = ("All output except the final elapsed-time line is compared exactly."
                  if timing_line else "All stdout is compared exactly (normalizing CRLF and one final newline).")
    return f"""Machine:
{json.dumps(machine, indent=2)}

Exact compiler command (run in a temporary directory):
{shlex.join(compile_command)}

Benchmark arguments: {json.dumps(args)}
Input contract: {contract}
{comparison}
Additional correctness inputs may be tested and are not disclosed.
Per-execution wall time limit: {timeout} seconds.
Execution is local, with no network or access to the user's project/home files in isolated mode.
All CPU cores and native SIMD are allowed. No GPU framework or third-party library is linked
unless explicitly present in the compiler command. No runtime dependency installation.
The application independently measures process wall time; your printed time is diagnostic only.

Python source begins:
{source}
Python source ends.
"""


def prepare(args):
    builtin = args.source is None
    if args.demo and not builtin:
        raise ValueError("--demo uses the bundled pi example; do not supply a source file")
    if args.demo and args.model:
        raise ValueError("--demo cannot be combined with --model")
    if builtin and args.arg:
        raise ValueError("Use --iterations for the bundled pi example; --arg is for custom sources")
    source = example("pi.py") if builtin else args.source.read_text(encoding="utf-8")
    if not source.strip():
        raise ValueError("Source file is empty")
    if len(source.encode()) > 1024 * 1024:
        raise ValueError("Source exceeds the 1 MiB limit")
    main_args = [str(args.iterations), "4", "1"] if builtin else (args.arg or [])
    checks = []
    if builtin:
        rng = random.Random(args.seed)
        checks = [["0", "4", "1"], ["1", "4", "1"], ["2", "4", "1"],
                  ["19", "4", "1"], [str(rng.randint(1000, 50000)), "4", "1"],
                  [str(rng.randint(1000, 50000)), "6", "1"], ["101", "8", "3"]]
    for encoded in args.check_args or []:
        try:
            check = json.loads(encoded)
        except ValueError:
            raise ValueError("Each --check-args must be a JSON array of strings") from None
        if not isinstance(check, list) or not all(isinstance(item, str) for item in check):
            raise ValueError("Each --check-args must be a JSON array of strings")
        checks.append(check)
    models = [] if args.demo else [Model.parse(value) for value in (args.model or DEFAULT_MODELS)]
    if len({m.label for m in models}) != len(models):
        raise ValueError("Each model may appear only once (one attempt per model)")
    return source, main_args, checks, models, builtin or args.timing_line, builtin


def probe(runner, compiler, flags, timeout):
    with tempfile.TemporaryDirectory(prefix="py2cpp-probe-") as directory:
        work = Path(directory)
        (work / "main.cpp").write_text(
            '#include <iostream>\n#include <thread>\nint main(){std::thread t([]{});t.join();std::cout<<"ready";}\n')
        compile_run = runner.run([compiler, *flags, "main.cpp", "-o", "main"], work, timeout, allow_processes=True)
        if not compile_run.ok:
            raise ValueError("Compiler/isolation preflight failed. No API requests were made.\n" +
                             (compile_run.stderr or execution_status(compile_run)))
        run = runner.run([str(work / "main")], work, min(timeout, 15))
        if not run.ok or run.stdout != "ready":
            raise ValueError("Executable/isolation preflight failed. No API requests were made.\n" +
                             (run.stderr or execution_status(run)))
        python_run = runner.run([sys.executable, "-I", "-c", "print('ready')"], work, 15)
        if not python_run.ok or python_run.stdout.strip() != "ready":
            raise ValueError("Python/isolation preflight failed. No API requests were made.\n" + python_run.stderr)
    return {"compile_seconds": compile_run.wall_seconds, "execution_seconds": run.wall_seconds}


def table(report, markdown=False):
    columns = ["Model", "Status", "Result", "Best (s)", "Median (s)", "Speedup"]
    rows = []
    baseline = report.get("baseline", {})
    if "best_seconds" in baseline:
        rows.append(["Python", "baseline", baseline["result"], f"{baseline['best_seconds']:.6f}",
                     f"{baseline['median_seconds']:.6f}", "1.00x"])
    for item in sorted(report["models"], key=lambda r: (r.get("status") != "ok", -r.get("speedup", 0))):
        valid = item.get("status") == "ok"
        rows.append([item["model"], ("faster" if item.get("faster") else "correct, slower") if valid else item["status"],
                     item.get("result", "—"), f"{item['best_seconds']:.6f}" if valid else "—",
                     f"{item['median_seconds']:.6f}" if valid else "—",
                     f"{item['speedup']:.2f}x" if valid else "—"])
    # Avoid terminal escape sequences from model-generated output or compiler messages.
    def display(value):
        return "".join(c if c.isprintable() else " " for c in str(value))[:80]
    rows = [[display(cell) for cell in row] for row in rows]
    if markdown:
        def line(row):
            return "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
        return "\n".join([line(columns), line(["---"] * len(columns)), *map(line, rows)])
    widths = [max(len(row[i]) for row in [columns, *rows]) for i in range(len(columns))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in [columns, *rows])


def save_report(root, report):
    write_json(root / "report.json", report)
    note = "\n\nSpeedup = best external Python wall time / best external C++ wall time.\n"
    note += "Only candidates matching every checked output and measured run are ranked.\n"
    if report.get("demo"):
        note += "OFFLINE DEMO: handwritten serial C++, not an AI model result.\n"
    (root / "report.md").write_text(redact(table(report, markdown=True) + note), encoding="utf-8")


def run_benchmark(args):
    source, main_args, checks, models, timing_line, builtin = prepare(args)
    compiler = shutil.which(args.cxx)
    if not compiler:
        raise ValueError(f"Compiler not found: {args.cxx}. On macOS install Xcode Command Line Tools.")
    compiler = str(Path(compiler).absolute())
    flags = compiler_flags() + (args.cxx_flag or [])
    runner = Runner(args.isolation, compiler)
    machine = machine_info(compiler)
    command = [compiler, *flags, "main.cpp", "-o", "main"]
    user_prompt = make_prompt(source, machine, command, main_args, timing_line, args.timeout, builtin)
    if args.ask_keys and not args.demo and not args.dry_run:
        from .setup import prompt_missing_keys
        prompt_missing_keys(models)
    if not args.demo and not args.dry_run and not any(model.key() for model in models):
        raise ValueError("No API keys configured for the selected models. Set " +
                         ", ".join(KEY_NAMES[m.provider][0] for m in models) +
                         ", use setup to save keys, or use run --ask-keys. Try run --demo without keys.")
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"),
                                dir=args.output.resolve()))
    print(f"Artifacts: {root}", flush=True)
    if runner.isolation == "none":
        print("Isolation disabled: programs run with your account's filesystem and network access.", flush=True)
    report = {
        "schema_version": 1, "tool_version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(), "machine": machine,
        "isolation": runner.isolation, "compiler_command": command, "demo": args.demo,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "configuration": {"args": main_args, "validation_args": checks, "seed": args.seed,
                          "python_runs": args.python_runs, "cpp_runs": args.cpp_runs,
                          "warmups": args.warmups, "timeout": args.timeout,
                          "compile_timeout": args.compile_timeout, "api_timeout": args.api_timeout,
                          "max_output_tokens": args.max_output_tokens, "timing_line": timing_line,
                          "attempts_per_model": 1, "api_retries": 0},
        "status": "dry_run" if args.dry_run else "running", "models": [],
    }
    (root / "source.py").write_text(redact(source), encoding="utf-8")
    write_json(root / "prompt.json", {"system": SYSTEM_PROMPT, "user": user_prompt})
    if args.dry_run:
        for model in models:
            url, body = request_spec(model, SYSTEM_PROMPT, user_prompt, args.max_output_tokens)
            report["models"].append({"model": model.label, "status": "not_requested", "endpoint": url, "request": body})
        save_report(root, report)
        print("Dry run saved. No programs executed and no API requests made.")
        return 0
    save_report(root, report)
    try:
        print("Checking compiler and execution isolation…", flush=True)
        report["preflight"] = probe(runner, compiler, flags, args.compile_timeout)
        print(f"Running Python baseline ({args.python_runs} measured run(s))…", flush=True)
        baseline = {"runs": [], "warmups": [], "validation": []}
        report["baseline"] = baseline
        expected = None
        for i in range(args.warmups + args.python_runs):
            record = timed_run(runner, source, main_args, args.timeout, timing_line, python=True)
            baseline["warmups" if i < args.warmups else "runs"].append(record)
            if record["status"] != "ok":
                raise ValueError("Python baseline failed: " + record.get("error", record["stderr"] or record["status"]))
            if expected is not None and record["result"] != expected:
                raise ValueError("Python baseline is nondeterministic; result output changed between runs")
            expected = record["result"]
        baseline.update(summarize(baseline["runs"]))
        baseline["result"] = expected
        for check in checks:
            record = timed_run(runner, source, check, args.timeout, timing_line, python=True)
            baseline["validation"].append(record)
            if record["status"] != "ok":
                raise ValueError("Python validation failed: " + record.get("error", record["stderr"] or record["status"]))
        save_report(root, report)

        candidates = [None] if args.demo else models
        for index, model in enumerate(candidates, 1):
            label = model.label if model else "demo:handwritten-serial"
            slug = re.sub(r"[^A-Za-z0-9._-]", "-", label)[:100]
            folder = root / f"{index:02d}-{slug}"
            folder.mkdir()
            item = {"model": label, "status": "pending", "runs": [], "warmups": [], "validation": [],
                    "artifact_directory": folder.name, "generation_requests": 0}
            report["models"].append(item)
            print(f"[{index}/{len(candidates)}] {label}", flush=True)
            try:
                if model:
                    if not model.key():
                        item.update(status="missing_key", error="Set " + " or ".join(KEY_NAMES[model.provider]))
                        continue
                    url, body = request_spec(model, SYSTEM_PROMPT, user_prompt, args.max_output_tokens)
                    write_json(folder / "request.json", {"url": url, "body": body})
                    item["generation_requests"] = 1
                    start = time.perf_counter()
                    try:
                        response = generate(model, SYSTEM_PROMPT, user_prompt, args.max_output_tokens, args.api_timeout)
                    finally:
                        item["generation_seconds"] = time.perf_counter() - start
                    write_json(folder / "response.json", response)
                    item["usage"] = response.get("usage", response.get("usageMetadata", {}))
                    item["returned_model"] = response.get("model", response.get("modelVersion"))
                    cpp = strip_fences(extract_text(model.provider, response))
                else:
                    cpp = example("demo.cpp")
                (folder / "main.cpp").write_text(redact(cpp), encoding="utf-8")
                with tempfile.TemporaryDirectory(prefix="py2cpp-compile-") as directory:
                    work = Path(directory)
                    (work / "main.cpp").write_text(cpp, encoding="utf-8")
                    compiled = runner.run(command, work, args.compile_timeout, allow_processes=True)
                    item["compile"] = compiled.to_dict()
                    (folder / "compiler.stderr.txt").write_text(redact(compiled.stderr), encoding="utf-8")
                    if not compiled.ok or not (work / "main").is_file():
                        item["status"] = "compile_timeout" if compiled.timed_out else "compile_failed"
                        continue
                    binary = work / "main"
                    # No generated executable is copied into the report directory.
                    for reference in baseline["validation"]:
                        record = timed_run(runner, binary, reference["args"], args.timeout, timing_line)
                        record["expected"] = reference["result"]
                        item["validation"].append(record)
                        if record["status"] != "ok" or record["result"] != reference["result"]:
                            item["status"] = record["status"] if record["status"] != "ok" else "wrong_answer"
                            item["result"] = record.get("result", "")
                            item["error"] = "Validation failed for arguments " + json.dumps(reference["args"])
                            break
                    if item["status"] != "pending":
                        continue
                    for i in range(args.warmups + args.cpp_runs):
                        record = timed_run(runner, binary, main_args, args.timeout, timing_line)
                        item["warmups" if i < args.warmups else "runs"].append(record)
                        item["result"] = record.get("result", "")
                        if record["status"] != "ok" or record["result"] != expected:
                            item["status"] = record["status"] if record["status"] != "ok" else "wrong_answer"
                            item["error"] = record.get("error", "Measured/warmup run failed or disagreed with Python")
                            break
                    if item["status"] == "pending":
                        item.update(summarize(item["runs"]))
                        item.update(status="ok", speedup=baseline["best_seconds"] / item["best_seconds"],
                                    median_speedup=baseline["median_seconds"] / item["median_seconds"])
                        item["faster"] = item["speedup"] > 1
            except ProviderError as exc:
                item.update(status="generation_failed", error=redact(str(exc)))
            except (OSError, ValueError) as exc:
                item.update(status="error", error=redact(str(exc)))
            finally:
                save_report(root, report)
                print(f"  {item['status']}", flush=True)
                if item.get("error"):
                    detail = "".join(c if c.isprintable() else " " for c in redact(item["error"]))
                    print("  " + detail[:240], flush=True)
        report["status"] = "completed"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        raise
    except (OSError, ValueError) as exc:
        report.update(status="failed", error=redact(str(exc)))
        raise
    finally:
        save_report(root, report)
    print("\n" + table(report))
    print(f"\nReport: {root / 'report.md'}")
    return 0 if any(item["status"] == "ok" for item in report["models"]) else 1
