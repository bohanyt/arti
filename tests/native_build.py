"""Build cloud-safe native probes once per worker, with isolated executable output."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import pytest

_BUILT = {}


def dotnet_exe():
    """The .NET SDK launcher (DOTNET_EXE or `dotnet` on PATH); skips the test when there is none.

    Everything that builds or runs the C# protocol/hold-runtime probes needs it. Without the SDK the
    probe is not evidence either way, so the test skips with this reason instead of erroring."""
    exe = os.environ.get("DOTNET_EXE") or shutil.which("dotnet")
    if not exe:
        pytest.skip("dotnet SDK not installed (needed to build/run the C# protocol probes)")
    return exe


@contextmanager
def _build_lock(path):
    # Workers share MSBuild's project obj/, but never bin/ or running apphosts.
    with path.open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 180
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("native probe build lock")
                time.sleep(.05)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)



def _run_build(args, cwd):
    # A cancelled/expired test owns the compiler descendants too. Do not leave
    # an orphan csc process holding output pipes or consuming the lab CPU.
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", **flags) as process:
        try:
            stdout, stderr = process.communicate(timeout=180)
        except BaseException:
            if os.name == "nt":
                if process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=10)
            else:
                import signal
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=10)
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def build_native(project, tmp_path_factory):
    project = Path(project).resolve()
    base = tmp_path_factory.getbasetemp()
    key = (project, base)
    if key not in _BUILT:
        output = base / "native" / project.stem
        output.mkdir(parents=True, exist_ok=True)
        # The same project can be built by a serial run and xdist workers in
        # different pytest temp trees. Scope the lock to the resolved project.
        digest = hashlib.sha256(str(project).encode()).hexdigest()[:20]
        lock = Path(tempfile.gettempdir()) / ("arti-pytest-native-" + digest + ".lock")
        with _build_lock(lock):
            if os.name == "nt":
                guard = subprocess.run('tasklist | findstr /i "smapi StardewModdingAPI"',
                    shell=True, capture_output=True, text=True)
                assert guard.returncode == 1, "SMAPI running or process check unavailable"
            built = _run_build([dotnet_exe(), "build", str(project), "-c", "Release",
                "--nologo", "-o", str(output), "-m:1", "-p:UseSharedCompilation=false"],
                cwd=project.parents[1])
            (output / "build.log").write_text(built.stdout + built.stderr, encoding="utf-8")
            assert built.returncode == 0, built.stdout + built.stderr
        _BUILT[key] = output
    return _BUILT[key]
