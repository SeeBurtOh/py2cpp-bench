# Python → C++ Bench

Give several models the same Python program and one chance to replace it with faster C++.
Compile each answer locally, compare its output with Python, and rank correct candidates by
measured speedup. Models may change algorithms, use all CPU cores, and use native SIMD.

**Local CLI · bring your own API keys · one generation per model · dependency-free core**

The bundled example is a 200-million-iteration pi estimate. Its Python calculation and
`Result: …` / `Execution Time: … seconds` output preserve the original specimen. Iteration
count and the two calculation parameters are runtime inputs. There is also an offline demo
using a handwritten serial C++ translation; demo timings are never labeled as model results.

## Quick start

Requires Python 3.11+ and a C++23-capable Clang toolchain. macOS is the supported native
isolation target for this first release. On a Mac without developer tools, run
`xcode-select --install` first.

From the repository directory, no package installation is necessary:

```bash
python3 -m py2cpp_bench doctor
python3 -m py2cpp_bench run --demo --iterations 1000000
```

Run the full offline specimen:

```bash
python3 -m py2cpp_bench run --demo
```

Example measured offline on an Apple M4 with Python 3.15 and Apple Clang 21, using
200,000,000 iterations and external wall time:

| Implementation | Printed result | Best seconds | Speedup |
| --- | --- | --- | --- |
| Original Python | `3.141592656089` | 10.665829 | 1.00× |
| Handwritten serial C++ demo | `3.141592656089` | 0.221110 | 48.24× |

This verifies the harness and original specimen; it is not a model comparison or a guarantee
of performance on another machine. The C++ demo uses the same arithmetic order, without SIMD
intrinsics or explicit multithreading.

Optional installation provides the shorter `py2cpp-bench` command:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
py2cpp-bench --help
```

## Bring your own keys

Start with the guided setup command in an interactive terminal:

```bash
python3 -m py2cpp_bench setup
```

Choose providers, then choose one of three options:

1. **OS keychain:** enter and confirm each key with terminal echo disabled. Keys are saved
   in macOS Keychain or Linux Secret Service, under service `py2cpp-bench.api-keys` and the
   selected provider's account name. Existing saved keys are kept unless you choose to replace
   them. Blank input skips a provider. The OS may ask you to unlock or approve access.
2. **Private entry for each run:** nothing is stored. Setup prints a command using `--ask-keys`;
   that run asks for missing credentials and keeps them only in the parent process's memory.
3. **Environment variables:** setup prints shell commands that prompt privately, without
   putting the entered key in shell history or writing a `.env` file.

For keychain storage, install the optional dependency in your virtual environment first:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[keychain]'
python -m py2cpp_bench setup --provider openai --storage keychain
```

Once saved, `run` finds keys automatically. Keychain support uses only the native macOS or
Linux Secret Service backend from [keyring](https://keyring.readthedocs.io/); custom/third-party,
plaintext, and null backends are not selected. If storage is unavailable, setup fails before
asking for a key. It never writes a plaintext substitute or installs dependencies automatically.
Linux requires a running desktop Secret Service; this does not add a Linux execution sandbox.
Keychain protection is governed by the OS and its access policies, not exclusive ownership by
this application. Keys are passed through the native library, never subprocess command arguments.

To avoid persistent storage entirely:

```bash
python3 -m py2cpp_bench run --ask-keys --model openai:gpt-6.1-sol
```

Lookup order is **environment variable → key entered for this invocation → saved OS-keychain
entry**. Existing environment variables override saved keys; unset an old variable to use a new
saved key. `--ask-keys` prompts only for missing providers, once per provider. Keys entered for
one run are not exported into the environment or saved. Key entry requires a real terminal and
refuses to fall back to echoing input. `doctor`, `--demo`, and `--dry-run` do not read the keychain
or prompt for keys, even with `--ask-keys`.

Remove this app's saved key with confirmation:

```bash
python3 -m py2cpp_bench setup --remove openai
```

This does not change environment variables or revoke the key at its provider. Setup makes no
provider requests, incurs no API charges, and does not validate account/model access. It never
prints entered or stored keys. Previously completed saves remain if setup is interrupted.

Environment variables remain supported without the optional keychain dependency:

| Provider | Environment variable | Default requested model ID |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `gpt-6.1-sol` |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` |
| xAI | `XAI_API_KEY` | `grok-4.3` |
| Google | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | `gemini-4-argon` |

The defaults reflect this project's initial comparison list, **not a guarantee of current
API availability or account access**. Supply any model ID supported by the selected adapter.
Unavailable models produce an error row; there is no silent substitution or retry.
Models without credentials receive a `missing_key` row. If no selected provider has a key,
the CLI stops before running the baseline.

To enter an OpenAI key without placing its value in shell history (Bash or Zsh):

```bash
export OPENAI_API_KEY="$(python3 -c 'import getpass, warnings; warnings.simplefilter("error", getpass.GetPassWarning); print(getpass.getpass("OpenAI API key: "))')"
python3 -m py2cpp_bench run --model openai:gpt-6.1-sol
```

Use the equivalent environment variable for each other provider. The included `.env.example`
documents the names; the application deliberately does not load `.env` files automatically.
Do not commit credentials. Each user pays their selected providers directly. This repository
contains no shared API key, hosted proxy, or telemetry endpoint.

Run all four configured defaults, or explicitly select a subset:

```bash
python3 -m py2cpp_bench run

python3 -m py2cpp_bench run \
  --model anthropic:claude-sonnet-5-5 \
  --model google:gemini-4-argon
```

Running a live benchmark sends the selected source and a machine/compiler description to each
selected provider. It makes one generation request per configured model, even if the answer
fails to compile. There are no tools, repair rounds, or HTTP retries. Requests use provider
defaults for sampling/reasoning and the configured output-token ceiling; these settings are
not necessarily equivalent compute budgets across providers. Token usage is saved when returned.

Inspect the exact prompts and request bodies before making any API calls:

```bash
python3 -m py2cpp_bench run --dry-run
```

This only inspects machine/compiler metadata and writes artifacts. It does not compile or
execute programs. Neither `doctor`, `--demo`, nor `--dry-run` makes model API requests.

## Benchmark rules

1. Probe compilation and isolated execution before spending API credits.
2. Run Python once for the measured baseline (configurable).
3. Run additional Python inputs to establish reference outputs. The pi example checks zero,
   small and seeded larger iteration counts, plus different calculation parameters.
4. Give each model the same system and user prompts, including the source, machine details,
   compiler command, runtime interface, and headline benchmark arguments. Do not disclose the
   additional test inputs or reference outputs in the prompt.
5. Save the response, remove one enclosing Markdown code fence if present, and compile once.
6. Check the C++ against the extra inputs, then run the headline case three times. Every
   validation, warmup, and measured run must match its Python result.
7. Rank correct candidates by `best Python wall seconds / best C++ wall seconds`. A correct
   candidate is marked faster only when this quotient is greater than one.

Matching the Python approximation is the objective; calculating a more accurate value of pi
does not excuse different printed digits. Alternate algorithms and parallel reductions are
allowed, but floating-point rounding differences can cause an exact-output failure. Tests
provide evidence on sampled inputs, not a proof of equivalence for every possible input.
Hardcoding answers or detecting the benchmark is prohibited by the prompt; generated code
remains available for inspection.

For the pi example, the result line is compared exactly and the final elapsed-time line must
be well formed, finite, and nonnegative. The reported internal time is diagnostic only.
The leaderboard uses **external process wall time**, including interpreter/executable startup,
sandbox startup, output, and process cleanup. Generation, compilation, and validation are
excluded. Compiler and generation durations are saved separately. Tiny workloads may be
dominated by startup costs; the 200-million-iteration case is the intended headline workload.

Benchmarks run serially. The same compiled executable is reused across runs, with an empty
working directory per invocation. The default is zero warmups, one Python run, and three C++
runs, preserving the original experiment. For a more balanced comparison:

```bash
python3 -m py2cpp_bench run --python-runs 3 --cpp-runs 3 --warmups 1
```

Best and median wall times are both displayed. `report.json` also includes median-to-median
speedup and every individual timing. Avoid other heavy workloads when comparing candidates.
A single generated answer measures that attempt, not a model's overall capability.

## Compiler and model freedom

The default command on Apple Silicon is:

```bash
clang++ -std=c++23 -O3 -mcpu=native -flto -DNDEBUG -pthread main.cpp -o main
```

On x86 the CLI uses `-march=native`. It probes the complete command and fails clearly if the
toolchain cannot support it. Specify `--cxx /path/to/clang++` or append flags with
`--cxx-flag=-FLAG`. All models receive and use the same command. Standard C++ threads and
platform SIMD intrinsics are available. OpenMP and GPU frameworks are not linked by default;
the model must use what the supplied command supports. `-ffast-math` is not enabled by default.

The models choose their implementations, but do not choose correctness checks, timing methods,
or resource limits. Unsupported output, compilation errors, wrong answers, runtime failures,
truncation, and timeouts remain visible in the report without a valid speedup.

## Use another Python program

The initial scope is **standalone, deterministic, CPU-oriented scripts** with command-line
arguments and stdout results. There is no project/dependency migration or interactive stdin.
Local imports, data files, network calls, and filesystem side effects are not supported inputs
in isolated mode. The app is an experimental optimization harness, not a general Python compiler.

```bash
python3 -m py2cpp_bench run ./my_program.py \
  --arg 1000000 \
  --check-args '["10"]' \
  --check-args '["12345"]' \
  --model openai:YOUR_MODEL_ID
```

For custom scripts, stdout is compared in full after CRLF normalization and removal of one
final newline. Add `--timing-line` only when the final output line contains a bare number of
seconds or `Execution Time: NUMBER seconds`; that line will be parsed and excluded from
correctness comparison. Custom scripts have no automatic extra test cases, so supply
`--check-args` to exercise the runtime inputs. Use `--arg=-VALUE` for arguments beginning with `-`.

## Artifacts and exit status

Each invocation gets a unique directory under `runs/` (override with `--output`). Runs are
ignored by Git because they can contain proprietary source and provider responses.

```text
runs/<UTC timestamp>-<unique suffix>/
  source.py
  prompt.json
  report.json
  report.md
  01-openai-<model>/
    request.json
    response.json
    main.cpp
    compiler.stderr.txt
```

The JSON report includes machine/compiler versions, source hash, validation seed and cases,
request counts, returned model IDs, token usage, compilation diagnostics, and stdout/stderr
for each execution. Results are saved after each candidate, including failures. Compiled
executables are temporary and removed when the run completes. No credentials or authorization
headers are written to reports; known environment, entered, and loaded keychain values are
redacted at persistence boundaries. Keychain lookups are never triggered just to redact text.
Inspect source and reports before choosing which artifacts to publish.

Exit codes: `0` means at least one correct candidate (or a successful doctor/dry run), `1`
means no candidate passed, `2` means setup/baseline failure, and `130` means interrupted.
A successful exit does not imply every provider succeeded or that the candidate was faster.

## Execution boundary

By default on macOS, both Python and C++ run under `sandbox-exec` with a read allowlist for
system/toolchain/runtime files, access to their own temporary working directory, and no network
or access to project/home data outside explicitly allowed runtime installations. The compiled
binary is readable but not writable during execution. Runtime child-process creation is denied;
native threads are allowed. Compilation has a separate allowance to launch the linker/toolchain.

Child environments are constructed from an allowlist, so API keys, other tokens, proxy
credentials, and loader-injection variables are not inherited. Output is capped at 1 MiB per
stream; wall deadlines, CPU limits, a per-file size limit, and process-group cleanup bound runs.
There is no hard aggregate memory/disk quota. This is defense in depth, **not a hardened service
for arbitrary hostile uploads**. Runtime/toolchain installations themselves must be trusted.

The tool fails closed if native isolation cannot start, including when run inside a host
environment that prohibits nested macOS sandboxes. It never silently falls back. This version
has no Linux native sandbox backend; use a disposable VM and explicitly pass `--isolation none`
there. That option also permits trusted local experiments, but removes filesystem/network/fork
restrictions; credential environment filtering and execution deadlines still apply. Windows
requires a suitable VM. macOS `sandbox-exec` is a legacy OS facility whose availability may change.

## Development

```bash
python3 -m unittest discover -s tests -v
# On macOS outside an enclosing sandbox, also verify OS-enforced denials:
PY2CPP_TEST_SANDBOX=1 python3 -m unittest discover -s tests -v
```

Tests cover output matching, one-request behavior, provider response parsing, credential
handling, masked setup/confirmation, native-backend selection, keychain failure/removal, credential
precedence and redaction, timeout/output limits, failure ranking, and the reporting pipeline. Keychain
reads/writes are mocked in automated tests; they never modify your real saved credentials. The offline demo
exercises real Python, Clang, and C++ execution. Provider APIs are mocked in tests; a successful
offline demo does not establish access to any live model.

Adapters use the documented [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create),
[Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create),
[xAI Chat Completions API](https://docs.x.ai/developers/rest-api-reference/inference/chat-completions), and
[Gemini generateContent API](https://ai.google.dev/api/generate-content).
