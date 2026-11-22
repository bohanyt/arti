"""Device-free code execution: real handles/pipe, lifted bridge and soak code.

No bridge imports, model loads, logins, game launches or real harness runs.
"""
import ast
import asyncio
import inspect
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import pytest

from scripts.free_play import soak
from scripts.livepath import bridge, rehearse


@pytest.mark.parametrize("reply,expected", [(None, "incharacter"), ("provider reply", "codex")])
def test_actual_fallback_chain_and_playback_source(reply, expected, monkeypatch, capsys):
    tree = ast.parse((rehearse.ROOT / "arti_bridge.py").read_text(encoding="utf-8"))
    chain = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_cursor_reply_with_fallback")
    cursor = NS(prewarm=lambda cfg: False)
    monkeypatch.setitem(sys.modules, "arti_cursor_agent", cursor)
    async def execute(lane, fn, *args):
        return fn(*args)
    def unavailable(*a):
        raise RuntimeError("offline")
    scope = dict(asyncio=asyncio, os=os, CONFIG={},
        arti_executors=NS(run=execute), arti_agy_agent=NS(prewarm=lambda cfg: None),
        arti_codex_agent=NS(prewarm=lambda cfg: None, send_turn=lambda *a: reply),
        _codex_cfg_untuk_kelas=lambda text, cfg: cfg, _sentences_or_empty=lambda text: [],
        pick_groq_model=lambda *a: "offline", groq_chat_completion=unavailable,
        _extract_trigger_message=lambda text: text, incharacter_fallback_reply=lambda text: "canned",
        _attention_no_provider_fallback=lambda trigger_type: False)
    exec(compile(ast.Module(body=[chain], type_ignores=[]), "actual-fallback", "exec"), scope)
    _, _, source = asyncio.run(scope[chain.name]("system", "content", "owner", config=dict(
        codex_primary_voice=True, _codex_primary_tes=True), trigger_type="mic"))
    assert source == expected
    # Execute the actual guarded witness at playback completion, with no os in its namespace.
    witness = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                   and any(isinstance(s, ast.ImportFrom) and any(a.name == "reply_source" for a in s.names) for s in n.body))
    call = dict(provider="codex" if source == "codex" else "cursor_chain", local_fallback=source == "incharacter")
    exec(compile(ast.Module(body=[witness], type_ignores=[]), "actual-playback-witness", "exec"), dict(
        CONFIG=dict(rehearsal_parent_pid=1), voiced=True, llm_call=call, _stardew_speech=None,
        ai_reply="PRIVATE_REPLY", turn_id="test", trigger=NS(text="PRIVATE_OWNER", trigger_type="mic")))
    row = json.loads(capsys.readouterr().out.split("[Livepath] ")[-1])
    assert row["source"] == expected and "PRIVATE_OWNER" not in json.dumps(row) and "PRIVATE_REPLY" not in json.dumps(row)


@pytest.mark.parametrize("late", [False, True])
def test_actual_postprocessor_canned_replacement_is_not_credited_to_codex(late):
    tree = ast.parse((rehearse.ROOT / "arti_bridge.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "post_process_response")
    scope = dict(CONFIG={}, re=re, _CJK_RE=re.compile("NEVER_MATCH"),
        _shorten_viewer_handles=lambda text, user: text, get_arti_reply_limits=lambda *a: (2, 300),
        arti_reply_policy=NS(reply_hard_cap=lambda s, c, cfg: (s, c)),
        filter_meta_history_talk=lambda text: (text, 0) if late else ("", 1),
        is_narrator_reply=lambda text: text == "truncated", _truncate_reply_length=lambda *a, **kw: "truncated",
        incharacter_fallback_reply=lambda text: "canned", strip_tts_expression_tags=lambda text: text)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "actual-postprocessor", "exec"), scope)
    call = dict(provider="codex", rehearsal_source="codex")
    assert scope[node.name]("model reply", "owner", rehearsal_call=call) == "canned"
    assert bridge.reply_source(call) == "incharacter"


def test_source_uses_last_answer_after_retry_and_sanitizes_labels():
    assert bridge.reply_source(dict(provider="codex", local_fallback=True, rehearsal_source="codex")) == "codex"
    assert bridge.reply_source(dict(rehearsal_source="groq:openrouter/model")) == "openrouter"
    assert bridge.reply_source(dict(rehearsal_source="PRIVATE_REPLY")) == "unknown"


def test_actual_echo_fallback_has_a_canned_playback_witness(capsys):
    tree = ast.parse((rehearse.ROOT / "arti_bridge.py").read_text(encoding="utf-8"))
    witness = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and
                   any(isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and
                       any(k.arg == "source" and isinstance(k.value, ast.Constant) and k.value.value == "incharacter"
                           for k in s.value.keywords) for s in n.body))
    exec(compile(ast.Module(body=[witness], type_ignores=[]), "actual-echo-witness", "exec"), dict(
        CONFIG=dict(rehearsal_parent_pid=1), voiced=True, turn_id="echo",
        trigger=NS(text="PRIVATE_OWNER", trigger_type="mic")))
    row = json.loads(capsys.readouterr().out.split("[Livepath] ")[-1])
    assert row["source"] == "incharacter" and "PRIVATE_OWNER" not in json.dumps(row)


def test_voice_warmup_waits_before_ready_and_refuses_cold_provider():
    ticks = [0]
    provider = NS(prewarm=lambda cfg: None, is_warm=lambda: ticks[0] >= 1)
    async def sleep(seconds):
        ticks[0] += seconds
    asyncio.run(bridge.warm_voice({}, provider, timeout=2, clock=lambda: ticks[0], sleep=sleep))
    assert ticks[0] == 1
    provider.is_warm = lambda: False
    with pytest.raises(RuntimeError, match="warmup unavailable"):
        asyncio.run(bridge.warm_voice({}, provider, timeout=1, clock=lambda: ticks[0], sleep=sleep))


def test_rejoin_is_sampled_unknown_then_new_launch_recovers(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    soak.write(runtime / "batch-host/host-launch.json", dict(pid=10))
    soak.write(runtime / "farmhand-launch.json", dict(pid=20))
    monkeypatch.setattr(soak, "host_liveness", lambda *a, **k: dict(lost=False))
    monkeypatch.setattr(rehearse, "process_witness", lambda pid: (pid == 10, pid * 100))
    rp = rehearse.VoiceBridge(tmp_path, {}, seconds=120, clock=lambda: 0)
    rp.first_game_at = 0
    rp._sample_game(runtime, tmp_path / "host")  # old farmhand exits: no abort
    soak.write(runtime / "farmhand-launch.json", dict(pid=30))
    monkeypatch.setattr(rehearse, "process_witness", lambda pid: (True, pid * 100))
    rp._sample_game(runtime, tmp_path / "host")
    rows = [json.loads(s) for s in (tmp_path / "game-liveness.jsonl").read_text().splitlines()]
    assert rows[0]["alive"] is None and rows[1]["alive"] is True
    # Reuse of a witnessed PID is different from a new launch PID.
    monkeypatch.setattr(rehearse, "process_witness", lambda pid: (True, pid * 101))
    with pytest.raises(RuntimeError, match="lost"):
        rp._sample_game(runtime, tmp_path / "host")


@pytest.mark.parametrize("fault", [None, "first", "later", "crash", "day_start_stop"])
def test_actual_soak_loop_records_budget_and_all_faults(tmp_path, monkeypatch, fault):
    """Run the actual _run_soak outer/inner loop; only OS/game boundaries are fakes."""
    tree = ast.parse(inspect.getsource(soak._run_soak))
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.While) and
                isinstance(n.test, ast.BoolOp) and any(isinstance(v, ast.Name) and v.id == "days" for v in ast.walk(n.test)))
    ticks = [0]
    class Child:
        returncode = None
        def poll(self):
            if ticks[0] >= 3:
                self.returncode = 1 if fault == "crash" else 2 if fault == "day_start_stop" else 0
            return self.returncode
    child = Child()
    def spawn(argv, **kw):
        if fault == "day_start_stop":
            kw["stdout"].write((json.dumps(dict(ok=False, refused="day_start_pass_not_clean:owner_stop")) + "\n").encode())
            kw["stdout"].flush()
        return child
    calls = []
    def sleep(seconds):
        ticks[0] += seconds
    def drawable():
        if (fault == "first" and ticks[0] == 1) or (fault == "later" and ticks[0] == 2):
            raise RuntimeError("lost window")
    monkeypatch.setattr(soak.shutil, "disk_usage", lambda path: NS(free=20 * 1024**3))
    # ADR 170: keep the real recorder's safety and stop-receipt methods.
    recorder = soak.JournalRecorder(tmp_path)
    recorder.progress_at = 0
    monkeypatch.setattr(soak, "call", lambda *a: calls.append(a))
    scope = dict(time=NS(monotonic=lambda: ticks[0], time=lambda: ticks[0], sleep=sleep),
        end=1, days=0, env={}, a=NS(days=1, rehearsal_checkout=True), run=tmp_path,
        real_path=True, realpath=NS(check=lambda: None, game_sample=lambda *a: None),
        # ADR 150: _run_soak initializes the opt-in verifier before this lifted loop.
        recorder=recorder, co_tenant=None, verifier=None,
        subprocess=NS(Popen=spawn, STDOUT=subprocess.STDOUT), sys=sys, Path=Path, __file__=soak.__file__, ROOT=soak.ROOT,
        cycle=tmp_path / "cycle", host_out=tmp_path / "host", host_pid=10,
        runtime=tmp_path / "runtime", rig=tmp_path / "rig", prep=tmp_path / "prep",
        HARNESS_LOOP_STALL_S=soak.HARNESS_LOOP_STALL_S, HOST_FREEZE_MACHINE_STALL_MAX_S=soak.HOST_FREEZE_MACHINE_STALL_MAX_S,
        drawable=NS(check=drawable), host_liveness=lambda *a, **k: dict(lost=False),
        host_gaps=NS(observe=lambda *a: None), disk_monitor=NS(sample=lambda: dict(allowed=True)),
        shutil=soak.shutil, write=soak.write, utc=soak.utc, call=lambda *a: calls.append(a))
    code = compile(ast.Module(body=[loop], type_ignores=[]), "actual-soak-loop", "exec")
    if fault in {"first", "later", "crash"}:
        with pytest.raises(RuntimeError, match="another reason|failed"):
            exec(code, scope)
    else:
        exec(code, scope)
    first = soak.read(tmp_path / "stop-reasons-0.json")["reasons"]
    observed = soak.read(tmp_path / "observed-stop-reasons-0.json")["reasons"]
    receipt = soak.read(tmp_path / "rehearsal-budget-stop.json")
    assert first[0] == "rehearsal_budget" and len(calls) == 1
    assert receipt["first_reasons"] == first and receipt["observed_reasons"] == observed
    assert (len(observed) == 2) == (fault in {"first", "later"})
    assert receipt["status"] == ("UNKNOWN" if fault == "day_start_stop" else "FAIL" if fault in {"first", "later", "crash"} else "PASS")
    assert scope['days'] == 0  # a scoped budget stop earns no saved-night credit
    assert not (tmp_path / 'verify-result.json').exists()  # verification stays opt-in


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows process handles required")
def test_parent_watchdog_observes_real_parent_exit_and_ends_helper(tmp_path):
    """Actual WinDLL, real parent exit, actual watchdog/taskkill; only tiny helpers."""
    import ctypes
    from ctypes import wintypes
    alert = tmp_path / "CT-ALERT.json"
    soak.write(tmp_path / "ct-alert-target.json", dict(path=str(alert)))
    helper = ("from scripts.livepath.bridge import start_parent_watchdog; import os,sys,time; from pathlib import Path; "
              "start_parent_watchdog(sys.argv[1],int(sys.argv[2])); "
              "Path(sys.argv[1],'watching').write_text(str(os.getpid())); time.sleep(15)")
    parent_code = ("import subprocess,sys,os; "
                   "p=subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2],str(os.getpid())],stdin=subprocess.DEVNULL); "
                   "sys.stdin.readline()")
    parent = subprocess.Popen([sys.executable, "-B", "-c", parent_code, helper, str(tmp_path)],
        cwd=rehearse.ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle, pid = None, None
    try:
        ready = tmp_path / "watching"
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists(), "watchdog helper did not reach readiness in 5s"
        pid = int(ready.read_text())
        handle = kernel.OpenProcess(0x100000, False, pid)
        assert handle
        parent.stdin.close()
        assert parent.wait(timeout=5) == 0
        assert kernel.WaitForSingleObject(handle, 10000) == 0
        assert (tmp_path / "UNSAFE_NOT_RESTORED").is_file()
        assert soak.read(alert)["state"] == "UNSAFE_NOT_RESTORED"
    finally:
        if parent.poll() is None:
            subprocess.run(["taskkill", "/PID", str(parent.pid), "/T", "/F"], timeout=10, capture_output=True)
            parent.wait(timeout=5)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=10, capture_output=True)
            kernel.CloseHandle(handle)
        parent.stdout.close()


def test_stop_closes_real_pipe_before_helper_python_exit(tmp_path):
    code = ("import sys,threading; t=threading.Thread(target=sys.stdin.readline,daemon=True); "
            "t.start(); print('READY',flush=True); t.join(); print('EOF',flush=True)")
    proc = subprocess.Popen([sys.executable, "-B", "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip() == b"READY"
        rp = rehearse.VoiceBridge(tmp_path, {}, seconds=120)
        rp.proc, rp.console = proc, NS(close=lambda: None)
        (tmp_path / "bridge-drained").touch()
        rp.stop()
        assert proc.returncode == 0 and proc.stdout.read().strip() == b"EOF" and proc.stderr.read() == b""
        assert soak.read(tmp_path / "realpath.json")["forced_stop"] is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def test_real_path_sessions_use_the_live_reply_settings():
    keys = ("codex_effort", "codex_service_tier", "codex_timeout_sec", "codex_thread_max_turns")
    assert {k: soak.REALPATH_BRIDGE_DEFAULTS[k] for k in keys} == {k: rehearse.LIVE_VOICE_PARITY[k] for k in keys}
