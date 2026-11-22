"""buat_karakter.py: Live7 skill runner. ARTI creates her own farmer in the farmhand CharacterCustomization.

  buat_karakter.py [--folder DIR] [--owner-hint TEXT] [--choice-file PATH] [--save-choice PATH] [--preview] [--yes]
                   [--config PATH] [--log PATH] [--max-presses N] [--pace-s SECONDS]

Preconditions: the farmhand joined a brand-new co-op world and stands in the menu the game opens for an unclaimed
cabin (`witness_run.py launch-farmhand --join-slot new` ends there, its exit says char_custom ready); the mod runs with
UiPadMenusEnabled + CharacterCreationEnabled; the farmhand window is INACTIVE (the HOST in front).

Steps, every one logged as one JSON line in --log (default RUN/buat-karakter-<utc>.jsonl):
  1 options_seen   one inspect_context (command inbox, no input): the published char_custom block -> the options
  2 model_choice   ARTI's model (gpt-6-luna via the existing Codex subscription lane, config --config) returns ONE
                   CharacterChoice.v1 with a reason per field (BoundedChooser: at most 3 requests, one ephemeral thread
                   each, hard --model-timeout, default 90 s); validated against the options seen; one re-ask with
                   the refusal as feedback. --choice-file replays a saved answer instead (logged as by=file);
                   --save-choice writes the validated answer (e.g. from the dry run, to replay it with --yes).
  3 press          (--yes only) per step ONE lease (acquire -> inspect under the lease -> one ui_press A {choice,
                   value | text} -> release, the FIXB43 uipad_op pattern): texts, gender, arrows, colour bars, OK.
                   The mod clicks the native component or types through the text-entry primitive and reads it back.
  4 verify         after OK: the native farmer_profile (customized, name, look, cabin owned, free in that cabin).
Without --yes nothing is pressed (dry run: steps 1-2 and the planned first press). --preview stops before OK.
With --yes, pause 1.5 s between changes and 3 s before OK, after verified release. --pace-s 0 disables pacing.

Exit codes: 0 created and verified (or dry run / preview ready) | 2 refused before any input | 3 failed (log names why).
Never during a trial runner; never a coordinate, never the Random button, never B / Escape.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
import arti_stardew_charcreate as cc  # noqa: E402
import arti_stardew_command as command  # noqa: E402
import arti_stardew_ui_pad as ui_pad  # noqa: E402
import m1_paths as paths  # noqa: E402
import uipad_op as U  # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_FAILED = 0, 2, 3
MODEL = "gpt-6-luna"
MAX_MODEL_ASKS = 3            # first answer + one re-ask for a refused answer + one for a text too wide for its box
MAX_WAITS = 20                # consecutive bounded waits (mouse over a text box, draw not yet synced): 0.5 s apart
MAX_REJECTS = 6               # consecutive native rejections (token / lease / name-sync races) before giving up
MAX_SAME_PRESS = 4            # the same control + payload this often without progress = stalled
WAIT_S = 0.5
MODEL_TIMEOUT_S = 90.0        # hard deadline per model answer (cold Codex client + a reasoned CharacterChoice)


class Refused(Exception):
    """Nothing was pressed."""


class Failed(Exception):
    pass


class Log:
    def __init__(self, path: Path | None, sink=None):
        self.path, self.sink, self.records = path, sink, []

    def __call__(self, event, **data):
        rec = cc.log_record(event, utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **data)
        self.records.append(rec)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.sink is not None:
            self.sink(line)
        return rec


# ---------------------------------------------------------------- reads (no input)

def _char_block(context, receiver=None):
    block = context.get("ui_pad") if isinstance(context, dict) else None
    interactive = context.get("interactive") if isinstance(context, dict) else None
    if (not isinstance(block, dict) or not isinstance(interactive, dict) or block.get("context") != cc.CONTEXT
            or block.get("menu_type") != cc.MENU_TYPE or interactive.get("menu_type") != cc.MENU_TYPE
            or interactive.get("receiver_token") != block.get("receiver_token")):
        raise Failed("char_custom_not_open")
    if receiver is not None and block.get("receiver_token") != receiver:
        raise Failed("char_custom_receiver_changed")
    return block


def read_once(io, folder):
    """(session, context) from one inspect_context; no lease, no input."""
    session = io.baca_sesi(folder, retry_detik=0.25)
    return session, U._inspect(io, folder, session)


# ---------------------------------------------------------------- one leased press (uipad_op.close_once pattern)

def press_once(*, io, folder, receiver, decide, clock):
    """Acquire -> inspect under the lease -> decide(menu) -> at most one ui_press -> release (always, verified).
    Returns {"action", "menu", "result"?}. A failure before the press raises Failed / ValueError after the release."""
    session = io.baca_sesi(folder, retry_detik=0.25)
    context = U._inspect(io, folder, session)
    identity = U._identity(session, context)
    _char_block(context, receiver)
    reason = session.alasan_belum_siap(command.OP_INPUT_ONCE, primitive=command.PRIMITIVE_UI_PRESS)
    if reason:
        raise ValueError("session_not_ready:" + reason)
    owner = "buat-karakter-" + uuid.uuid4().hex[:12]
    generation = int(clock.time() * 1000)
    io.tulis_kontrol(command.KIND_ACQUIRE, folder=folder, sesi=session, owner_id=owner,
                     generation=generation, control_seq=1, expected_screen_id=session.screen_id)
    out = {}
    try:
        deadline = clock.monotonic() + 1.0
        current = session
        while clock.monotonic() < deadline:
            current = io.baca_sesi(folder, retry_detik=0.25)
            if current.mentah.get("owner_id") == owner and current.mentah.get("generation") == generation:
                break
            clock.sleep(0.02)
        else:
            raise ValueError("acquire_not_confirmed")
        fresh = U._inspect(io, folder, current)
        if U._identity(current, fresh) != identity:
            raise ValueError("lease_binding_changed")
        block = _char_block(fresh, receiver)
        view = ui_pad.baca_ui_pad(fresh, age_s=0.0)
        menu = cc.parse_block(block)
        if view is None or menu is None:
            raise Failed("char_custom_block_unreadable")
        action = decide(menu)
        out.update(action=action, menu=menu, block=block)
        if action.kind != "press":
            return out
        # Under the generic UI layer the char_custom menu also carries an exit-only affordance list (no exit here);
        # the char_custom context's own button table publishes A, and that is the authority for this menu.
        a_cell = view.buttons.get("A") if view.context == cc.CONTEXT else None
        own_table = isinstance(a_cell, dict) and isinstance(a_cell.get("does"), str)
        cap = ((fresh.get("capabilities") or {}).get("ui_pad") or {}) if isinstance(fresh, dict) else {}
        available = view.available or (own_table and view.ready and cap.get("available") is True)
        if not available or not (view.press_allowed("A") or own_table):
            raise ValueError("press_not_published:" + str(view.wait_reason))
        args = view.press_args("A", **action.args)
        current = io.baca_sesi(folder, retry_detik=0.25)
        admission = U._lease_admission(current, fresh, identity, owner, generation, clock)
        expiry = current.mentah["lease_until_unix_ms"]

        def guarded_publish(publish):
            latest = io.baca_sesi(folder, retry_detik=0.25)
            U._lease_admission(latest, fresh, identity, owner, generation, clock)
            if latest.mentah["lease_until_unix_ms"] < expiry:
                raise ValueError("lease_expiry_shortened")
            publish()

        out["args"] = args
        out["result"] = io.minta_ui_press(**args, folder=folder, sesi=current, owner_id=owner, generation=generation,
                                          control_seq=1, context_generation=fresh["context_generation"],
                                          runtime_instance_id=fresh["runtime_instance_id"],
                                          expected_location_id=current.location_id, expected_screen_id=current.screen_id,
                                          admisi_detik=admission, deadline_cap_unix_ms=expiry, batas_detik=4.5,
                                          guarded_publish=guarded_publish)
    finally:
        current = io.baca_sesi(folder, retry_detik=0.25)
        seq = current.mentah.get("control_seq") if current.mentah.get("owner_id") == owner else 1
        io.tulis_kontrol(command.KIND_RELEASE, folder=folder, sesi=session, owner_id=owner,
                         generation=generation, control_seq=(seq if type(seq) is int else 1) + 1,
                         expected_screen_id=session.screen_id)
        deadline = clock.monotonic() + 1.0
        while clock.monotonic() < deadline:
            released = io.baca_sesi(folder, retry_detik=0.25)
            if (command.lease_state(released.mentah).bound(command.lease_state(session.mentah))
                    and command.lease_state(released.mentah).retired(owner, generation, ((seq if type(seq) is int else 1) + 1,))
                    and released.inflight_request_id is None
                    and command.input_release_state(released.mentah, request_id=getattr(out.get("result"), "request_id", None),
                        result=getattr(out.get("result"), "mentah", None)) is True):
                break
            clock.sleep(0.02)
        else:
            raise Failed("release_not_confirmed")
    return out


def result_view(result):
    raw = (getattr(result, "mentah", None) or {}) if result is not None else {}
    return {"status": getattr(result, "status", None), "success": getattr(result, "sukses", None),
            "outcome": raw.get("outcome"), "reason": raw.get("ui_pad_reason") or getattr(result, "alasan", None),
            "effect": raw.get("ui_key_effect"), "readback": raw.get("ui_pad_readback"),
            "calls": len(raw.get("ui_pad_calls") or []), "request_id": getattr(result, "request_id", None)}


# ---------------------------------------------------------------- ARTI's model

class BoundedChooser:
    """One CharacterChoice answer from ARTI's admitted Codex lane, bounded.

    The runtime's public ``complete()`` renders GameDecision requests from a game snapshot and cannot carry this
    contract, so this wrapper keeps that path's safety terms for a provider instance this process owns alone:
    the admission policy (construct_stardew_decision_provider), at most MAX_MODEL_ASKS physical requests, one at a
    time (refused while the provider reports a physical request in flight), ONE ephemeral foreground thread per answer
    (``_prepare_new_thread``: never the spare background thread ``_prepare_physical`` starts), and a HARD deadline:
    the call runs in a daemon worker; past the deadline the answer is abandoned (a late result is discarded, it has no
    callback), the lane refuses every further request, and the caller closes the provider (its SDK app-servers stop)
    and exits, so a stuck run can never hang the runner."""

    def __init__(self, provider, timeout_s: float = MODEL_TIMEOUT_S, max_requests: int = MAX_MODEL_ASKS):
        self.provider, self.timeout_s, self.max_requests = provider, float(timeout_s), max_requests
        self.requests = 0
        self.abandoned = False

    def _prepare(self):
        prepare = getattr(self.provider, "_prepare_new_thread", None)
        if prepare is None:
            raise RuntimeError("provider_without_codex_thread")   # only the admitted Codex subscription lane
        return prepare(False)

    def __call__(self, messages):
        if self.abandoned:
            raise RuntimeError("model_lane_abandoned_after_timeout")
        if self.requests >= self.max_requests:
            raise RuntimeError("model_request_cap")
        if getattr(self.provider, "physical_inflight", False) is True:
            raise RuntimeError("provider_physical_inflight")
        self.requests += 1
        box: dict = {}
        done = threading.Event()
        payload = {"response_format": {"json_schema": {"schema": cc.CHOICE_SCHEMA}}}

        def work():
            try:
                box["text"] = self.provider._physical_text(messages, payload, False, self._prepare())
            except BaseException as exc:  # noqa: BLE001 - surfaced to the caller below
                box["error"] = exc
            finally:
                done.set()
        threading.Thread(target=work, name="buat-karakter-model", daemon=True).start()
        if not done.wait(self.timeout_s):
            self.abandoned = True
            raise TimeoutError(f"model_timeout:{self.timeout_s:.0f}s")
        if "error" in box:
            raise box["error"]
        if not isinstance(box.get("text"), str):
            raise RuntimeError("model_invalid_response")
        return box["text"]


def codex_chooser(config_path: Path, timeout_s: float = MODEL_TIMEOUT_S):
    """ARTI's own decision model through the existing admitted Codex subscription lane (gpt-6-luna, config effort and
    tier), bounded by :class:`BoundedChooser`. The provider is closed by the caller."""
    import arti_stardew_runtime as rt
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    if config.get("stardew_decision_model") != MODEL:
        raise Refused(f"config model {config.get('stardew_decision_model')!r} is not ARTI's {MODEL}")
    provider = rt.construct_stardew_decision_provider(config)
    if provider is None or provider.policy.provider_kind != "codex_subscription":
        raise Refused("ARTI's Codex decision lane is not admitted by this config (stardew_decision_provider_admission)")
    return BoundedChooser(provider, timeout_s), provider


def readback(io, folder, expected, log, clock, *, wait_s=5.0, after=None):
    """The native farmer_profile after OK, always logged (also when the OK verdict was uncertain). -> mismatches."""
    deadline = clock.monotonic() + wait_s
    profile = interactive = None
    try:
        while True:
            session, context = read_once(io, folder)
            profile = context.get("farmer_profile")
            interactive = (context.get("interactive") or {}).get("menu_type")
            bad = cc.verify_created(profile, expected, interactive_menu=interactive)
            if not bad or clock.monotonic() >= deadline:
                break
            clock.sleep(WAIT_S)
    except (ValueError, OSError, Failed, command.KesalahanPerintah) as exc:
        bad = ["readback_unavailable:" + str(exc)[:120]]
    log("verify", passed=not bad, mismatches=bad, expected=expected, farmer_profile=profile, interactive_menu=interactive,
        after=after)
    return bad


def ask_model(chooser, seen, owner_hint, log, *, feedback=(), attempt0=0):
    """Up to two model answers (the second only with the first one's refusal as feedback). -> (choice, asks)."""
    feedback = list(feedback)
    for attempt in range(attempt0, min(attempt0 + 2, MAX_MODEL_ASKS)):
        messages = cc.choice_messages(seen, owner_hint=owner_hint, feedback=feedback)
        log("model_request", attempt=attempt + 1, model=MODEL, messages=messages)
        try:
            raw = chooser(messages)
        except Exception as exc:  # noqa: BLE001 - a provider failure ends the skill before any input of this choice
            log("model_error", attempt=attempt + 1, error=f"{type(exc).__name__}: {exc}"[:300])
            return None, attempt + 1
        choice, errors = cc.parse_choice(raw, seen)
        log("model_choice", attempt=attempt + 1, raw=raw if isinstance(raw, str) else json.dumps(raw), valid=choice is not None,
            errors=errors, choice=choice.wire() if choice else None, reasons=choice.reasons if choice else None,
            summary=choice.summary if choice else None)
        if choice is not None:
            return choice, attempt + 1
        feedback = errors
    return None, min(attempt0 + 2, MAX_MODEL_ASKS)


# ---------------------------------------------------------------- the run

def pace_wait(seconds, *, io, folder, clock):
    """Only after press_once has verified release; short slices leave Ctrl+C responsive."""
    deadline = clock.monotonic() + seconds
    while True:
        if io.baca_sesi(folder, retry_detik=0.25).mentah.get("stop_latched"):
            raise Failed("pace_cancelled:stop_latched")
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            return
        clock.sleep(min(0.1, remaining))


def run(a, *, io=command, clock=time, chooser=None, log=None, trial_running=None):
    log = log or Log(None)
    folder = a.folder
    log("start", folder=str(folder), yes=bool(a.yes), preview=bool(a.preview), owner_hint=a.owner_hint,
        choice_file=str(a.choice_file) if a.choice_file else None)
    if trial_running is not None and trial_running() is not False:
        raise Refused("a trial runner is running (or the process list is unknown)")
    session, context = read_once(io, folder)
    block = _char_block(context)
    receiver = block["receiver_token"]
    menu = cc.parse_block(block)
    if menu is None:
        raise Refused("char_custom block unreadable (mod action table or shape changed)")
    seen = cc.catalog(menu)
    start_profile = context.get("farmer_profile")
    log("options_seen", receiver=receiver, options=seen, block=block, farmer_profile=start_profile)

    if a.choice_file:
        raw = Path(a.choice_file).read_text(encoding="utf-8-sig")
        choice, errors = cc.parse_choice(raw, seen)
        log("model_choice", attempt=0, by="file", raw=raw, valid=choice is not None, errors=errors,
            choice=choice.wire() if choice else None, reasons=choice.reasons if choice else None,
            summary=choice.summary if choice else None)
        asks = 0
        if choice is None:
            raise Refused("choice file refused: " + ",".join(errors))
    else:
        if chooser is None:
            raise Refused("no model chooser (production: --config with ARTI's gpt-6-luna lane)")
        choice, asks = ask_model(chooser, seen, a.owner_hint, log)
        if choice is None:
            raise Refused("ARTI's model gave no valid CharacterChoice")
    if getattr(a, "save_choice", None):
        Path(a.save_choice).write_text(json.dumps(dict(contract=cc.CHOICE_CONTRACT, **choice.wire(), reasons=choice.reasons,
                                                       summary=choice.summary), ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        log("choice_saved", path=str(a.save_choice))
    first = cc.next_action(menu, choice, confirm=not a.preview)
    if not a.yes:
        log("dry_run", first_action=first.__dict__, note="nothing pressed (--yes presses)")
        return EXIT_OK, {"dry_run": True, "choice": choice.wire(), "first_action": first.__dict__}

    presses = waits = rejects = 0
    seen_press: dict[str, int] = {}
    pressed_pickers: set[str] = set()
    ok_menu = None
    paced_control = None

    def decide(menu):
        action = cc.next_action(menu, choice, confirm=not a.preview)
        if (a.pace_s > 0 and action.kind == "press" and (presses or action.control == "ok")
                and action.control != paced_control):
            # Defer the press, release and verify first, then wait in the outer loop.
            return cc.Action("wait", control=action.control, reason="viewer_pace")
        return action

    while True:
        if presses >= a.max_presses:
            raise Failed(f"press_budget:{a.max_presses}")
        try:
            out = press_once(io=io, folder=folder, receiver=receiver,
                             decide=decide, clock=clock)
        except ValueError as exc:
            # Lease / token / identity races refused before the press (the release ran): bounded retry.
            rejects += 1
            log("retry", reason=str(exc), rejects=rejects)
            if rejects > MAX_REJECTS:
                raise Failed("press_refused:" + str(exc))
            clock.sleep(WAIT_S)
            continue
        action = out["action"]
        if action.kind == "fail":
            raise Failed(action.reason)
        if action.kind == "done":
            log("preview_ready", menu=out["block"])
            return EXIT_OK, {"preview": True, "choice": choice.wire(), "presses": presses}
        if action.kind == "wait":
            if action.reason == "viewer_pace":
                seconds = a.pace_s * (2 if action.control == "ok" else 1)
                log("pace", before=action.control, seconds=seconds)
                pace_wait(seconds, io=io, folder=folder, clock=clock)
                paced_control = action.control
                continue  # Re-inspect under a NEW lease; never replay a stale action after the wait.
            waits += 1
            log("wait", reason=action.reason, waits=waits)
            if waits > MAX_WAITS:
                raise Failed("wait_budget:" + str(action.reason))
            clock.sleep(WAIT_S)
            continue
        presses += 1
        view = result_view(out.get("result"))
        log("press", n=presses, control=action.control, args=action.args, result=view)
        if view["status"] == command.TIDAK_PASTI:
            # Never press on after an uncertain verdict (the mod latches the receiver too). An uncertain OK may still have
            # created the farmer: its native readback is logged before the run fails.
            if action.control == "ok":
                readback(io, folder, cc.expected_profile(out["menu"], choice, pressed_pickers, start_profile), log, clock,
                         wait_s=0.0, after="ok_uncertain")
            raise Failed(f"press_uncertain:{action.control}:{view['reason']}")
        if view["status"] in (command.DITOLAK, command.GAGAL_LOKAL):
            rejects += 1
            if rejects > MAX_REJECTS:
                raise Failed(f"press_rejected:{action.control}:{view['reason']}")
            clock.sleep(WAIT_S)
            continue
        if view["success"]:
            paced_control = None
            rejects = waits = 0                  # the bounds count CONSECUTIVE refusals / waits
            control = action.control
            if "_" in control and control.rsplit("_", 1)[1] in cc.BARS:
                pressed_pickers.add(control.rsplit("_", 1)[0])
            if control == "ok":
                ok_menu = out["menu"]
                break
            continue                         # an applied press always changed a published value
        if view["reason"] == "text_too_wide":
            field = "name" if action.control == "name" else "favorite_thing"
            if a.choice_file or asks >= MAX_MODEL_ASKS:
                raise Failed(f"text_too_wide:{field}")
            choice, asks = ask_model(chooser, seen, a.owner_hint, log, attempt0=asks,
                                     feedback=[f"{field}:too_wide_for_the_box_use_fewer_or_narrower_letters"])
            if choice is None:
                raise Failed(f"text_too_wide:{field}")
            seen_press.clear()
            continue
        # No effect (e.g. guard_refused:* before any native call, ok_not_accepted): retried a bounded number of times.
        key = action.control + json.dumps(action.args, sort_keys=True)
        seen_press[key] = seen_press.get(key, 0) + 1
        if seen_press[key] >= MAX_SAME_PRESS:
            raise Failed(f"stalled:{action.control}:{view['reason']}")
        clock.sleep(WAIT_S)

    bad = readback(io, folder, cc.expected_profile(ok_menu, choice, pressed_pickers, start_profile), log, clock,
                   after="ok_applied")
    if bad:
        raise Failed("verify:" + ";".join(bad))
    return EXIT_OK, {"created": True, "name": choice.name, "presses": presses, "summary": choice.summary}


def pace_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("pace must be finite and non-negative")
    return seconds


def build_parser():
    ap = argparse.ArgumentParser(prog="buat_karakter.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", default=str(paths.COMMAND_DIR), help="the mod's command inbox (default: the installed mod's)")
    ap.add_argument("--owner-hint", default=None, help="what the owner said on stream (a wish, never an order)")
    ap.add_argument("--choice-file", default=None, help="replay a saved CharacterChoice.v1 instead of asking the model")
    ap.add_argument("--save-choice", default=None, help="write the validated CharacterChoice.v1 here (replay with --choice-file)")
    ap.add_argument("--config", default=str(paths.TRIAL_CONFIG), help="runtime config with ARTI's gpt-6-luna Codex lane")
    ap.add_argument("--log", default=None, help="JSONL log (default RUN/buat-karakter-<utc>.jsonl)")
    ap.add_argument("--preview", action="store_true", help="apply every choice but stop before OK")
    ap.add_argument("--yes", action="store_true", help="press (without it: dry run, nothing pressed)")
    ap.add_argument("--pace-s", type=pace_seconds, default=1.5,
                    help="seconds between changes with --yes; twice this before OK (default 1.5; 0 disables)")
    ap.add_argument("--max-presses", type=int, default=120)
    ap.add_argument("--model-timeout", type=float, default=MODEL_TIMEOUT_S,
                    help="hard deadline in seconds per model answer (10-300); past it the answer is abandoned")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_path = Path(a.log) if a.log else paths.RUN / f"buat-karakter-{stamp}.jsonl"
    log = Log(log_path)
    provider = None
    rc, report = EXIT_FAILED, {}
    try:
        import win_facts as wf
        chooser = None
        if not a.choice_file:
            chooser, provider = codex_chooser(Path(a.config), min(300.0, max(10.0, a.model_timeout)))
        rc, report = run(a, chooser=chooser, log=log, trial_running=wf.trial_running)
    except Refused as exc:
        rc, report = EXIT_REFUSED, {"refused": str(exc)}
    except (Failed, ValueError, OSError, command.KesalahanPerintah) as exc:
        rc, report = EXIT_FAILED, {"failed": str(exc)}
    finally:
        if provider is not None:
            provider.close()
    log("end", exit=rc, report=report)
    print(json.dumps(dict(report, exit=rc, log=str(log_path)), ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
