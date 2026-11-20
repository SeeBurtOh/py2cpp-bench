"""Bounded subprocesses with clean environments and macOS filesystem isolation."""

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import platform
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import time


OUTPUT_LIMIT = 1024 * 1024


@dataclass
class Execution:
    command: list[str]
    returncode: int
    wall_seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False

    @property
    def ok(self):
        return self.returncode == 0 and not self.timed_out and not self.output_limited

    def to_dict(self):
        return asdict(self)


def clean_environment(cwd):
    # Allowlist: provider keys, tokens, proxy credentials, and loader injection
    # variables must never be inherited by either Python or generated C++.
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(cwd), "TMPDIR": str(cwd), "TMP": str(cwd), "TEMP": str(cwd),
        "LANG": "C", "LC_ALL": "C", "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def developer_toolchain_root():
    """Resolve the selected Apple tools, including versioned full Xcode bundles."""
    try:
        selected = subprocess.run(
            ["/usr/bin/xcode-select", "--print-path"], capture_output=True,
            text=True, timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if selected.returncode or not selected.stdout.strip():
        return None
    directory = Path(selected.stdout.strip()).resolve()
    # Libraries outside Contents/Developer can belong to the same trusted app bundle.
    for parent in directory.parents:
        if parent.suffix == ".app":
            return str(parent)
    if directory in (Path("/"), Path.home(), Path.home().parent):
        return None
    return str(directory)


def sandbox_profile(cwd, compiler=None, allow_processes=False, read_paths=(), toolchain_root=None):
    read_roots = {
        "/System", "/usr", "/bin", "/sbin", "/Library/Developer",
        "/Library/Apple", "/Library/Preferences/com.apple.dt.Xcode.plist",
        "/private/var/db/dyld", "/private/var/db/xcode_select_link",
        "/private/var/db/timezone", "/private/etc/localtime",
        "/opt/homebrew/Cellar", "/opt/homebrew/opt", "/opt/homebrew/lib",
        "/opt/homebrew/bin", "/opt/homebrew/include", "/opt/homebrew/share",
        str(Path(sys.base_prefix).resolve()), str(Path(sys.prefix).resolve()),
        str(Path(sys.executable).resolve()),
    }
    if compiler:
        path = Path(compiler).resolve()
        # For custom toolchains, allow their installation prefix, never '/' or home.
        prefix = path.parent.parent
        if prefix not in (Path("/"), Path.home(), Path.home().parent):
            read_roots.add(str(prefix))
        read_roots.add(str(path))
    if allow_processes and toolchain_root:
        read_roots.add(toolchain_root)
    reads = "\n".join(f"  (subpath {json.dumps(p)})" for p in sorted(read_roots))
    reads += "\n" + "\n".join(f"  (literal {json.dumps(str(Path(p).resolve()))})" for p in read_paths)
    return f"""(version 1)
(deny default)
(allow process-exec)
{'(allow process-fork)' if allow_processes else ''}
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup
  (global-name "com.apple.system.logger")
  (global-name "com.apple.system.notification_center")
  (global-name "com.apple.cfprefsd.daemon")
  (global-name "com.apple.cfprefsd.agent"))
(allow file-read-metadata)
(allow file-read*
{reads}
  (literal "/")
  (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))
(allow file-read* file-write* (subpath {json.dumps(str(Path(cwd).resolve()))}))
(allow file-write* (literal "/dev/null"))
"""


class Runner:
    def __init__(self, isolation="auto", compiler=None):
        if os.name != "posix":
            raise ValueError("This release requires macOS or Linux (use a VM on Windows).")
        if isolation == "auto":
            if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
                raise ValueError("Native isolation currently requires macOS sandbox-exec. "
                                 "In a disposable Linux VM, explicitly use --isolation none.")
            isolation = "macos"
        self.isolation = isolation
        self.compiler = compiler
        self.toolchain_root = developer_toolchain_root() if isolation == "macos" and compiler else None

    def run(self, command, cwd, timeout, *, allow_processes=False, read_paths=()):
        cwd = Path(cwd).resolve()
        original = [str(value) for value in command]
        command = original
        if self.isolation == "macos":
            profile = sandbox_profile(cwd, self.compiler, allow_processes, read_paths, self.toolchain_root)
            command = ["/usr/bin/sandbox-exec", "-p", profile, *command]

        def limits():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            cpu = max(1, math.ceil(timeout) * (os.cpu_count() or 1))
            hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
            if hard != resource.RLIM_INFINITY:
                cpu = min(cpu, hard)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            file_limit = 256 * 1024 * 1024
            hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
            if hard != resource.RLIM_INFINITY:
                file_limit = min(file_limit, hard)
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))

        started = time.perf_counter()
        process = subprocess.Popen(
            command, cwd=cwd, env=clean_environment(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            preexec_fn=limits,
        )
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        timed_out = output_limited = False

        def kill_group():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Some enclosing developer sandboxes permit signaling a direct child
                # but prohibit process-group signaling. Still terminate that child.
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        try:
            with selectors.DefaultSelector() as selector:
                for name in buffers:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    remaining = timeout - (time.perf_counter() - started)
                    if remaining <= 0:
                        timed_out = True
                        kill_group()
                        break
                    for key, _ in selector.select(min(remaining, 0.05)):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        target = buffers[key.data]
                        room = OUTPUT_LIMIT - len(target)
                        target.extend(data[:room])
                        if len(data) > room:
                            output_limited = True
                            kill_group()
                    if output_limited:
                        break
                if not timed_out and not output_limited:
                    remaining = max(0.001, timeout - (time.perf_counter() - started))
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        kill_group()
        finally:
            # Also reap ordinary descendants after a successful run; no background jobs.
            kill_group()
            process.wait()
            process.stdout.close()
            process.stderr.close()
        return Execution(original, process.returncode, time.perf_counter() - started,
                         buffers["stdout"].decode("utf-8", errors="replace"),
                         buffers["stderr"].decode("utf-8", errors="replace"),
                         timed_out, output_limited)


def machine_info(compiler):
    info = {
        "os": platform.system(), "os_release": platform.release(),
        "architecture": platform.machine(), "logical_cpus": os.cpu_count(),
        "python": platform.python_version(), "compiler": str(compiler),
    }
    version = subprocess.run([compiler, "--version"], capture_output=True, text=True, timeout=10)
    if version.returncode:
        raise ValueError("Compiler --version failed: " + version.stderr[:1000])
    info["compiler_version"] = version.stdout.strip()
    if platform.system() == "Darwin":
        for field in ("machdep.cpu.brand_string", "hw.memsize", "hw.physicalcpu",
                      "hw.perflevel0.physicalcpu", "hw.perflevel1.physicalcpu"):
            result = subprocess.run(["/usr/sbin/sysctl", "-n", field], capture_output=True,
                                    text=True, timeout=5)
            if result.returncode == 0:
                info[field] = result.stdout.strip()
    elif Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu_model"] = line.split(":", 1)[1].strip()
                break
    return info


def compiler_flags():
    native = "-mcpu=native" if platform.machine().lower() in ("arm64", "aarch64") else "-march=native"
    return ["-std=c++23", "-O3", native, "-flto", "-DNDEBUG", "-pthread"]
