"""Prepare or run a booked voice rehearsal. Never launch from --dry-run."""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.free_play import soak
from scripts.livepath.bridge import load_config, owner_hash
from scripts.livepath.disk import DiskRecorder
from scripts.livepath.cache import validate_cache

HOLD = Path(os.environ.get("ARTI_HOUSEKEEPING_HOLD") or ROOT.parent / "HOUSEKEEPING_HOLD")
OWNER_LINES = (
    (30, "Arti, sekarang kamu lagi ngapain di Stardew?"),
    (300, "Arti, rencana kamu buat hari ini apa?"),
    (900, "Arti, tadi ada kejadian menarik apa?"),
    (1500, "Arti, kamu sekarang lagi di mana?"),
    (2100, "Arti, habis ini kamu mau ngapain?"),
    (2520, "Arti, gimana kabar kebunmu sekarang?"),
)
FORBIDDEN = {"stream-folder", "live-checkout"}
UNSAFE_EXIT = 3


def scratch_path(run, env, *, root=ROOT):
    """A rig Codex lane must have no repository, authority or link ancestor."""
    base = Path(env["LOCALAPPDATA"]) / "ARTI/livepath-scratch" / Path(run).name
    resolved = safe_path(base)
    if resolved == root.resolve() or root.resolve() in resolved.parents:
        raise RuntimeError("codex_scratch_inside_repo")
    for part in (base.absolute(), *base.absolute().parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("codex_scratch_link")
        if any((part / name).exists() for name in (".git", "AGENTS.md", "AGENTS.override.md")):
            raise RuntimeError("codex_scratch_inside_project")
    return resolved


def login_preflight(env):
    # Existence only: no credential contents enter the harness.
    for path in (Path(env["USERPROFILE"]) / ".codex/auth.json",
                 Path(env["LOCALAPPDATA"]) / "ARTI/codex-stardew/auth.json"):
        if not path.is_file():
            raise RuntimeError("missing Codex login: " + str(path.parent))


def unsafe_state(run, reason):
    print("!!! UNSAFE_NOT_RESTORED: " + reason + " — CT intervention required; do not reuse completed.json !!!", file=sys.stderr)
    try:
        if run.is_dir():
            (run / "UNSAFE_NOT_RESTORED").write_text(
                reason + "\nCT MUST reconcile owned games/day runner, rehearsal DLL/config, saves and free-play lock.\n",
                encoding="utf-8")
        from scripts.livepath.safety import alert
        alert(run, reason)
    except Exception:
        print("!!! UNSAFE_NOT_RESTORED: receipt unavailable; CT must inspect the retained lock !!!", file=sys.stderr)


def safe_path(path):
    resolved = Path(path).resolve()
    if FORBIDDEN.intersection(p.lower() for p in resolved.parts):
        raise ValueError("protected stream/live checkout")
    return resolved


def read_booking(path):
    path = safe_path(path)
    if path.name.lower() in {"config_local.json", "arti_soul.md", ".env"}:
        raise ValueError("private config is not a booking")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    def validate(value):
        if isinstance(value, dict):
            for k, v in value.items():
                key = k.lower()
                if key in {"token", "password", "secret", "api_key", "credentials"} or key.endswith(
                        ("_api_key", "_api_keys", "_access_token", "_refresh_token", "_auth_token", "_password", "_secret")):
                    raise ValueError("booking must contain no credentials")
                validate(v)
        elif isinstance(value, list):
            for v in value:
                validate(v)
        elif isinstance(value, str) and FORBIDDEN.intersection(value.lower().replace("\\", "/").split("/")):
            raise ValueError("booking references a protected stream/live checkout")
    validate(data)
    return data


def process_names():
    raw = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, timeout=20)
    return {row[0].lower() for row in csv.reader(io.StringIO(raw)) if row}


_admitted_hold = None  # HOLD text seen at admission; None when no HOLD existed


def read_hold():
    return HOLD.read_text(encoding="utf-8", errors="replace") if HOLD.exists() else None


def idle_guard():
    global _admitted_hold
    names = process_names()
    if "stardewmoddingapi.exe" in names:
        raise RuntimeError("SMAPI present; no launch")
    # CT books the rig for a rehearsal by writing the HOLD itself, naming it. Any other HOLD refuses.
    hold = read_hold()
    if hold is not None:
        booked = hold[:2000].lower()
        if not ("rehearsal" in booked and "bridge lead" in booked):
            raise RuntimeError("HOUSEKEEPING_HOLD present and not booked for this rehearsal; no launch")
    _admitted_hold = hold
    if {"obs64.exe", "obs32.exe"} & names:
        raise RuntimeError("OBS must already be closed; harness never closes it")


def process_witness(pid):
    """Return (alive, Windows creation time); inaccessible evidence stays unknown."""
    import ctypes
    from ctypes import wintypes
    if not pid:
        return None, None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return (False if ctypes.get_last_error() == 87 else None), None
    try:
        code = wintypes.DWORD()
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or not kernel.GetProcessTimes(
                handle, *(ctypes.byref(t) for t in times)):
            return None, None
        return code.value == 259, (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def preflight(code_sha, min_free_gib, output, *, runs_root, root=ROOT):
    for p in (Path.cwd(), __file__, root, output):
        safe_path(p)
    if os.environ.get("ARTI_REHEARSE_CHECKOUT") and root.name.lower() != os.environ["ARTI_REHEARSE_CHECKOUT"].lower():
        raise RuntimeError("this checkout is not the one ARTI_REHEARSE_CHECKOUT names")
    if os.name != "nt":
        raise RuntimeError("booked Windows rig only")
    idle_guard()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=root, text=True).strip()
    if len(code_sha) != 40 or head != code_sha or branch != os.environ.get("ARTI_REHEARSE_BRANCH", branch):
        raise RuntimeError("the checkout must be at the full pinned --code-sha (and on ARTI_REHEARSE_BRANCH when set)")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root):
        raise RuntimeError("checkout must be clean")
    for env in ("venv", "venv312"):
        if not (root / env / "Scripts/python.exe").is_file():
            raise RuntimeError("missing checkout interpreter: " + env)
    if not (root / "ARTI_SOUL.md").is_file():
        raise RuntimeError("Bridge lead must provision ARTI_SOUL.md here; harness checks existence only")
    scratch_path(output, os.environ, root=root)
    login_preflight(os.environ)
    validate_cache()
    print("HUMAN CHECK REQUIRED: Windows Listen to this device OFF for CABLE Input and CABLE Output; app monitoring OFF.")
    if shutil.disk_usage("C:\\").free < min_free_gib * 1024**3:
        raise RuntimeError("C: below --min-free-gib")
    # The rig owner-idle guard only exempts launch records under its own evidence folder.
    runs_root = Path(runs_root).resolve()
    if output.exists() or output.parent != runs_root or not output.name.startswith("livepath-"):
        raise RuntimeError("output must be new: <rig root>/rig/free-play-runs/livepath-<name>")


# Example voice settings for a rehearsal (not anyone's live config). Private
# config is never loaded here. Without them the rehearsal would run the FIFO queue with no idle talk, and a pass
# would say nothing about the stream. Non-secret values only.
LIVE_VOICE_PARITY = dict(
    voice_queue_enabled=True, initiative_enabled=True, initiative_backoff_base_sec=0,
    stardew_initiative_material_enabled=True, stardew_initiative_gap_sec=240, arti_reply_inisiatif_max_sentences=1,
    codex_effort="high", codex_service_tier="priority", codex_timeout_sec=12.0, codex_thread_max_turns=8,
    speech_censor_enabled=True, tts_jeda_antar_kalimat_sec=0.0,
)


def bridge_config(run, runtime):
    cfg = soak.realpath_bridge_config(run, run / "mind", runtime)
    cfg.update(LIVE_VOICE_PARITY)
    cfg.update(
        bridge_test_mode=False, active_profile="smoke", asr_enabled=False, vts_enabled=False,
        youtube_chat_enabled=False, ssn_chat_enabled=False, donation_enabled=False, vision_enabled=False,
        screen_context_enabled=False, desktop_audio_enabled=False, obs_scene_switch_enabled=False,
        text_input_enabled=True, tts_engine="supertone", supertonic_use_cuda=True,
        virtual_cable_name="CABLE Input", subtitle_enabled=False, health_check_on_startup=True,
        cursor_agent_enabled=False, agy_agent_enabled=False, provider_probe_enabled=False,
        scouter_enabled=False, observer_enabled=False, observer_catchup_on_startup=False,
        observer_rag_live_enabled=False, vault_summary_external_enabled=False,
        vault_rag_enabled=False, vault_rag_live_enabled=False, vault_rag_reindex_on_startup=False,
        vault_rag_reindex_on_shutdown=False, vault_rag_db_path=str(run / "vault_rag.db"),
        observer_db_path=str(run / "observer_rag.db"), arti_state_dir=str(run / "arti-state"),
        transcript_dir=str(run / "transcripts"), telemetry_dir=str(run / "telemetry"),
        stream_session_id=run.name + "-smoke", session_log_keep_n=0,
        live7_activity_events_enabled=False, minecraft_enabled=False, stardew_runtime_enabled=False,
        stardew_provider_enabled=False, stardew_spend_enabled=False, reflex_enabled=False,
        video_enabled=False, vision_provider_chain=[], scouter_provider_chain=[], observer_provider_chain=[],
        expression_nod_enabled=False,
        rehearsal_parent_pid=os.getpid(),
    )
    # Test-mode stop/parent fields are irrelevant to the real loop; never advertise them.
    cfg = {k: v for k, v in cfg.items() if not k.startswith("bridge_test_")}
    cfg["bridge_test_mode"] = False
    return cfg


def child_env(env, run):
    # Allow only OS/runtime and established rig transport variables. No provider keys inherited.
    # SYSTEMDRIVE: Windows expands %SystemDrive% for the ProgramData known folder that Codex resolves.
    allowed = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "USERPROFILE", "HOMEDRIVE",
               "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROGRAMDATA", "COMPUTERNAME", "USERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    result = {k: v for k, v in env.items() if k.upper() in allowed or
              k.startswith(("ARTI_M1_", "ARTI_HOST_", "ARTI_FARMHAND_", "ARTI_STARDEW_")) or
              k in {"ARTI_CHECKOUT", "ARTI_VENV_PYTHON", "ARTI_MIND_DIR", "PYTHONPATH"}}
    scratch = scratch_path(run, env)
    result.update(ARTI_REHEARSAL_CONFIG=str(run / "rehearsal-config.json"),
                  PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                  TEMP=str(scratch), TMP=str(scratch))
    return result


class VoiceBridge(soak.RealPathBridge):
    """Use the existing bridge lifecycle seam, real stdin, and Ctrl+Break shutdown."""
    def __init__(self, run, env, *, seconds, min_free_gib=15, disk_recorder=None, **kwargs):
        super().__init__(run, env, **kwargs)
        self.seconds = seconds
        self.min_free_gib = min_free_gib
        self.sent = 0
        self.first_game_at = None
        self.game_identities = {}
        self.disk_recorder = disk_recorder
        self.game_halt = threading.Event()
        self.game_monitor = None
        self.game_error = None
        self.lines = [(t, line) for t, line in OWNER_LINES if t < seconds - 15]

    def spawn(self, ignored):
        self._check_hold_obs()  # HOST is already ours; soak's idle/admission guards preceded its launch.
        cfg = bridge_config(self.run, self.run / "runtime")
        path = self.run / "rehearsal-config.json"
        soak.write(path, cfg)
        load_config(path)
        scratch_path(self.run, self.env).mkdir(parents=True, exist_ok=True)
        self.dir.mkdir(exist_ok=True)
        self.console = (self.dir / "console.log").open("xb")
        self._record(spawn_attempted=True, stopped=False, reason="spawn_pending")
        self.proc = self._popen([self.python, "-B", "-u", str(ROOT / "arti_bridge.py"), "--no-wizard", "--no-save-bridge"],
                                cwd=ROOT, env=child_env(self.env, self.run), stdin=subprocess.PIPE,
                                stdout=self.console, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self._spawned_at = self._clock()
        self._record(reason="starting", pid=self.proc.pid)
        return True

    def _check_hold_obs(self):
        # The admitted booking HOLD may stay; a new or changed HOLD, or OBS, stops startup.
        if read_hold() != _admitted_hold or {"obs64.exe", "obs32.exe"} & process_names():
            raise RuntimeError("hold/OBS appeared during startup")

    def logs(self):
        from scripts.livepath.check import log_text
        return "\n".join(log_text(p) for p in sorted((self.run / "session_logs").glob("*_bridge.log")))

    def wait_ready(self):
        deadline = self._spawned_at + 240
        while self.proc.poll() is None and self._clock() < deadline:
            if self.disk_recorder and self.disk_recorder.floor_crossed:
                raise RuntimeError("C: free-space floor crossed during startup")
            text = self.logs()
            self._check_supertone(text)
            if "SISTEM SIAP!" in text:
                self._record(real_path=True, reason="real_loop_ready")
                return True
            self._sleep(.5)
        raise RuntimeError("real bridge did not become ready")

    @staticmethod
    def _check_supertone(text):
        from scripts.livepath.check import supertone_fallbacks
        if supertone_fallbacks(text):
            raise RuntimeError("Supertone unavailable/fallback observed; rehearsal aborted")

    def check(self):
        self._check_hold_obs()
        if self.proc.poll() is not None:
            raise RuntimeError("real bridge exited during rehearsal")
        self._check_supertone(self.logs())
        if self.game_error:
            raise RuntimeError(self.game_error)
        free = shutil.disk_usage("C:\\").free
        if free < self.min_free_gib * 1024**3:
            if self.disk_recorder:
                self.disk_recorder.sample("floor_crossing", free_bytes=free)
            raise RuntimeError("C: rehearsal free-space floor reached")
        if self.disk_recorder and self.disk_recorder.floor_crossed:
            raise RuntimeError("C: free-space floor was crossed during rehearsal")
        if self.first_game_at is None:
            self.first_game_at = self._clock()
        elapsed = self._clock() - self.first_game_at
        if self.sent < len(self.lines) and elapsed >= self.lines[self.sent][0]:
            offset, line = self.lines[self.sent]
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            with (self.run / "owner-lines.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(dict(index=self.sent, offset_s=offset, sent_s=elapsed,
                                       owner_sha256=owner_hash(line))) + "\n")
            self.sent += 1

    def game_sample(self, runtime, host_out):
        if self.game_monitor is not None:
            if self.game_error:
                raise RuntimeError(self.game_error)
            return
        self._sample_game(runtime, host_out)
        def monitor():
            while not self.game_halt.wait(1):
                try:
                    self._sample_game(runtime, host_out)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.game_error = "game witness lost: " + type(exc).__name__
                    return
        self.game_monitor = threading.Thread(target=monitor, name="livepath-game-witness", daemon=True)
        self.game_monitor.start()

    def _sample_game(self, runtime, host_out):
        pids = []
        for name in ("batch-host/host-launch.json", "farmhand-launch.json"):
            p = runtime / name
            pids.append(soak.read(p).get("pid") if p.is_file() else None)
        # Exact launch PIDs only; also require fresh HOST telemetry, not PID presence alone.
        live = soak.host_liveness(host_out, time.time(), pids[0])
        witnesses = [process_witness(pid) for pid in pids]
        states = []
        for pid, (alive, created) in zip(pids, witnesses):
            if created is not None:
                previous = self.game_identities.setdefault(pid, created)
                if previous != created:
                    alive = False
            states.append(alive)
        # Rejoin legitimately replaces the farmhand. Never credit its disconnected
        # interval as continuity, but let the existing runner perform recovery.
        identity_changed = any(created is not None and self.game_identities.get(pid) != created
                               for pid, (_, created) in zip(pids, witnesses))
        row = dict(elapsed_s=self._clock() - self.first_game_at, pids=pids,
                   alive=False if states[0] is False or identity_changed else None if states != [True, True] else True,
                   host_lost=live["lost"],
                   reason="farmhand_rejoin_or_process_unknown" if states[1] is not True else "sampled")
        with (self.run / "game-liveness.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        if row["alive"] is False or row["host_lost"]:
            raise RuntimeError("owned game process/telemetry lost")

    def stop(self):
        if self.proc is not None and self.proc.stdin is not None:
            self.proc.stdin.close()  # EOF releases text_input_worker's buffered-reader lock before Python exit.
        self.game_halt.set()
        if self.game_monitor is not None:
            self.game_monitor.join(timeout=2)
        if self.proc is None:
            if getattr(self, "console", None) is not None:
                self.console.close()
            self._record(stopped=True, spawned=False, reason="never_spawned")
            return
        forced = False
        drained = self.run / "bridge-drained"
        if self.proc.poll() is None:
            # The bridge drains its actual queues and observes three idle seconds.
            # No Ctrl+Break based on buffered start/end log snapshots.
            (self.run / "bridge-stop-request").touch()
            deadline = self._clock() + 90
            while self.proc.poll() is None and not drained.is_file() and self._clock() < deadline:
                self._sleep(.5)
        if self.proc.poll() is None:
            if not drained.is_file():
                forced = True  # drain timeout is never a clean stop, even if Break works
                try:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                except (OSError, ValueError):
                    self._kill_tree(self.proc)
            try:
                self.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                forced = True
                # Only the Popen-owned, still-live bridge tree; never image-name taskkill.
                self._kill_tree(self.proc)
                self.proc.wait(timeout=15)
        self.console.close()
        self._record(stopped=True, forced_stop=forced, rc=self.proc.returncode)


def cable_monitoring(booking):
    """How the voice may be heard locally: an attestation it isn't, or the owner's dated yes that it may be.

    Exactly one of the two. A live routing may keep local monitoring on, so "disabled" can be untrue, and a
    booking must never have to claim it just to run. The harness never changes routing either way."""
    disabled = booking.get("cableMonitoringDisabled") is True
    accepted = booking.get("cableMonitoringAcceptedByOwner")
    if accepted is not None and not (isinstance(accepted, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2} \S.*", accepted)):
        raise ValueError("cableMonitoringAcceptedByOwner must be 'YYYY-MM-DD <what the owner said>'")
    if disabled and accepted:
        raise ValueError("booking says both that monitoring is off and that the owner accepts it on")
    if accepted:
        print(f"AUDIBLE: voice may play on this machine's output for the whole run; owner accepted: {accepted}")
        return "audible_owner_accepted"
    if not disabled:
        raise ValueError("CT must attest CABLE Output Windows Listen/monitoring is disabled, "
                         "or record the owner's acceptance, before AFK use")
    return "disabled_attested"


def plan(args, booking):
    run = safe_path(args.output)
    if not math.isfinite(args.minutes) or args.minutes <= 1 or not math.isfinite(args.min_free_gib) or args.min_free_gib < 15:
        raise ValueError("minutes must exceed 1; minimum free space must be >=15 GiB")
    paths = {name: safe_path(booking[name]) for name in
             ("dll", "helper", "template", "hostTemplate", "trialConfig", "provenance", "rigConfig")}
    trial = read_booking(paths["trialConfig"])
    trial.update(active_profile="smoke", arti_mind_dir=str(run / "mind"), arti_state_dir=str(run / "arti-state"),
                 live7_moments_enabled=True, live7_activity_events_enabled=False)
    if "codex" not in str(trial.get("stardew_decision_provider_kind", "")).lower():
        raise ValueError("trial must use the booked Codex provider")
    rig = read_booking(paths["rigConfig"])
    if not rig.get("ownerIdleScript"):
        raise ValueError("rig must supply its established owner-idle guard")
    cable_route = cable_monitoring(booking)
    for name in ("root", "game", "appData", "ownerIdleScript"):
        safe_path(rig[name])
    # The installed mod's non-secret inbox is part of the free-play restore surface.
    mod_config = read_booking(Path(rig["game"]) / "Mods/ArtiStardewBridge/config.json")
    safe_path(mod_config["CommandInboxDirectory"])
    return dict(code_sha=args.code_sha, minutes=args.minutes, min_free_gib=args.min_free_gib, cable_route=cable_route,
                output=str(run), runs_root=str(Path(rig["root"]) / "rig/free-play-runs"),
                farm=booking.get("farm", "Standard"),
                bridge_config=bridge_config(run, run / "runtime"), trial_config=trial,
                expected_owner_hashes=[owner_hash(line) for t, line in OWNER_LINES if t < args.minutes * 60 - 15],
                paths={k: str(v) for k, v in paths.items()},
                dll_sha256=booking["dllSha256"], helper_sha256=booking["helperSha256"])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--spec", type=Path, required=True, help="CT-reviewed non-secret rig/artifact booking")
    p.add_argument("--code-sha", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--minutes", type=float, default=45)
    p.add_argument("--min-free-gib", type=float, default=15)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    admitted = False
    try:
        for path in (Path.cwd(), __file__, a.output, a.spec):
            safe_path(path)
        booking = read_booking(a.spec)
        prepared = plan(a, booking)
        if a.dry_run:
            print(json.dumps(dict(dry_run=True, admission="NOT_CHECKED; repeated before launch", **prepared), indent=2))
            return 0
        preflight(a.code_sha, a.min_free_gib, a.output.resolve(), runs_root=prepared.get("runs_root"))
        admitted = True
        # soak owns the output directory creation, baseline, scoped game close and restore.
        # Put prepared inputs in a sibling so soak can require a new output directory.
        inputs = a.output.resolve().with_name(a.output.name + "-inputs")
        inputs.mkdir(parents=True, exist_ok=False)
        soak.write(inputs / "trial.json", prepared["trial_config"])
        soak.write(inputs / "request.json", prepared)
        original_env = dict(os.environ)
        sanitized = child_env(original_env, a.output.resolve())
        sanitized = {k: v for k, v in sanitized.items() if not k.startswith("ARTI_")}
        scratch = scratch_path(a.output.resolve(), original_env)
        scratch.mkdir(parents=True, exist_ok=False)
        sanitized.update(TEMP=str(scratch), TMP=str(scratch), PYTHONPATH=str(ROOT))
        os.environ.clear()
        os.environ.update(sanitized)
        os.environ["ARTI_FREE_PLAY_RIG_CONFIG"] = prepared["paths"]["rigConfig"]
        # Config precedence is shared by all game-runner children.
        os.environ["ARTI_MIND_DIR"] = str(a.output.resolve() / "mind")
        disk = DiskRecorder(inputs / "disk-space.jsonl", a.min_free_gib)
        controllers = []
        def bridge_factory(run, env):
            controller = VoiceBridge(run, env, seconds=a.minutes * 60,
                min_free_gib=a.min_free_gib, disk_recorder=disk,
                python=str(ROOT / "venv/Scripts/python.exe"))
            controllers.append(controller)
            return controller
        args = SimpleNamespace(
            hours=a.minutes / 60, days=1, farm=prepared["farm"], output=a.output.resolve(),
            code_sha=a.code_sha, dll=Path(prepared["paths"]["dll"]), dll_sha256=prepared["dll_sha256"],
            helper=Path(prepared["paths"]["helper"]), helper_sha256=prepared["helper_sha256"],
            template=Path(prepared["paths"]["template"]), host_template=Path(prepared["paths"]["hostTemplate"]),
            trial_config=inputs / "trial.json", provenance=Path(prepared["paths"]["provenance"]),
            host_delay=0, staging_script=None, staging_target="farmhand", operator_rearm=False,
            enroll_only=False, real_path=True, realpath_bridge_config=None,
            rehearsal_checkout=ROOT, rehearsal_trial_defaults=dict(live7_moments_enabled=True, live7_activity_events_enabled=False),
            rehearsal_bridge_factory=bridge_factory,
        )
        try:
            disk.start()
            soak.run_soak(args)
            if (a.output / "UNSAFE_NOT_RESTORED").exists():
                raise RuntimeError("cleanup did not establish safe restoration")
        finally:
            try:
                # soak deliberately preserves an unsafe game/save barrier. Even if
                # that cleanup raises early, our separately owned voice child must stop.
                for controller in controllers:
                    if not controller.state.get("stopped"):
                        controller.stop()
            finally:
                try:
                    disk.stop()
                finally:
                    os.environ.clear()
                    os.environ.update(original_env)
                    if a.output.is_dir():
                        soak.write(a.output / "rehearsal-request.json", prepared)
                        if (inputs / "disk-space.jsonl").is_file():
                            shutil.copy2(inputs / "disk-space.jsonl", a.output / "disk-space.jsonl")
        return 0
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        if admitted and a.output.is_dir():
            from scripts.livepath.safety import cleanup_receipt
            print("REFUSED/FAILED: " + type(exc).__name__, file=sys.stderr)
            try:
                soak.write(a.output / "failure.json", dict(type=type(exc).__name__, utc=soak.utc()))
                safe = cleanup_receipt(a.output)
            except Exception:
                # A receipt write/read failure must still produce a loud result.
                safe = False
            if not safe:
                unsafe_state(a.output, type(exc).__name__)
                return UNSAFE_EXIT
        print("REFUSED/FAILED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
