"""One day using the existing immutable Live7 approval, with a fresh native morning gate."""
import argparse
import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/m1_operator"))
sys.path.insert(0, str(ROOT / "scripts"))
import live_cycle
import poll_state

class ClockGated(live_cycle.Backend):
    def approve(self, cycle, state, attempt):
        deadline = time.monotonic() + 20
        samples = []
        pose = None
        while True:
            diagnostics = {}
            pose = poll_state.sample(diagnostics=diagnostics)
            samples.append(dict(pose=pose, diagnostics=diagnostics))
            ready = (pose and type(pose.get("time_of_day")) is int and pose["time_of_day"] >= 600
                     and pose.get("is_player_free") is True and pose.get("active_menu") is None)
            if ready or time.monotonic() >= deadline:
                break
            time.sleep(.5)
        # ADR 148: native 06:00 is a normal morning. A missing TCP sample is
        # unavailable evidence, not proof of a bad clock/menu/control state.
        clock = dict(at=time.time(), pose=pose, samples=samples)
        late = bool(pose) and pose.get("time_of_day", 0) > 1200
        if late:
            # Game time is not a hard stop: a late start is a named harness fault and the day continues.
            clock["harness_fault"] = dict(id="H-1_day_start_after_1200", time_of_day=pose.get("time_of_day"), action="continue")
        ipc.publish(cycle / ("clock-" + attempt["id"] + ".json"), json.dumps(clock), critical=False)
        if not ready:
            raise live_cycle.Refused("free-play morning gate: require native >=06:00, free, menu null; sample unavailable or not ready after bounded wait")
        return super().approve(cycle, state, attempt)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cycle-dir", type=Path, required=True)
    p.add_argument("--host-output", type=Path, required=True)
    a = p.parse_args()
    sys.exit(live_cycle.main(["start-day", "--cycle-dir", str(a.cycle_dir), "--once"], backend=ClockGated()))
