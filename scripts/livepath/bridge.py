"""Rehearsal-only config validation and content-free bridge witnesses.

No runtime imports, subprocesses, audio or model loading at import time.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time


# Mabar (owner plays HOST and talks to her): the owner's mic and her VTS avatar are part of the session.
MABAR_PERIPHERALS = frozenset({
    "asr_enabled", "vts_enabled", "youtube_chat_enabled", "ssn_chat_enabled", "donation_enabled", "vision_enabled",
})


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("active_profile") != "smoke" or cfg.get("bridge_test_mode") is not False:
        raise ValueError("rehearsal requires smoke profile and real main loop")
    mabar = cfg.get("livepath_mode") == "mabar"
    if cfg.get("livepath_mode") not in (None, "mabar"):
        raise ValueError("unknown livepath_mode")
    for name in ("asr_enabled", "vts_enabled", "youtube_chat_enabled", "ssn_chat_enabled", "donation_enabled",
                 "vision_enabled", "desktop_audio_enabled", "obs_scene_switch_enabled"):
        if mabar and name in MABAR_PERIPHERALS:
            if type(cfg.get(name)) is not bool:
                raise ValueError("mabar peripheral must be an explicit bool: " + name)
            continue
        if cfg.get(name) is not False:
            raise ValueError("rehearsal peripheral must be off: " + name)
    if mabar and cfg.get("youtube_chat_enabled") and not str(cfg.get("youtube_video_id") or "").strip():
        raise ValueError("mabar youtube chat needs the live video id")
    if cfg.get("ssn_chat_enabled") and cfg.get("youtube_chat_enabled"):
        raise ValueError("chat: SSN already carries YouTube; use SSN or the video id, not both")
    if cfg.get("ssn_chat_enabled") and cfg.get("ssn_session_source") not in ("app", "file"):
        raise ValueError("SSN chat needs ssn_session_source app or file")
    if mabar:
        if cfg.get("vision_runtime_on_start") or cfg.get("vision_background_poll"):
            raise ValueError("mabar vision starts off and captures on demand")
        if cfg.get("codex_web_search") != "disabled" or cfg.get("host_web_topic_enabled"):
            raise ValueError("mabar search is restricted to the isolated Codex lookup role")
        if cfg.get("web_lookup_enabled") or cfg.get("web_lookup_codex_enabled"):
            if (cfg.get("web_lookup_provider_chain") != ["codex"]
                    or cfg.get("web_lookup_regex_enabled") is not False
                    or any(cfg.get(key) is not True for key in (
                        "web_lookup_enabled", "web_lookup_codex_enabled", "web_lookup_model_decides",
                        "web_lookup_announce", "codex_agent_enabled"))):
                raise ValueError("mabar lookup requires model choice, a spoken notice, and Codex only")
    if cfg.get("tts_engine") != "supertone" or cfg.get("supertonic_use_cuda") is not True:
        raise ValueError("rehearsal requires GPU Supertone")
    return cfg


def owner_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rehearsal_health(config, *, tts=None, **kwargs):
    """Health rows for disabled peripherals without ever opening/querying a mic.

    The ordinary checker queries a default mic even with interactive testing off;
    on a machine with no inputs that query can raise. Only cable playback is required.
    Mabar uses the mic and VTS, so it gets the ordinary checker (it logs; it never refuses).
    """
    if config.get("livepath_mode") == "mabar":
        import bridge_health
        return bridge_health.run_startup_health_check(config, tts=tts, **kwargs)
    from bridge_health import HealthReport
    report = HealthReport()
    report.add("Mic devices", "SKIP", "Rehearsal: capture and ASR disabled")
    report.add("VTS API", "FAIL", "Rehearsal: VTS deliberately disabled")
    report.add("PTT hotkey", "FAIL", "Rehearsal: hotkeys deliberately disabled; real owner stdin enabled")
    report.add("TTS output", "OK" if getattr(tts, "device_id", None) is not None else "FAIL",
               "Required virtual cable")
    return report


def supertone_env(env):
    """Reuse the prepared user cache; disable HuggingFace network access as well."""
    return dict(env, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")


def emit(row):
    # The tee locks each write, so include the newline in the same write.
    sys.stdout.write("[Livepath] " + json.dumps(row, sort_keys=True) + "\n")
    sys.stdout.flush()


OWNER_KINDS = frozenset({"mic", "mic_pasif"})
_reactions = dict(last_reply=None, last_reply_utc=None, pending={})


def _game_now():
    """Native game date/time from the shared mind `now` row, or None (never raises)."""
    try:
        import arti_mind
        from arti_runtime.time_grounding import current_state
        row = arti_mind.read_now(arti_mind.mind_dir())
        return (current_state(row).get("current_time") or None) if row else None
    except Exception:  # noqa: BLE001 - a record field, never a reason to disturb the voice
        return None


def _write_reaction(path, row):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def record_reaction(event, turn_id, kind, text, reply, *, path=None, clock=time.time):
    """Mabar: the owner's own speech, with what ARTI said just before and in answer (owner_reactions.jsonl).

    The owner's mic text only (no chat, no viewer names); written for the post-session reviewer."""
    path = path or os.environ.get("ARTI_MABAR_REACTIONS")
    if not path:
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))
    pending = _reactions["pending"]
    if event == "start" and kind in OWNER_KINDS:
        pending[turn_id] = dict(v=1, mode="mabar", utc=now, kind=kind, owner_text=str(text), game=_game_now(),
                                arti_before=_reactions["last_reply"], arti_before_utc=_reactions["last_reply_utc"])
    elif event == "voiced" and reply:
        _reactions["last_reply"], _reactions["last_reply_utc"] = str(reply), now
        if turn_id in pending:
            row = pending.pop(turn_id)
            _write_reaction(path, dict(row, arti_after=str(reply), arti_after_utc=now))
    elif event == "end" and turn_id in pending:
        _write_reaction(path, dict(pending.pop(turn_id), arti_after=None, arti_after_utc=None, note="no spoken reply"))


def turn_event(event, turn_id, trigger, *, reply=None, **fields):
    text = trigger.text
    kind = ("own_play" if getattr(text, "_own_play", False) else
            "status" if getattr(text, "_live7_status", False) else
            "moment" if getattr(text, "_live7_moment", False) else trigger.trigger_type)
    row = dict(event=event, turn_id=turn_id, kind=kind, **fields)
    if kind == "mic":
        row["owner_sha256"] = owner_hash(str(text))
    emit(row)  # content-free: the reply text never reaches this log
    record_reaction(event, turn_id, kind, text, reply)


def reply_source(call, *, generated=False):
    """Fixed labels only: never persist a model name, reply or error as a source."""
    if generated:
        return "local"
    if "rehearsal_source" in call:
        provider, _, model = str(call["rehearsal_source"]).partition(":")
    elif call.get("local_fallback"):
        return "incharacter"
    else:
        provider, model = call.get("provider"), str(call.get("model", ""))
    if provider == "groq" and model.startswith("openrouter/"):
        return "openrouter"
    return provider if provider in {"codex", "cursor", "agy", "groq", "openrouter", "gemini", "sambanova", "incharacter"} else "unknown"


async def warm_voice(config, provider, *, timeout=60, clock=time.monotonic, sleep=None):
    """Prepare the real voice thread before READY, without manufacturing a turn."""
    import asyncio
    sleep = sleep or asyncio.sleep
    provider.prewarm(config)
    deadline = clock() + timeout
    while not provider.is_warm():
        if clock() >= deadline:
            raise RuntimeError("rehearsal Codex voice warmup unavailable")
        await sleep(.25)


_idle_since = None


def drain_stop(run, *, pending, busy, clock=time.monotonic):
    """Called by the real loop between turns, independent of buffered log timing."""
    global _idle_since
    stopping = (Path(run) / "bridge-stop-request").is_file()
    if not stopping or pending or busy:
        _idle_since = None
    else:
        now = clock()
        if _idle_since is None:
            _idle_since = now
        if now - _idle_since >= 3:
            emit(dict(event="drained", pending=0, idle_s=now - _idle_since))
            (Path(run) / "bridge-drained").touch()
            raise KeyboardInterrupt
    return stopping


def start_parent_watchdog(run, parent_pid):
    """Hold the parent's Windows handle, so PID reuse cannot hide a hard kill."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x100000, False, int(parent_pid))  # SYNCHRONIZE
    if not handle:
        raise RuntimeError("rehearsal parent unavailable; no voice launch")

    def watch():
        try:
            while kernel.WaitForSingleObject(handle, 1000) == 258:  # WAIT_TIMEOUT
                pass
            try:
                sys.stderr.write("!!! UNSAFE_NOT_RESTORED: rehearsal parent lost; terminating owned voice tree !!!\n")
                sys.stderr.flush()
                (Path(run) / "UNSAFE_NOT_RESTORED").write_text(
                    "Parent lost; voice tree terminated. CT must reconcile games, DLL and baseline.\n",
                    encoding="utf-8")
                from scripts.livepath.safety import alert
                alert(run, "parent_lost")
            finally:
                # Only this still-live process and its descendants, never an image name.
                import subprocess
                try:
                    subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"], timeout=10)
                finally:
                    os._exit(3)
        finally:
            kernel.CloseHandle(handle)
    threading.Thread(target=watch, name="livepath-parent-watchdog", daemon=True).start()


def groq_probe(turn_id, trigger, config, system, content, pick, trim):
    """No network, no text in witnesses, and probe errors never affect a turn."""
    try:
        before = system + "\n" + content
        total = len(system) + len(content)
        # Routing can advance a round-robin cursor; preserve it around the dry run.
        missing = object()
        cursor = getattr(pick, "_rr_idx", missing)
        try:
            model = pick(trigger, config, prompt_chars=total)
        finally:
            if cursor is missing:
                if hasattr(pick, "_rr_idx"):
                    delattr(pick, "_rr_idx")
            else:
                pick._rr_idx = cursor
        sys_after, user_after = trim(system, content)
        after = sys_after + "\n" + user_after
        marker = "[Pesan/Panggilan Sekarang:]"
        _, sep, current = content.partition(marker)
        current = sep + current if sep else content
        markers = dict(native_facts="[NATIVE_FACTS]", kini="[KINI]",
                       stardew="[STARDEW]", soul="[JIWA ARTI]")
        survives = {k: v in after for k, v in markers.items()}
        # A retained header with its facts cut off is still a lost native-facts block.
        facts = re.findall(r"\[NATIVE_FACTS\][^\r\n]*", before)
        survives["native_facts"] = bool(facts) and all(line in after for line in facts)
        emit(dict(event="groq_probe", turn_id=turn_id, model=model,
                  chars_before=total, chars_after=len(sys_after) + len(user_after),
                  system_before=len(system), user_before=len(content),
                  system_after=len(sys_after), user_after=len(user_after),
                  size_forced_fast=total > int(config.get("groq_prompt_char_soft_cap", 10000)),
                  current_survives=bool(current) and current in user_after,
                  present={k: v in before for k, v in markers.items()},
                  survives=survives))
    except Exception:
        try:
            emit(dict(event="groq_probe_error", turn_id=turn_id))
        except Exception:
            pass
