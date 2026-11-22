"""Finite ordinary-main-loop M1 observer. Never chooses a gameplay primitive.

The existing production BridgeTrialSession owns budgets, provider and runtime.
The existing external controller/watchdog owns containment and config restoration.
This adapter admits one owner goal only after the joined actor/readiness gate.
"""
import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[1]))
from arti_runtime import ipc
from pathlib import Path
import asyncio
import hashlib
import json
import os
import sys
import threading
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.stardew_trial_controller import write
from scripts.stardew_trial_watchdog import process_identity


def read(path):
    return json.loads(ipc.read_text(path, encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(ipc.read_bytes(path)).hexdigest()


def readiness_deadline(status, expected_operation, absolute):
    if status.get("outcome") != "active" or status.get("operation_id") != expected_operation:
        raise ValueError("readiness_operation_not_active")
    return min(absolute, status["operation"]["deadline_monotonic"] - 3)


class OneOwnerIngress:
    """No mic/hotkey ingress; one exact post-readiness goal through real queue."""
    def __init__(self, ordinary, owner_text, on_rejected):
        self.ordinary, self.owner_text, self.on_rejected = ordinary, owner_text, on_rejected
        self.armed = False
        self.consumed = False

    def __call__(self, *args, **kwargs):
        if self.armed and not self.consumed and args == (self.owner_text,) and kwargs == {"trigger_type": "ptt"}:
            self.consumed = True
            self.armed = False
            return self.ordinary(*args, **kwargs)
        self.on_rejected()
        return False


def main(folder):
    folder = Path(folder).resolve()
    source = read(folder / "source-identities.json")
    namespace = read(folder / "namespace.json")
    approval = read(folder / "operator-approval.json")
    admission = read(folder / "phase2-admission.json")
    assert approval["integrated_head"] == source["source"]
    assert approval["independent_review"] == approval["ci"] == "PASS"
    assert approval["dispatch"] == "1000000001"
    assert admission["run_id"] == namespace["run_id"] == folder.name
    assert admission["phase2_authorized"] is True
    assert str(ROOT) == source["checkout"]
    for name, identity in source["source_files"].items():
        assert sha(ROOT / name) == identity["sha256"], "source_drift"
    assert not any(k.startswith("ARTI_STARDEW_BRIDGE_TRIAL_") for k in os.environ)
    config = read(folder / "bridge-config-trial-private.json")
    summary = read(folder / "config-summary.json")[0]
    assert sha(ROOT / "config_local.json") == summary["trial_sha256"]
    assert not (folder / "journal").exists() and not (folder / "STOP").exists()
    ready = read(folder / "watchdog-ready.json")
    assert ready["run_id"] == namespace["run_id"] and ready["containment_id"] == namespace["containment_id"]
    for name in ("controller", "watchdog"):
        assert process_identity(ready[name]["pid"]) == ready[name]
    registration_end = time.monotonic() + 2
    while True:
        children = read(folder / "owned-children.json")
        if "bridge" in children:
            break
        assert time.monotonic() < registration_end, "bridge_registration_missing"
        time.sleep(.02)
    launcher = process_identity(children["bridge"])
    if os.getpid() == launcher["pid"]:
        assert os.getppid() == ready["controller"]["pid"]
    else:
        assert os.getppid() == launcher["pid"]
        assert Path(launcher["image"]).resolve() == ROOT / "venv/Scripts/python.exe"
    status = read(folder / "operation-status.json")
    end = readiness_deadline(status, namespace["operation_ids"]["bridge01-readiness"], ready["deadline_monotonic"])
    for key in ("run_id", "controller", "watchdog", "containment_id"):
        assert status[key] == ready[key]
    os.chdir(ROOT)
    start = time.monotonic()
    stopped = threading.Event()
    supervision = {"autonomous_started": None}

    def supervise():
        while not stopped.is_set():
            write(folder, "supervisor-heartbeat.json", dict(run_id=folder.name, monotonic=time.monotonic(), **supervision))
            stopped.wait(.2)

    threading.Thread(target=supervise, daemon=True).start()
    result = dict(run_id=folder.name, result="BLOCKED", reason="STARTUP_NOT_READY", owner_ingress=0,
        ingress_attempts=0, provider_requests=0, physical_inputs=0, continuation_decisions=0,
        autonomous_seconds=0, manual_pulses=0, manual_rescue=0, runtime_import="NOT_REACHED")
    bridge = None
    runtime = None
    try:
        from scripts.stardew_m1_supervisor import prepare, gameplay, OWNER
        prepare(folder, config, source, namespace, sha)
        import arti_stardew_runtime as runtime_module
        import arti_bridge as bridge
        from arti_stardew_command import KesalahanPerintah, baca_sesi
        assert Path(runtime_module.__file__).resolve() == ROOT / "arti_stardew_runtime.py"
        assert Path(bridge.__file__).resolve() == ROOT / "arti_bridge.py"
        assert all(bridge.CONFIG.get(k) == v for k, v in config.items()), "effective_config_drift"
        assert config.get("tts_engine") != "supertone" or config.get("supertonic_prewarm_on_startup") is False

        async def no_vts_connect(self):
            # VTS connectivity NOT CERTIFIED by this proof. Object remains real.
            return None

        bridge.VTSController.connect = no_vts_connect
        for name in ("voice_listener_worker", "init_global_hotkey", "init_vision_hotkey"):
            setattr(bridge, name, lambda *a, **kw: None)
        bridge.hotkey_active = bridge.hotkey_registered = bridge.vision_hotkey_registered = False

        def rejected():
            result["ingress_attempts"] += 1
            result["reason"] = "FORBIDDEN_INGRESS_ATTEMPT"
            (folder / "STOP").touch()

        ingress = OneOwnerIngress(bridge.queue_voice_trigger, OWNER, rejected)
        bridge.queue_voice_trigger = ingress
        result["runtime_import"] = "OBSERVED"
        write(folder, "actual-process-source.json", dict(run_id=folder.name, process=process_identity(),
            bridge_launcher=launcher, parent_pid=os.getppid(), executable=sys.executable,
            source=source["source"], tree=source["tree"], runtime_file=str(Path(runtime_module.__file__)),
            runtime_sha256=sha(Path(runtime_module.__file__)), bridge_sha256=sha(Path(bridge.__file__)),
            observer_sha256=sha(Path(__file__)), ordinary_main_loop=True, production_bridge_trial=True))
        prior = read(folder / "excluded-native-identities.json")

        async def run():
            nonlocal runtime
            task = asyncio.create_task(bridge.main_loop())
            pin = first_seq = identities = None
            try:
                while time.monotonic() < end:
                    assert not (folder / "STOP").exists(), "STOP"
                    if task.done():
                        task.result()
                        raise ValueError("bridge_exited_before_readiness")
                    current = bridge._stardew_runtime
                    if current is None:
                        await asyncio.sleep(.1)
                        continue
                    if runtime is None:
                        runtime = current
                        identities = (id(current), id(current._receiver), id(current.state_store), id(current.observation_store))
                    assert runtime is current
                    assert identities == (id(current), id(current._receiver), id(current.state_store), id(current.observation_store))
                    provider = current.decision_provider
                    assert provider is not None and provider.physical_requests == 0 and not provider._clients
                    assert provider._logical_timeout_sec == runtime_module._STARDEW_DECISION_LOGICAL_TIMEOUT_SEC, "PROVIDER_TIMEOUT_CHANGED"
                    assert current.physical_action_publications == current.inbox_ops == 0
                    assert current.active_goal is None and current.queued_count == 0 and current._current_generation == 0
                    assert not current._owner_voice_busy and current._owner_voice_epoch == 0
                    assert result["ingress_attempts"] == 0 and not bridge.hotkey_active
                    assert current.status == "READY", "RUNTIME_" + current.status
                    trial = bridge._stardew_bridge_trial
                    assert trial is not None and trial.runtime is current and not trial.stop_requested
                    actors = current.state_store.semua_identitas()
                    assert len(actors) <= 1, "AMBIGUOUS_ACTOR"
                    if not actors or not current.listener_started:
                        await asyncio.sleep(.1)
                        continue
                    state = current.state_store.latest(actors[0].player_id)
                    try:
                        live = baca_sesi(config["stardew_command_dir"])
                    except KesalahanPerintah:
                        live = None
                    if state is None or live is None:
                        await asyncio.sleep(.1)
                        continue
                    assert live.session_id not in prior["session_ids"]
                    assert live.mentah.get("runtime_instance_id") and live.mentah["runtime_instance_id"] not in prior["runtime_ids"]
                    assert live.world_ready and not live.is_main_player and not live.is_split_screen
                    assert not live.input_busy and not live.inflight_request_id and not live.mentah.get("record_faulted")
                    assert (state.player_id, state.screen_id) == (live.player_id, live.screen_id)
                    assert not state.active_menu and state.time_of_day < 1800
                    rich = [current.observation_store.get(state.player_id, domain) for domain in ("scene.current_location", "progress.inventory", "ui.context")]
                    if time.monotonic() - state.diterima_pada > 1 or not all(r and r.available and r.authoritative and not r.stale and not r.requires_resync and r.player_id == state.player_id and r.screen_id == state.screen_id and time.monotonic() - r.received_monotonic < 10 for r in rich):
                        await asyncio.sleep(.1)
                        continue
                    current_pin = (live.session_id, live.player_id, live.screen_id, state.location, tuple((r.domain, r.session_id) for r in rich))
                    if pin is None:
                        pin, first_seq = current_pin, state.seq
                    else:
                        assert pin == current_pin, "READINESS_BINDING_CHANGED"
                        if state.seq > first_seq:
                            result.update(result="READINESS_OBSERVED", reason="STARTUP_RUNTIME_READY", runtime_count=1,
                                runtime_objects=identities, status=current.status, listener_started=True,
                                native_session=live.session_id, native_runtime_instance=live.mentah["runtime_instance_id"],
                                player=state.player_id, screen=state.screen_id, location=state.location,
                                sequences=[first_seq,state.seq], fresh_motor=True, fresh_rich=True,
                                stable_binding=True, elapsed_seconds=time.monotonic()-start)
                            await gameplay(folder,bridge,current,config,result,supervision,ready,task,ingress)
                            return
                    await asyncio.sleep(.1)
                raise ValueError("STARTUP_READINESS_TIMEOUT")
            finally:
                task.cancel()
                bridge._stop_stardew_runtime()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        asyncio.run(run())
    except BaseException as exc:
        result.update(result="BLOCKED", reason=str(exc) if isinstance(exc,(ValueError,AssertionError)) else type(exc).__name__)
    finally:
        write(folder,"operator-result.json",result)
        write(folder,"terminal-stop.json",dict(run_id=folder.name,reason=result["reason"],monotonic=time.monotonic()))
        (folder / "STOP").touch()
        # External watchdog owns termination, absence proof and restoration.
        time.sleep(10)
        stopped.set()


if __name__ == "__main__":
    main(sys.argv[1])
