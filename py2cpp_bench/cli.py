"""Command-line entry points."""

import argparse
import json
import math
from pathlib import Path
import shutil
import sys

from . import __version__
from .benchmark import probe, run_benchmark
from .credentials import clear_credentials
from .providers import DEFAULT_MODELS, KEY_NAMES, redact
from .runner import Runner, compiler_flags, machine_info
from .setup import guided_setup


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def parser():
    root = argparse.ArgumentParser(prog="py2cpp-bench", description="One-shot Python-to-C++ optimization benchmarks. Bring your own API keys.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="guided masked key entry and optional OS-keychain storage")
    setup.add_argument("--provider", action="append", choices=tuple(KEY_NAMES), help="provider to configure; repeat to select several")
    setup.add_argument("--storage", choices=("keychain", "prompt", "environment"), help="choose storage without the menu")
    setup.add_argument("--remove", choices=tuple(KEY_NAMES), help="remove only this app's saved key for a provider")
    doctor = commands.add_parser("doctor", help="probe machine, C++23 compiler and execution isolation (no API calls)")
    run = commands.add_parser("run", help="benchmark the bundled pi example or a standalone Python script")
    for sub in (doctor, run):
        sub.add_argument("--cxx", default="clang++", help="compiler executable (default: clang++)")
        sub.add_argument("--cxx-flag", action="append", help="append a compiler flag; use --cxx-flag=-FLAG")
        sub.add_argument("--isolation", choices=("auto", "none"), default="auto",
                         help="auto requires macOS sandbox-exec; none is for trusted code/disposable VMs")
        sub.add_argument("--compile-timeout", type=positive_float, default=120)
    run.add_argument("source", nargs="?", type=Path, help="standalone Python source; defaults to bundled pi.py")
    run.add_argument("--model", action="append", metavar="PROVIDER:MODEL", help="repeat to select models; defaults: " + ", ".join(DEFAULT_MODELS))
    run.add_argument("--ask-keys", action="store_true", help="privately prompt for missing keys for this run only; never save them")
    run.add_argument("--iterations", type=nonnegative_int, default=200_000_000, help="pi iterations (default: 200000000)")
    run.add_argument("--arg", action="append", help="argument for a custom script; use --arg=-VALUE for flags")
    run.add_argument("--check-args", action="append", metavar='\'["arg1", "arg2"]\'', help="additional undisclosed validation case, as a JSON array")
    run.add_argument("--timing-line", action="store_true", help="exclude final numeric elapsed-time line from custom script comparison; automatic for pi")
    run.add_argument("--python-runs", type=positive_int, default=1)
    run.add_argument("--cpp-runs", type=positive_int, default=3)
    run.add_argument("--warmups", type=nonnegative_int, default=0, help="unmeasured warmups for both languages (default: 0)")
    run.add_argument("--timeout", type=positive_float, default=120, help="wall seconds per program invocation")
    run.add_argument("--api-timeout", type=positive_float, default=180, help="HTTP socket timeout in seconds; no retries")
    run.add_argument("--max-output-tokens", type=positive_int, default=16384)
    run.add_argument("--seed", type=int, default=2026, help="validation input seed, saved for reproducibility")
    run.add_argument("--output", type=Path, default=Path("runs"), help="parent directory for unique run artifacts")
    run.add_argument("--demo", action="store_true", help="offline pipeline demo using handwritten C++ (no API keys or calls)")
    run.add_argument("--dry-run", action="store_true", help="save prompts and request bodies without executing programs or calling APIs")
    return root


def main(argv=None):
    clear_credentials()
    args = parser().parse_args(argv)
    try:
        if args.command == "setup":
            return guided_setup(args)
        if args.command == "doctor":
            compiler = shutil.which(args.cxx)
            if not compiler:
                raise ValueError(f"Compiler not found: {args.cxx}")
            info = machine_info(compiler)
            runner = Runner(args.isolation, compiler)
            flags = compiler_flags() + (args.cxx_flag or [])
            info["isolation"] = runner.isolation
            info["compiler_flags"] = flags
            info["preflight"] = probe(runner, compiler, flags, args.compile_timeout)
            info["api_key_variables"] = KEY_NAMES
            print(json.dumps(info, indent=2))
            return 0
        return run_benchmark(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Completed reports or previously saved keys are unchanged.", file=sys.stderr)
        return 130
    except EOFError:
        print("\nSetup cancelled. Any keys already saved remain in the OS keychain.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        message = "".join(c if c.isprintable() or c == "\n" else " " for c in redact(str(exc)))
        print("Error: " + message, file=sys.stderr)
        return 2
    finally:
        clear_credentials()
