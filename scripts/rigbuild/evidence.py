"""One evidence folder per run: logged steps, create-new JSON, the source identity and the privacy guard (ADR 245).

`Evidence.run()` is the hand procedures' `run()`: admission first, one create-new log per step, a `runs.jsonl` row
with the command, exit, seconds and the admitting row. `prepare()` is `prepare_tb.py` plus the checkout and
`package_guard` steps the briefs did by hand. Every child runs in the build checkout with the environment of
`run.py` (offline NuGet, one MSBuild worker, no deploy, no ZIP) and the `bootstrap/` privacy guard on PYTHONPATH.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import time
from contextlib import redirect_stdout

from scripts.rigbuild import machine
from scripts.rigbuild.machine import Refused

TOOL_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = Path(__file__).resolve().with_name("bootstrap")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hashes(folder):
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): sha(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def build_env(cfg, checkout, evidence, task, base_temp, environ=None):
    """run.py's ENV, with the privacy guard's parameters instead of its hard-coded paths."""
    environ = dict(os.environ if environ is None else environ)
    game = Path(cfg.game).as_posix()
    environ.update(
        PYTHONPATH=str(BOOTSTRAP) + os.pathsep + str(checkout), PYTEST_PLUGINS="rigbuild_reporting",
        PYTHONDONTWRITEBYTECODE="1", DOTNET_ROOT=str(Path(cfg.dotnet).parent), DOTNET_EXE=str(cfg.dotnet),
        DOTNET_ROLL_FORWARD="Major", DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
        ARTI_A5_PYTHON=str(cfg.python312), ARTI_A5_P2=str(checkout), ARTI_GAME_RUNTIME_PATH=game,
        EnableModDeploy="false", EnableModZip="false", MSBUILDDISABLENODEREUSE="1", UseSharedCompilation="false",
        RestoreSources=str(cfg.nuget_offline), NuGetAudit="false", ARTI_IL_GAME_PATH=game,
        ARTI_RIGBUILD_TASK=task, ARTI_RIGBUILD_EVIDENCE=str(evidence), ARTI_RIGBUILD_CHECKOUT=str(checkout),
        ARTI_RIGBUILD_BASETEMP=str(base_temp), ARTI_RIGBUILD_PRIVATE_ROOT=str(cfg.private_root),
        ARTI_RIGBUILD_SAVES=str(cfg.saves))
    return environ


def install_guard(env):
    """Load bootstrap/sitecustomize.py from this tool and install its audit hook in THIS process (the hand
    procedures ran their chain runner as a child with the guard on PYTHONPATH; here the runner is this process)."""
    spec = importlib.util.spec_from_file_location("rigbuild_privacy_guard", BOOTSTRAP / "sitecustomize.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install(module.params_from(env))
    return module


class Evidence:
    def __init__(self, folder, *, cfg, checkout, head, env, policy, task, say=print, popen=subprocess.Popen,
                 read_machine=None, sleep=time.sleep, mono=time.monotonic, now=machine.clock, git_fn=None):
        self.E, self.cfg, self.C, self.head = Path(folder), cfg, Path(checkout), head
        self.env, self.policy, self.task, self.say = env, policy, task, say
        self.popen, self.sleep, self.mono, self.now = popen, sleep, mono, now
        self.read_machine = read_machine or (lambda: machine.snapshot(cfg))
        self._git = git_fn
        self.slot = machine.Slot(cfg.slot, task, policy=policy, admit_fn=self.gate, events=self.E / "slot-events.jsonl",
                                 sleep=sleep, mono=mono, now=now, say=say)

    # --- files -------------------------------------------------------------------------------------------------
    def path(self, name):
        return self.E / name

    def write(self, name, value):
        with (self.E / name).open("x", encoding="utf8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def load(self, name):
        return json.loads((self.E / name).read_text(encoding="utf-8-sig"))

    def exists(self, name):
        return (self.E / name).exists()

    def append(self, name, row):
        with (self.E / name).open("a", encoding="utf8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def block(self, detail, outcome="NOT READY"):
        if (self.E / "BLOCKED.md").exists():
            return
        with (self.E / "BLOCKED.md").open("x", encoding="utf8") as f:
            f.write(f"{outcome}\n\n{self.now()['local']}\nSource: {self.head}\n\n{detail}\n"
                    "No product/test repair, staging, commit, push, PR, deployment or session.\n")
        self.say(f"{outcome}: {detail}")

    # --- source ------------------------------------------------------------------------------------------------
    def git(self, *args):
        if self._git is not None:
            return self._git(*args)
        return subprocess.check_output(["git", *args], cwd=self.C, text=True, creationflags=NO_WINDOW).strip()

    def identity(self):
        if self.git("rev-parse", "HEAD") != self.head or self.git("status", "--porcelain"):
            raise Refused("source identity/cleanliness changed: " + str(self.C))

    # --- machine -----------------------------------------------------------------------------------------------
    def gate(self, label, pytest=False):
        return machine.admit(self.policy, label, read=self.read_machine, pytest=pytest, identity=self.identity,
                             record=lambda row: self.append("resource-gates.jsonl", row), sleep=self.sleep,
                             mono=self.mono, say=self.say)

    def run(self, name, args, pytest=False):
        admission = self.gate(name, pytest)
        args = [str(a) for a in args]
        if self.policy.assert_slot_owner and (Path(args[0]) == Path(self.cfg.dotnet) or pytest):
            if not self.slot.owned():
                raise Refused("pytest slot ownership changed before " + name)
        log = self.E / (name + ".log")
        if log.exists():
            raise Refused("step log already exists (a step never runs twice): " + str(log))
        start = self.mono()
        self.say("START " + name)
        with log.open("xb") as f:
            p = self.popen(args, cwd=self.C, env=self.env, stdout=f, stderr=subprocess.STDOUT,
                           creationflags=NO_WINDOW)
            (self.E / "active.json").write_text(json.dumps(dict(name=name, pid=p.pid)), encoding="utf8")
            rc = p.wait()
        row = dict(name=name, source_sha=self.head, command=args, cwd=str(self.C), exit=rc,
                   seconds=round(self.mono() - start, 3), log=str(log), admission=admission,
                   finished_utc=self.now()["utc"])
        self.append("runs.jsonl", row)
        self.say(f"DONE {name} exit={rc} seconds={row['seconds']}")
        return rc

    def require(self, name, args, pytest=False):
        rc = self.run(name, args, pytest)
        if rc:
            raise RuntimeError(f"{name} failed exit={rc}; see {self.E / (name + '.log')}")

    def record(self, name, rc, **extra):
        """A step done in this process (no child): one runs.jsonl row like the others."""
        self.append("runs.jsonl", dict(name=name, source_sha=self.head, command=["in-process", name], exit=rc,
                                       seconds=0, finished_utc=self.now()["utc"], **extra))


def resolve(checkout, commit, git_fn=None):
    run = git_fn or (lambda *a: subprocess.check_output(["git", *a], cwd=checkout, text=True,
                                                        creationflags=NO_WINDOW).strip())
    try:
        return run("rev-parse", "--verify", commit + "^{commit}")
    except subprocess.CalledProcessError as exc:
        raise Refused(f"source {commit} is not a commit in {checkout}") from exc


def package_guard(checkout, log=None):
    """`python -m scripts.livepath.package_guard <checkout>` (brief step 1): exit 1 names private paths only."""
    from scripts.livepath import package_guard as guard
    out = io.StringIO()
    with redirect_stdout(out):
        rc = guard.main([str(checkout)])
    text = out.getvalue()
    if log is not None:
        Path(log).write_text(text, encoding="utf8")
    if rc:
        raise Refused(text.strip())
    return text.strip()


def prepare(ev, *, source, detach=False, fetch=None, previous_native=None, require_detached=False):
    """Checkout verified or detached at the source, clean, no private state; tracked-file list and identity."""
    if fetch:
        subprocess.check_call(["git", "fetch", fetch], cwd=ev.C, creationflags=NO_WINDOW)
    head = resolve(ev.C, source, ev._git)
    ev.head = head
    current = ev.git("rev-parse", "HEAD")
    if ev.git("status", "--porcelain"):
        raise Refused("checkout is not clean: " + str(ev.C))
    if current != head:
        if not detach:
            raise Refused(f"checkout {ev.C} is at {current}, not {head}; pass --detach to check it out")
        ev.git("checkout", "--detach", head)
    detached = ev.git("rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    if require_detached and not detached:
        raise Refused(f"checkout {ev.C} is on a branch; the package procedure builds a detached checkout")
    ev.identity()
    guard_line = package_guard(ev.C, ev.E / "package-guard.log")
    ev.write("ct-tracked-files.json", [p for p in ev.git("ls-files", "-z").split("\0") if p])
    changed = None
    if previous_native:
        changed = ev.git("diff", "--name-only", previous_native, "HEAD", "--", "*.cs", "*.csproj").splitlines()
    identity = dict(source_sha=head, source_tree=ev.git("rev-parse", "HEAD^{tree}"),
                    previous_native_build_source_sha=previous_native, native_changed_files_since_previous=changed,
                    detached=detached, checkout_clean=True, package_guard=guard_line, tool_sha=tool_sha())
    ev.write("source-identity.json", identity)
    return identity


def tool_sha():
    """The commit of the checkout this tool runs from (it may differ from the source it builds)."""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=TOOL_ROOT, text=True,
                                       creationflags=NO_WINDOW).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def free_gib(cfg):
    import shutil
    return shutil.disk_usage(Path(cfg.rig_root).anchor or "C:\\").free / 1024**3
