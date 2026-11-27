"""WS2 runtime side of ``ui_key`` + the deterministic day-start rule + the B8 store-close rule.

Kept out of ``arti_stardew_runtime.py`` on purpose (several workstreams edit that file in parallel):
``StardewGoalRuntime`` inherits :class:`UiKeyRuntimeMixin` and calls a handful of hooks.

Every physical step here uses the existing lease protocol (ACQUIRE -> one guarded input_once ->
RELEASE), the existing session-limit/trial-input accounting (``_publish_action``) and the existing
priority STOP (``_water_action_pin``). The runtime never invents a target: ui_key args are copied
from the mod's published ``ui_key`` block, the store-close args from the published chest close target.

Evidence ceiling: UNIT_TESTED. End-of-night observability (ui.context while the farmhand saves and
the host syncs) is a HYPOTHESIS until the live witness.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

import copy
import threading
from collections.abc import Mapping
import time
from arti_stardew_pause import game_now, wait_resume
from dataclasses import asdict, dataclass, replace
from typing import Any

import arti_stardew_command as command
from arti_stardew_reading_dwell import DWELL_CONTEXTS, PageClock, ReadingDwell, page_key, page_text
from arti_stardew_day_start import DayStartPolicy, DayStartState, DayStartStep, note_result, plan_day_start_step
from arti_stardew_ui import UiKeyView, baca_ui_key

MODEL_UI_KEY_KINDS = ("dialogue", "question", "npc_question", "letter")
MODEL_UI_KEY_KEYS = ("advance", "answer", "cancel", "close")
_DAY_START_WAIT_JOURNAL_S = 10.0
# Reading dwell (smoke 2026-10-23 item 8) for these _run_ui_key origins. day_start dwells in its own planner;
# skill_terminal_cleanup / failsafe closes keep their own short bounds and never wait.
_DWELL_UI_KEY_ORIGINS = ("model", "routine_", "extras_")
# _run_ui_pad ui_press origins: model/skill decisions (S29 TV + mail pages) and text events. skill_found_dialogue
# keeps its 15 s per-box cap; menu/economy/festival rules are not reading pages.
_DWELL_UI_PAD_ORIGINS = ("model", "text_event")
# Never hold ARTI still to read in a fight-capable place or right after she was hurt.
_DWELL_HOSTILE_PREFIXES = ("UndergroundMine", "VolcanoDungeon", "Mine")
_DWELL_DANGER_S = 15.0
_READBACK_WAIT_S = 2.5  # ui.context is re-sent when dirty; allow a slow end-of-night frame
# Leased finite primitives: session enabled key, command-client call, owner-id label. Primitives v2 WS-G1 adds
# ui_nav / ui_press (ui_pad_enabled) and the WS-G0 pad_button (pad_button_enabled) to the same envelope.
_LEASED_ROUTES = {
    "select_item": ("select_item_enabled", "minta_select_item", "select"),
    "world_interact": ("world_interaction_enabled", "minta_world_interaction", "event"),
    command.PRIMITIVE_UI_KEY: ("ui_key_enabled", "minta_ui_key", "uikey"),
    "container_exit": ("container_exit_enabled", "minta_container_exit", "close"),
    command.PRIMITIVE_UI_NAV: ("ui_pad_enabled", "minta_ui_nav", "uinav"),
    command.PRIMITIVE_UI_PRESS: ("ui_pad_enabled", "minta_ui_press", "uipress"),
    command.PRIMITIVE_PAD_BUTTON: ("pad_button_enabled", "minta_pad_button", "padbtn"),
    # Primitives v2 WS-G2: container_transfer WITH a mode (ship_one / ship_stack / take_out) only; the v1 store chain
    # keeps its own path (_execute_store_chain), this route never carries a v1 transfer.
    "container_transfer": ("container_transfer_enabled", "minta_container_transfer", "xfer"),
}
# pad_button readback = the menu it opens. LB / RB (toolbar shift) have no menu: they never verify here, so the
# runtime never offers them (arti_stardew_ui_pad.OPEN_BUTTON holds Start / Select only; review minor 4).
_PAD_BUTTON_MENU = {"Start": "GameMenu", "Y": "GameMenu", "Select": "QuestLog"}


@dataclass(frozen=True)
class LeasedRun:
    """Outcome of one leased finite input (ui_key or the store close)."""
    lifecycle: str                 # completed | rejected | interrupted | not_taken | uncertain
    reason: str
    request_id: str | None = None
    outcome: str | None = None
    applied: bool = False          # mod verdict success
    verified: bool = False         # applied AND strictly newer readback agrees
    submitted: bool = False
    cleanup: bool = True
    effect: str | None = None
    result_receiver_token: str | None = None
    result_menu_type: str | None = None
    context_revision: int | None = None
    readback: str | None = None    # Primitives v2 WS-G3: the mod's ui_pad_readback (name / number / answer / item)
    economy: dict | None = None     # WS-G2: the mod's ui_pad_economy ledger (ui_press on a shop / crafting row)
    input_count: int | None = None  # correlated native publications, never inferred from the reservation

    prearm_rejected: bool = False  # correlated native no-input refusal; never a verified effect

    @property
    def hard_uncertain(self) -> bool:
        return self.lifecycle in {"uncertain", "interrupted"} or (self.submitted and not self.cleanup)

    @property
    def uncertain(self) -> bool:
        return self.hard_uncertain or (self.applied and not self.verified)


class UiKeyRuntimeMixin:
    """Mixed into StardewGoalRuntime; uses its lock, stores, lease helpers and journal."""

    # ------------------------------------------------------------------ setup / flags
    def _init_ui_key_runtime(self) -> None:
        self._ui_key_uncertain: dict[str, str] = {}
        self._day_start_policy = DayStartPolicy.from_config(self._config)
        self._day_start_state = DayStartState()
        self._day_start_lock = threading.Lock()
        self._day_start_last_date: tuple | None = None
        self._day_start_last_terminal: tuple | None = None
        self._owner_play_stopped = False
        self._day_start_submitted = 0  # day-start runs that reached a request write (pre-phase accounting)
        # PART_C2: agent (non-day-start) publications, counted at the _publish_action boundary, and their count when
        # the date last changed. More agent publications since then close the wake-up dialogue window.
        self._day_start_publishing = False
        self._agent_publications = 0
        self._day_start_agent_base: int | None = None
        self._store_close_enabled = self._config.get("stardew_store_close_after_finish", True) is not False
        self._reading_dwell = ReadingDwell.from_config(self._config)
        self._reading_pages = PageClock()

    @property
    def _day_start_active(self) -> bool:
        state = getattr(self, "_day_start_state", None)
        return bool(state is not None and state.active)

    # ------------------------------------------------------------------ reads
    def _ui_key_record(self, player_id: int | None):
        if player_id is None:
            return None, None
        rec = self._obs.get(player_id, "ui.context")
        if (rec is None or not rec.is_complete or not rec.authoritative or bool(getattr(rec, "stale", False))
                or bool(getattr(rec, "requires_resync", False))):
            return None, None
        now = time.monotonic()
        age = now - rec.received_monotonic
        if not 0 <= age <= 2:
            return None, None
        pages = getattr(self, "_reading_pages", None)
        policy = getattr(self, "_reading_dwell", None)
        if pages is not None and policy is not None and policy.on:  # zero bookkeeping with the dwell off
            self._reading_note_health(player_id, now)
            # First sighting of a page: typing and provider latency already count as reading time.
            key = page_key(rec.facts)
            if key is not None:
                pages.note(key, rec.received_monotonic)
        return rec, age

    def _reading_note_health(self, player_id: int | None, now: float) -> None:
        """Remember the last native HP loss (monotonic) for the dwell's danger check; read-only."""
        state = getattr(self, "_state", None)
        hp = getattr(state.latest(player_id), "health", None) if state is not None and player_id is not None else None
        last = getattr(self, "_reading_hp", None)
        if type(hp) is int:
            if type(last) is int and hp < last:
                self._reading_hp_drop_at = now
            self._reading_hp = hp

    def _reading_danger(self, player_id: int | None) -> str | None:
        """Why a reading dwell must not hold ARTI still now: a hostile location, a fresh HP loss, or a danger
        hook (``_danger_until`` monotonic, or the self-defence policy's ``damaged_at`` game clock)."""
        state = getattr(self, "_state", None)
        motor = state.latest(player_id) if state is not None and player_id is not None else None
        location = str(getattr(motor, "location", "") or "")
        if location.startswith(_DWELL_HOSTILE_PREFIXES):
            return "hostile_location"
        now = time.monotonic()
        if now - getattr(self, "_reading_hp_drop_at", float("-inf")) < _DWELL_DANGER_S:
            return "hp_dropped"
        if now < getattr(self, "_danger_until", float("-inf")):
            return "danger_hook"
        live = getattr(getattr(getattr(self, "_skill_session", None), "campaign", None), "live", None)
        damaged, clock = getattr(live, "damaged_at", None), getattr(motor, "time_of_day", None)
        if type(damaged) is int and type(clock) is int and 0 <= (clock // 100 * 60 + clock % 100) - (
                damaged // 100 * 60 + damaged % 100) <= 20:
            return "under_attack"
        return None

    @W.span('ui_dwell', 91, runtime=True)
    def _reading_dwell_wait(self, player_id: int | None, *, kind: str | None, origin: str) -> float:
        """Viewer reading time before a confirming key. Runs BEFORE any ACQUIRE/request write, never under a
        lease or the runtime lock; STOP / action cancel end it at once (the leased path then refuses)."""
        policy = getattr(self, "_reading_dwell", None)
        rec, _ = self._ui_key_record(player_id)
        if rec is None:
            return 0.0
        from arti_stardew_screen_text import page_fields
        fields = page_fields(rec.facts, kind)
        # Capture before a confirm even with dwell disabled, elapsed, or danger-skipped.
        if fields.get("text") or fields.get("options"):
            self._journal({"ev": "ui_reading_dwell", "at": time.time(), "origin": origin,
                           "kind": kind, "chars": len(page_text(rec.facts)), **fields})
        if policy is None or not policy.on:
            return 0.0
        danger = self._reading_danger(player_id)
        if danger is not None:
            self._journal({"ev": "ui_reading_dwell", "origin": origin, "kind": kind, "skipped": danger})
            return 0.0
        text = page_text(rec.facts)
        need = policy.seconds(text, kind=kind)
        start = game_now(self)
        left = self._reading_pages.remaining(page_key(rec.facts), need, start)
        if left <= 0:
            return 0.0
        self._journal({"ev": "ui_reading_dwell", "origin": origin, "kind": kind, "chars": len(text), **fields,
                       "need_s": round(need, 2), "wait_s": round(left, 2)})
        while game_now(self) - start < left:
            if self._worker_stop.is_set() or self._action_cancel.is_set() or self._reading_danger(player_id):
                break
            W.call('ui_dwell', 94, self._worker_stop.wait, min(0.1, max(0.0, left - (game_now(self) - start))))
        return game_now(self) - start

    def _ui_key_context(self, player_id: int | None):
        """``_water_context`` for a menu-mode ui.context: never requires gameplay or a free player."""
        from arti_stardew_runtime import MovementContext
        rec, _ = self._ui_key_record(player_id)
        if rec is None:
            return None
        facts = rec.facts
        for key in ("runtime_instance_id", "session_id", "location_id"):
            if not isinstance(facts.get(key), str) or not facts[key] or len(facts[key]) > 128:
                return None
        for key in ("player_id", "screen_id", "context_generation", "ui_revision"):
            if type(facts.get(key)) is not int:
                return None
        if (facts["context_generation"] < 1 or facts["screen_id"] < 0
                or facts["player_id"] != rec.player_id or facts["screen_id"] != rec.screen_id):
            return None
        return MovementContext(facts["runtime_instance_id"], facts["session_id"], rec.session_id,
                               facts["context_generation"], rec.player_id, rec.screen_id,
                               facts["location_id"], facts["ui_revision"])

    def _ui_key_view(self, player_id: int | None) -> UiKeyView | None:
        rec, age = self._ui_key_record(player_id)
        if rec is None:
            return None
        return baca_ui_key(rec.facts, age_s=age, authoritative=rec.authoritative, complete=rec.is_complete)

    def _ui_key_session(self, folder, ctx, *, enabled_key: str, primitive: str, acquired: bool,
                        owner: str | None = None, generation: int | None = None):
        """Same session preconditions as ``_water_precheck`` for one bound farmhand; raises ValueError."""
        from arti_stardew_runtime import _SESSION_READ_RETRY_SECONDS
        binding, pid = self._binding_view()
        if (binding.status != "bound" or pid is None or pid != ctx.player_id or binding.is_main_player is not False
                or binding.is_split_screen is not False):
            raise ValueError("binding_mismatch")
        session = self._commands.baca_sesi(folder, retry_detik=_SESSION_READ_RETRY_SECONDS)
        raw = session.mentah
        if not command.lease_state(raw).matches(ctx.command_session_id, ctx.runtime_instance_id,
                pid, ctx.screen_id, ctx.location, binding.player_name):
            raise ValueError("command_binding_mismatch")
        admission = command.admission_state(raw, ((enabled_key, True), ("release_acked", True)))
        if admission:
            raise ValueError("session_precondition:" + admission)
        reason = session.alasan_belum_siap(command.OP_INPUT_ONCE, primitive=primitive)
        if reason:
            raise ValueError("session_unavailable")
        if self._live7_stop_pending(folder, session):
            raise ValueError("stop_pending")
        if not acquired:
            if (not command.lease_state(raw).can_acquire(generation or self._lease_generation + 1)
                    or (folder / command.NAMA_CONTROL).exists() or (folder / command.NAMA_REQUEST).exists()):
                raise ValueError("owner_or_slot_busy")
        elif not command.lease_state(raw).owns(owner, generation, 1, live=True):
            raise ValueError("lease_expired_or_mismatched")
        return session

    # ------------------------------------------------------------------ model offering
    def _ui_key_offer(self, binding, player_id: int | None) -> dict | None:
        """Snapshot entry: only dialogue/question/npc_question/letter, never runtime-owned kinds."""
        if self._day_start_active or binding.status != "bound" or player_id is None:
            return None
        view = self._ui_key_view(player_id)
        ctx = self._ui_key_context(player_id)
        if (view is None or ctx is None or view.kind not in MODEL_UI_KEY_KINDS or not view.available
                or view.receiver_token in self._ui_key_uncertain or ctx.context_generation != view.context_generation):
            return None
        if not self._ui_key_slot_free(binding, ctx):
            return None
        if not self._routine_question_offerable(view):
            return None  # WS3b: a walk across the bed must never let the model end the day
        return {"receiver_token": view.receiver_token, "menu_type": view.menu_type, "kind": view.kind,
                "keys": [{"key": k, "target_token": t} for k, t in view.keys],
                "options": [{"index": o["index"], "label": o["label"]} for o in view.options]}

    def _ui_key_slot_free(self, binding, ctx) -> bool:
        try:
            folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
            self._ui_key_session(folder, ctx, enabled_key="ui_key_enabled", primitive=command.PRIMITIVE_UI_KEY,
                                 acquired=False)
            return True
        except (OSError, ValueError, TypeError, command.KesalahanPerintah):
            return False

    @staticmethod
    def _ui_key_offer_payload(target: dict) -> dict:
        return {"target_ref": None, "observed_target": target,
                "args": "copy receiver_token, menu_type, kind, key, target_token; option_index for answer",
                "result_scope": "one menu step; no world/resource credit"}

    # ------------------------------------------------------------------ leased finite input
    def _run_leased_once(self, primitive: str, args: dict, ctx, *, origin: str, revalidate,
                         expect_outcome: str | None = None, publication_cost: int = 1) -> LeasedRun:
        """ACQUIRE -> one guarded input_once -> RELEASE, renewing the 1.5 s lease while the mod settles.

        ``revalidate()`` runs under the runtime lock immediately before the request write and must raise
        ValueError when the published target changed. Never retried; an ambiguous write stays uncertain.
        """
        from arti_stardew_runtime import _WaterLeasePin, _new_id
        wait_resume(self)
        enabled_key, invoke_name, owner_label = _LEASED_ROUTES[primitive]
        success = (command.OUTCOME_SUKSES_PAD_BUTTON.get(args.get("button")) if primitive == command.PRIMITIVE_PAD_BUTTON
                   else command.OUTCOME_SUKSES_PRIMITIVE.get(primitive))
        if expect_outcome is not None:
            success = expect_outcome  # FIXB31: the event skip (pad_button Select in a cutscene) succeeds as EventSkipped
        invoke = getattr(self._commands, invoke_name)
        folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
        pin = None
        submitted = False
        cleanup = True
        result = None
        call_done, call_state = threading.Event(), {}
        call_thread = None
        renewal_failure = None
        before_rev = None
        selection_revision = None
        toolbar_before = None
        current = None
        native_request_id = None
        transfer_before = None
        press_container_before = None
        item_view_before = None

        def finish(run):
            return self._leased_finish(pin, call_thread, call_done, run, call_state=call_state,
                                       primitive=primitive, args=args, ctx=ctx, session=current,
                                       native_request_id=native_request_id)
        try:
            rec = self._obs.get(ctx.player_id, "ui.context")
            before_rev = rec.domain_revision if rec is not None else None
            live_moves = getattr(getattr(self, "_skill_session", None), "mode", None) == "part_live"
            if live_moves and primitive == command.PRIMITIVE_UI_PRESS and args.get("context") == "other":
                item_view_before = self._ui_pad_view(ctx.player_id)
            if live_moves and primitive == "container_transfer" and args.get("mode") not in ("ship_one", "ship_stack"):
                transfer_before = self._container_view(ctx)
            elif (live_moves and primitive == command.PRIMITIVE_UI_PRESS
                  and isinstance(getattr(rec, "facts", {}).get("container"), dict)):
                press_container_before = self._container_view(self._water_context(ctx.player_id, container=True))
            if primitive == command.PRIMITIVE_PAD_BUTTON and args.get("button") in ("LB", "RB"):
                from arti_stardew_generic_world import inventory, fingerprint
                rows = inventory(self, ctx)
                if len(rows) <= 12:
                    raise ValueError("toolbar_rows_unobserved")
                toolbar_before = fingerprint(rows)
            if primitive == "select_item":
                inventory = self._obs.get(ctx.player_id, "progress.inventory")
                selection_revision = inventory.domain_revision if inventory is not None else None
            with self._lock:
                if self._worker_stop.is_set() or self._action_cancel.is_set():
                    raise ValueError("action_cancelled")
                session = self._ui_key_session(folder, ctx, enabled_key=enabled_key, primitive=primitive, acquired=False)
                # Reserve the known route before starting the 1.5 s lease and
                # one-second admission window. Durable budget journal writes
                # may be slow; they must not age a prepared native request.
                # Reservations remain non-refundable, including a partial refusal.
                for _ in range(max(1, publication_cost)):
                    self._publish_action(lambda: None)
                if self._worker_stop.is_set() or self._action_cancel.is_set():
                    raise ValueError("action_cancelled")
                session = self._ui_key_session(folder, ctx, enabled_key=enabled_key, primitive=primitive, acquired=False)
                self._lease_generation += 1
                generation = self._lease_generation
                owner = _new_id("g1-" + owner_label)
                pin = _WaterLeasePin(folder, session, owner, generation)
                self._water_action_pin = pin
                self._commands.tulis_kontrol(command.KIND_ACQUIRE, folder=folder, sesi=session, owner_id=owner,
                                             generation=generation, control_seq=1, expected_screen_id=session.screen_id)
            current = self._wait_water_acquire(pin)
            lease_until = command.lease_state(current.mentah).expiry
            if type(lease_until) is not int:
                raise ValueError("lease_expired_or_mismatched")

            def guarded(publish):
                nonlocal submitted
                with self._lock:
                    self._publication_guard()
                    if self._worker_stop.is_set() or self._action_cancel.is_set():
                        raise ValueError("action_cancelled")
                    self._ui_key_session(folder, ctx, enabled_key=enabled_key, primitive=primitive, acquired=True,
                                         owner=pin.owner, generation=pin.generation)
                    revalidate()

                    def write():
                        nonlocal submitted
                        submitted = True  # a write error can be ambiguous; never replay
                        publish()
                    # The full known cost, including a single input, was reserved
                    # before ACQUIRE (ADR 73/111). Keep durable accounting off the
                    # live lease; identity/offer/STOP checks above still guard it.
                    write()

            @W.task(self)
            def call():
                wait_token = command.result_wait_clock.set(getattr(self, "_action_wait_now", lambda: game_now(self)))
                try:
                    route_deadlines = {}
                    if primitive == "world_interact" and args.get("kind") in command.EVENT_ROUTE_KINDS:
                        route_deadlines["execution_deadline_unix_ms"] = int(time.time() * 1000) + (120_000 if args.get("kind") in command.APPROACH_ROUTE_KINDS else 20_000)
                        if self._trial_deadline_unix_ms is not None:
                            route_deadlines["deadline_cap_unix_ms"] = self._trial_deadline_unix_ms
                    self._last_input_request_id = None
                    call_state["result"] = invoke(
                        **args, folder=folder, sesi=current, owner_id=pin.owner, generation=pin.generation,
                        control_seq=1, context_generation=ctx.context_generation,
                        runtime_instance_id=ctx.runtime_instance_id, expected_location_id=current.location_id,
                        expected_screen_id=current.screen_id,
                        admisi_detik=max(0.001, min(1.0, (lease_until - int(time.time() * 1000)) / 1000)),
                        batas_detik=4.5, guarded_publish=guarded, **route_deadlines)
                except Exception as exc:  # noqa: BLE001 — post-start ambiguity stays fail closed
                    call_state["error"] = exc
                finally:
                    command.result_wait_clock.reset(wait_token)
                    call_done.set()

            call_thread = threading.Thread(target=call, name="stardew-g1-" + origin, daemon=True)
            call_thread.start()
            while not W.call('poll', 98, call_done.wait, 0.02):
                live = self._water_bound_session(pin)
                raw = live.mentah
                lease = command.lease_state(raw)
                if not lease.owns(pin.owner, pin.generation) or lease.blocked:
                    renewal_failure = "owner_or_fault_changed"
                    break
                if live.inflight_request_id is not None:
                    if native_request_id is None:
                        native_request_id = live.inflight_request_id
                    elif live.inflight_request_id != native_request_id:
                        renewal_failure = "inflight_identity_changed"
                        break
                expiry = lease.expiry
                if not lease.owns(pin.owner, pin.generation, live=True):
                    # PM-1 a10 d3:J980-992: the mod paused ~12 s mid-warp, so the 1.5 s lease lapsed before native
                    # ever showed the request inflight. Native admission refuses an expired lease, so nothing can be
                    # in flight: a STOP here only latched a HALT (operator re-arm) and froze the run for 600 s.
                    unadmitted = native_request_id is None and live.inflight_request_id is None
                    renewal_failure = "lease_expired_unadmitted" if unadmitted else "lease_expired"
                    break
                if self._worker_stop.is_set() or self._action_cancel.is_set():
                    renewal_failure = "action_cancelled"
                    break
                if native_request_id is not None and expiry - int(time.time() * 1000) <= command.RENEW_HINT_MS:
                    try:
                        self._renew_water(pin, native_request_id)
                    except (OSError, ValueError, TypeError, command.KesalahanPerintah) as exc:
                        # fix10f: same as the world path: the result still decides; the lease lapses natively.
                        renewal_failure = str(exc)[:200]
                        break
            if renewal_failure is not None:
                self._recover_lease_fault(pin, renewal_failure)  # fix10f: never a persistent STOP
                W.call('poll', 99, call_done.wait, 5.0)
            if call_thread is not None:
                W.call('poll', 129, call_thread.join, timeout=0.1)
            result = call_state.get("result")
            if result is None and isinstance(call_state.get("error"), Exception):
                raise call_state["error"]
            if result is None:
                raise ValueError("result_unconfirmed")
        except (OSError, ValueError, TypeError, command.KesalahanPerintah) as exc:
            reason = str(exc)[:120] if isinstance(exc, ValueError) else type(exc).__name__
            return finish(LeasedRun(
                "uncertain" if submitted else "not_taken", reason, submitted=submitted))
        except Exception as exc:  # noqa: BLE001
            return finish(LeasedRun(
                "uncertain" if submitted else "not_taken", type(exc).__name__, submitted=submitted))
        lifecycle = {command.SELESAI: "completed", command.DITOLAK: "rejected", command.TERPUTUS: "interrupted",
                     command.TIDAK_DIAMBIL: "not_taken", command.GAGAL_LOKAL: "not_taken"}.get(result.status, "uncertain")
        env = result.mentah or {}
        # PM-1 fix9 (CONTROLLER_CONTRACT_v1 result): native's verdict IS the outcome. Python only checks that this
        # receipt answers this request and the inputs it reserved; it never re-derives the primitive's effect terms
        # (attempt 8 d2:J840: a v1 chest put carries no `mode`, so native TransferObserved was re-labelled uncertain).
        count = env.get("press_count")
        failed = [name for name, ok in (
            ("primitive", env.get("primitive") == primitive), ("input_args", env.get("input_args") == args),
            ("cleanup_confirmed", env.get("cleanup_confirmed") is True),
            ("request_id", env.get("request_id") == result.request_id), ("accepted", env.get("accepted") is True),
            ("session_id", env.get("session_id") == current.session_id),
            ("runtime_instance_id", env.get("runtime_instance_id") == ctx.runtime_instance_id),
            ("player_id", env.get("player_id") == current.player_id), ("screen_id", env.get("screen_id") == current.screen_id),
            ("context_generation", env.get("context_generation") == ctx.context_generation),
            ("location_id", env.get("location_id") == current.location_id),
            ("press_count", primitive not in ("select_item", "world_interact") or type(count) is int and (
                1 <= count <= publication_cost if args.get("kind") in command.EVENT_ROUTE_KINDS else count == 1)),
        ) if not ok]
        if lifecycle == "completed" and failed:
            self._journal({"ev": "action_completed_not_observed", "request_id": result.request_id,
                           "primitive": primitive, "failed_checks": failed})
            lifecycle = "uncertain"
        applied = lifecycle == "completed" and result.sukses is True and result.outcome == success
        readback = env.get("ui_pad_readback")
        if renewal_failure is not None and applied:
            # fix10f: native's verified receipt settled the request; the unconfirmed renewal was moot.
            self._journal({"ev": "renewal_fault_moot", "request_id": result.request_id, "cause": renewal_failure})
            renewal_failure = None
        run = LeasedRun(lifecycle, renewal_failure or result.alasan or result.outcome or lifecycle, result.request_id,
                        result.outcome, applied, False, submitted, True, env.get("ui_key_effect"),
                        env.get("result_receiver_token"), env.get("result_menu_type"),
                        readback=readback if isinstance(readback, str) and len(readback) <= (131072
                            if primitive == command.PRIMITIVE_UI_PRESS and args.get("context") == "other" else 128) else None,
                        economy=env.get("ui_pad_economy") if isinstance(env.get("ui_pad_economy"), dict) else None)
        run = finish(run)
        if run.applied and run.cleanup:
            # The newer ui.context readback is a FACT added to native's done (and the revision the next decision
            # waits for); a lagging or disagreeing sample is journaled, never a downgrade of the native verdict.
            agreed, revision = self._leased_readback(primitive, args, ctx, before_rev,
                                                     event_skip=expect_outcome == "EventSkipped", expected_request_id=result.request_id,
                                                     selection_revision=selection_revision, toolbar_before=toolbar_before,
                                                     exit_transition=(env.get("result_context_generation"), env.get("result_receiver_token"))
                                                     if primitive == "container_exit" else None)
            if not agreed:
                self._journal({"ev": "native_done_readback_lag", "request_id": result.request_id,
                               "primitive": primitive, "outcome": result.outcome})
            run = replace(run, verified=True, context_revision=revision)
            if agreed and primitive == "container_transfer" and transfer_before is not None:
                from arti_runtime.transfer_outcomes import publish
                after = self._container_view(self._water_context(ctx.player_id, container=True))
                publish(self, args, run.request_id, transfer_before, after)
            elif agreed and primitive == command.PRIMITIVE_UI_PRESS and press_container_before is not None:
                from arti_runtime.transfer_outcomes import publish_changes
                after = self._container_view(self._water_context(ctx.player_id, container=True))
                publish_changes(self, run.request_id, press_container_before, after)
            if agreed and item_view_before is not None:
                from arti_runtime.transfer_outcomes import publish_menu
                publish_menu(self, args, run, item_view_before, self._ui_pad_view(ctx.player_id))
        return run

    def _leased_finish(self, pin, call_thread, call_done, run: LeasedRun, *, call_state=None,
                       primitive=None, args=None, ctx=None, session=None, native_request_id=None) -> LeasedRun:
        if call_thread is not None and call_thread.is_alive():
            try:
                if pin is not None:
                    self._recover_lease_fault(pin, "sender_still_running")
            except Exception:  # noqa: BLE001
                pass
            W.call('poll', 95, call_done.wait, 5.0)
            W.call('poll', 128, call_thread.join, timeout=0.1)
        # STOP may deliver the original terminal only while joining the sender.
        # Release acknowledgement cannot erase its cleanup debt, or certify an
        # unresolved/mismatched submitted request. Never replay or promote its effect.
        native_cleanup = not run.submitted
        terminal = (call_state or {}).get("result")
        if pin is not None:
            pin.request_id = getattr(terminal, "request_id", None) or native_request_id
            self._last_input_request_id = pin.request_id
        if terminal is not None and ctx is not None and session is not None:
            env = terminal.mentah or {}
            # CompleteRejected(BindingMismatch) precedes input and omits most
            # pins on legacy leased routes. Reuse the exact no-run predicate;
            # release and a fresh SAME actor are still required. A newer menu
            # context may explain the refusal, but never verifies selection.
            from arti_stardew_runtime import _native_prearm_binding_refusal
            # PM-1 fix4 #1: a correlated typed refusal that sent no input (CommandBusy debt, admission) is clean too.
            if run.lifecycle == "rejected" and (env.get("reject_reason") == "BindingMismatch"
                    and _native_prearm_binding_refusal(terminal, env, session.session_id, native_request_id, ctx)
                    and ("primitive" not in env or env["primitive"] == primitive)
                    and ("input_args" not in env or env["input_args"] == args)
                    or (native_request_id is None or native_request_id == terminal.request_id)
                    and command.authenticated_no_input(env, terminal.request_id, getattr(pin, "generation", None))):
                fresh = self._ui_key_context(ctx.player_id)
                if fresh is not None and all(getattr(fresh, k, None) == getattr(ctx, k, None) for k in (
                        "runtime_instance_id", "command_session_id", "observation_session_id",
                        "player_id", "screen_id", "location")):
                    native_cleanup = True
                    run = replace(run, prearm_rejected=True, request_id=terminal.request_id, input_count=0)
            correlated = (terminal.request_id is not None and env.get("request_id") == terminal.request_id
                          and (native_request_id is None or terminal.request_id == native_request_id)
                          and env.get("primitive") == primitive and env.get("input_args") == args
                          and env.get("session_id") == session.session_id
                          and env.get("runtime_instance_id") == ctx.runtime_instance_id
                          and env.get("player_id") == session.player_id and env.get("screen_id") == session.screen_id
                          and env.get("context_generation") == ctx.context_generation
                          and env.get("location_id") == session.location_id)
            if correlated:
                native_cleanup = run.prearm_rejected or (env.get("cleanup_confirmed") is True
                                  and (env.get("input_settled") is True
                                       or "input_settled" not in env
                                       and primitive not in {"world_interact", "container_exit", "container_transfer"}))
                # Native pre-arm rejection has no run/input to settle. Require the
                # exact correlated refusal and a fresh unchanged TV receiver before
                # clearing cleanup debt; lease release still has to succeed below.
                if (primitive == command.PRIMITIVE_UI_PRESS and run.lifecycle == "rejected"
                        and env.get("status") == "rejected" and env.get("accepted") is False
                        and env.get("reject_reason") == "ArmRejected"
                        and str(env.get("arm_status") or "").startswith("PRECONDITION_UNAVAILABLE(")
                        and env.get("run_id") is None and env.get("press_count") in (None, 0)
                        and env.get("cleanup_confirmed") is not False
                        and env.get("input_settled") is not False):
                    fresh = self._ui_key_context(ctx.player_id)
                    view = self._ui_pad_view(ctx.player_id)
                    key = self._ui_key_view(ctx.player_id)
                    identity = ("runtime_instance_id", "command_session_id", "observation_session_id",
                                "player_id", "screen_id", "location", "context_generation")
                    if (fresh is not None and all(getattr(fresh, k, None) == getattr(ctx, k, None) for k in identity)
                            and view is not None and view.available and view.context == "question"
                            and view.receiver_token == args.get("receiver_token")
                            and view.target_token == args.get("target_token")
                            and key is not None and key.available and key.question_key == "TV"
                            and key.receiver_token == view.receiver_token):
                        native_cleanup = True
                        run = replace(run, prearm_rejected=True)
                count = env.get("press_count")
                run = replace(run, request_id=terminal.request_id, outcome=terminal.outcome,
                              input_count=count if type(count) is int and count >= 0 else None)
        cleanup = True
        if pin is not None:
            cleanup = self._cleanup_water(pin)
        with self._lock:
            self._water_action_pin = None
        return replace(run, cleanup=run.cleanup and native_cleanup and cleanup)

    @W.span('settle_readback', 92, runtime=True)
    def _leased_readback(self, primitive: str, args: dict, ctx, before_rev, *,
                         event_skip: bool = False, expected_request_id: str | None = None,
                         selection_revision=None, toolbar_before=None, exit_transition=None) -> tuple[bool, int | None]:
        """Strictly newer ui.context must agree with the mod verdict (bounded wait, read-only)."""
        deadline = game_now(self) + _READBACK_WAIT_S
        while True:
            rec = self._obs.get(ctx.player_id, "ui.context")
            if (rec is not None and rec.is_complete and rec.authoritative and before_rev is not None
                    and rec.domain_revision > before_rev and rec.session_id == ctx.observation_session_id):
                facts = rec.facts
                interactive = facts.get("interactive") if isinstance(facts.get("interactive"), dict) else None
                receiver = interactive.get("receiver_token") if interactive else None
                if primitive == "select_item":
                    inv = self._obs.get(ctx.player_id, "progress.inventory")
                    item = inv.facts.get(f"slot:{args['slot']}", {}) if inv is not None else {}
                    ok = (inv is not None and inv.is_complete and inv.authoritative
                          and not getattr(inv, "stale", False) and not getattr(inv, "requires_resync", False)
                          and 0 <= time.monotonic()-inv.received_monotonic <= 2
                          and inv.session_id == ctx.observation_session_id and inv.screen_id == ctx.screen_id
                          and selection_revision is not None and inv.domain_revision > selection_revision
                          and inv.facts.get("meta:inventory", {}).get("active_slot") == args["slot"]
                          and item.get("is_active") is True
                          and (item.get("qualified_item_id") or "") == args["qualified_item_id"]
                          and item.get("quality", 0) == args["quality"] and item.get("stack", 0) == args["stack"]
                          and facts.get("toolbar", {}).get("active_slot") == args["slot"])
                elif primitive == "world_interact":
                    from arti_stardew_event_control import effect_valid, receipt_target_id
                    receipt = facts.get("event_action_receipt")
                    target_id = receipt_target_id(args)
                    ok = (isinstance(receipt, dict) and receipt.get("verified") is True
                          and target_id is not None
                          and receipt.get("request_id") == expected_request_id
                          and receipt.get("target_token") == args["target_token"]
                          and receipt.get("source_fact_id") == args["source_fact_id"]
                          and receipt.get("outcome") == command.OUTCOME_SUKSES_INTERACT.get(args["kind"])
                          and effect_valid(args["kind"], receipt.get("before"), receipt.get("after"),
                                           target_id=target_id))
                    if ok and args["kind"] in command.EVENT_ROUTE_KINDS:
                        trace = receipt.get("trace")
                        ok = (isinstance(trace, list) and 1 <= len(trace) <= (512 if args["kind"] in command.APPROACH_ROUTE_KINDS else 40)
                              and all(isinstance(row, dict) and row.get("verified") is True and row.get("cleanup") is True
                                      for row in trace) and trace[-1].get("kind") == "A")
                elif primitive == "container_exit":
                    ok = (exit_transition is not None
                          and not getattr(rec, "stale", False) and not getattr(rec, "requires_resync", False)
                          and 0 <= time.monotonic() - rec.received_monotonic <= 2
                          and facts.get("context_generation") == exit_transition[0]
                          and receiver == exit_transition[1] and receiver != args["receiver_token"]
                          and not (isinstance(facts.get("container"), dict)
                                   and facts["container"].get("available") is True))
                elif primitive in (command.PRIMITIVE_UI_NAV, command.PRIMITIVE_UI_PRESS):
                    # WS-G1: the published binding moved (snap / state / tab / receiver) or the menu is gone.
                    pad = facts.get("ui_pad") if isinstance(facts.get("ui_pad"), dict) else None
                    ok = (receiver != args["receiver_token"] or pad is None
                          or pad.get("receiver_token") != args["receiver_token"]
                          or pad.get("target_token") != args["target_token"])
                elif primitive == "container_transfer":
                    # WS-G2: the published receiver block (bin / chest) moved past the readback token the args pinned.
                    key = "shipping_bin" if args.get("mode") in ("ship_one", "ship_stack") else "container"
                    block = facts.get(key) if isinstance(facts.get(key), dict) else None
                    ok = block is not None and block.get("readback_token") != args["readback_token"]
                elif primitive == command.PRIMITIVE_PAD_BUTTON and event_skip:
                    import arti_stardew_text_event as _te
                    ok = _te.event_skip_readback(facts)  # FIXB31: the event row is gone / Select no longer skips
                elif primitive == command.PRIMITIVE_PAD_BUTTON:
                    if args.get("button") in ("LB", "RB"):
                        from arti_stardew_generic_world import inventory, fingerprint
                        rows = inventory(self, ctx)
                        expected = ((toolbar_before[12:] + toolbar_before[:12]) if args["button"] == "RB"
                                    else (toolbar_before[-12:] + toolbar_before[:-12])) if toolbar_before else None
                        ok = expected is not None and bool(rows) and fingerprint(rows) == expected
                    else:
                        ok = interactive is not None and interactive.get("menu_type") == _PAD_BUTTON_MENU.get(args.get("button"))
                else:
                    block = facts.get("ui_key") if isinstance(facts.get("ui_key"), dict) else {}
                    token = next((k.get("target_token") for k in block.get("keys", []) or []
                                  if isinstance(k, dict) and k.get("key") == args["key"]), None)
                    ok = (receiver != args["receiver_token"] or block.get("receiver_token") != args["receiver_token"]
                          or token != args["target_token"] or block.get("ready") is not True)
                if ok:
                    return True, rec.domain_revision
            if game_now(self) >= deadline:
                return False, rec.domain_revision if rec is not None else None
            W.call('poll', 96, time.sleep, 0.05)

    @W.span('arming', 93, runtime=True)
    def _run_ui_key(self, args: dict, *, origin: str) -> LeasedRun:
        """One ui_key through the leased path. ``origin`` is ``model`` or ``day_start`` (journaled)."""
        command.validate_ui_key_args(args)
        binding, pid = self._binding_view()
        if args["key"] != "cancel" and origin.startswith(_DWELL_UI_KEY_ORIGINS):
            self._reading_dwell_wait(pid, kind=args["kind"], origin=origin)
        ctx = self._ui_key_context(pid)
        self._journal({"ev": "ui_key_request", "origin": origin, "receiver_token": args["receiver_token"],
                       "menu_type": args["menu_type"], "kind": args["kind"], "key": args["key"],
                       "option_index": args.get("option_index"), "profession_id": args.get("profession_id")})
        if ctx is None:
            run = LeasedRun("not_taken", "ui_context_unavailable")
        elif args["receiver_token"] in self._ui_key_uncertain:
            run = LeasedRun("not_taken", "ui_key_uncertain_latched")
        else:
            def revalidate():
                view = self._ui_key_view(pid)
                if (view is None or view.receiver_token != args["receiver_token"] or not view.available
                        or view.token_for(args["key"]) != args["target_token"]
                        or view.context_generation != ctx.context_generation):
                    raise ValueError("ui_key_target_changed")
            try:
                run = self._run_leased_once(command.PRIMITIVE_UI_KEY, args, ctx, origin="uikey-" + origin,
                                            revalidate=revalidate)
            except Exception as exc:  # noqa: BLE001 — never let a ui step escape as an exception
                run = LeasedRun("not_taken", type(exc).__name__)
        self._journal({"ev": "ui_key_result", "origin": origin, "request_id": run.request_id, "status": run.lifecycle,
                       "outcome": run.outcome, "effect": run.effect, "result_receiver_token": run.result_receiver_token,
                       "result_menu_type": run.result_menu_type, "verified": run.verified, "cleanup": run.cleanup,
                       "reason": run.reason})
        if run.uncertain:
            with self._lock:
                self._ui_key_uncertain[args["receiver_token"]] = run.reason
                self._journal({"ev": "ui_key_uncertain", "receiver_token": args["receiver_token"], "reason": run.reason})
                if run.hard_uncertain:
                    from arti_stardew_runtime import PAUSED_RECOVERY
                    self._status = PAUSED_RECOVERY
                    self._revoke_play()
                    self._pending_event_reason = None
        return run

    # ------------------------------------------------------------------ model decision path
    def _ui_key_model_reason(self, request, args) -> str | None:
        offered = getattr(request.snapshot, "ui_key_target", None)
        if offered is None:
            return "ui_key_not_offered"
        if self._day_start_active:
            return "day_start_active"
        if (not isinstance(args, dict) or args.get("key") not in MODEL_UI_KEY_KEYS
                or args.get("kind") not in MODEL_UI_KEY_KINDS
                or any(args.get(k) != offered[k] for k in ("receiver_token", "menu_type", "kind"))
                or {"key": args.get("key"), "target_token": args.get("target_token")} not in offered["keys"]):
            return "ui_key_target_not_offered"
        if args["receiver_token"] in self._ui_key_uncertain:
            return "ui_key_uncertain_latched"
        try:
            command.validate_ui_key_args(args)
        except command.KesalahanPerintah:
            return "ui_key_args_invalid"
        if args["key"] == "answer" and not any(o["index"] == args["option_index"] for o in offered["options"]):
            return "ui_key_option_not_offered"
        return None

    def _execute_ui_key_decision(self, decision, request, goal):
        from arti_stardew_runtime import ActionEvidenceView, OwnerTurnResult
        args = dict(decision.choice.args)
        with self._lock:
            reason = self._ui_key_model_reason(request, args)
        if reason:
            run = LeasedRun("not_taken", reason)
        else:
            run = self._run_ui_key(args, origin="model")
        evidence = ActionEvidenceView(
            run.lifecycle, run.reason, run.request_id, run.outcome,
            before={"ui_key": {k: args.get(k) for k in ("receiver_token", "menu_type", "kind", "key")}},
            after=({"ui_key": {"effect": run.effect, "result_receiver_token": run.result_receiver_token,
                               "result_menu_type": run.result_menu_type}} if run.request_id else None),
            world_effect_status=("observed_ui_key" if run.verified else "unchanged" if run.lifecycle == "completed"
                                 and not run.applied else "zero_effect" if not run.submitted else "uncertain"),
            cleanup_confirmed=run.cleanup)
        with self._lock:
            superseded = (not self._request_current(request) or self._active is not goal or self._action_cancel.is_set())
            if not superseded and goal is not None:
                goal.last_action = evidence
            if self._inflight_generation == request.generation_id:
                self._inflight_generation = None
            self._action_busy = False
            self._journal({"ev": "action_result", "goal_id": request.goal_id, "action": asdict(evidence)})
            if not superseded:
                self._schedule_continuation(decision, request)
        if not superseded and run.verified and run.cleanup:
            self._resume_after_pause(reason="action_settled")
        return OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=not superseded and run.verified,
                               discarded=superseded, reason="superseded" if superseded else run.reason,
                               action_evidence=evidence)

    def _ui_key_precheck_decision(self, request, args) -> None:
        """Continuation precheck branch (nothing written): the offered ui_key target is still current."""
        reason = self._ui_key_model_reason(request, args)
        if reason:
            raise ValueError(reason)
        binding, pid = self._binding_view()
        view = self._ui_key_view(pid)
        if (view is None or not view.available or view.receiver_token != args["receiver_token"]
                or view.token_for(args["key"]) != args["target_token"]):
            raise ValueError("ui_key_target_changed")

    # ------------------------------------------------------------------ day-start rule
    def _day_start_preconditions(self, *, explicit: bool = False) -> tuple[bool, int | None]:
        from arti_stardew_runtime import PAUSED_RECOVERY, READY
        # The trial runner's explicit pre-phase runs before the owner turn re-arms READY: a fresh runtime
        # starts PAUSED_RECOVERY only because the runner's own reservation line is in the journal. Any real
        # fault (an input already published, an uncertain ui_key, an active goal) keeps it refused.
        fresh_startup = (explicit and self._status == PAUSED_RECOVERY and self._active is None
                         and self._physical_action_publications == self._day_start_submitted
                         and not self._ui_key_uncertain)
        if (not self._day_start_policy.enabled or (self._status != READY and not fresh_startup)
                or self._worker_stop.is_set() or self._owner_play_stopped or self._action_busy):
            return False, None
        limit = self._action_session_limit
        if limit is not None and self._physical_action_publications >= limit:
            return False, None
        binding, pid = self._binding_view()
        if binding.status != "bound" or pid is None or binding.is_main_player is not False or binding.is_split_screen is not False:
            return False, None
        return True, pid

    def _day_start_track_date(self, pid: int, now: float) -> tuple | None:
        date = self._date_tuple(self._state.latest(pid)) if pid is not None else None
        state = self._day_start_state
        if date is not None and date != self._day_start_last_date:
            if self._day_start_last_date is not None:
                if not state.active:
                    state.reset()
                state.date_changed_at = now
                self._day_start_agent_base = getattr(self, "_agent_publications", 0)
            self._day_start_last_date = date
        return date

    def _day_start_agent_acted(self) -> bool:
        """PART_C2: ARTI published its own input since the observed date change (the wake-up window is over)."""
        base = getattr(self, "_day_start_agent_base", None)
        return base is not None and getattr(self, "_agent_publications", 0) > base

    def _day_start_tick(self, *, explicit: bool = False) -> DayStartStep | None:
        """One planner step (+ at most one leased ui_key). Serialized; returns None when not applicable."""
        state = self._day_start_state
        if (state.active and state.host_wait_deadline is not None
                and time.monotonic() > state.host_wait_deadline):
            with self._lock:
                state.blocked = "host_wait_wall_bound"
                self._journal(dict(ev="day_start_blocked", reason=state.blocked, actions=state.actions))
            return DayStartStep("blocked", state.blocked)
        if getattr(self, "_pause_boundary", lambda: False)():
            return None
        if not getattr(self, "_day_start_policy", None) or not self._day_start_policy.enabled:
            return None
        if not self._day_start_lock.acquire(blocking=False):
            return None
        try:
            now = game_now(self)
            with self._lock:
                ok, pid = self._day_start_preconditions(explicit=explicit)
                state = self._day_start_state
                if (state.active and state.started_at is not None
                        and now - state.started_at > state.wall_limit(self._day_start_policy)):
                    # The wall bound holds even while preconditions fail (binding lost in save/warp,
                    # status not READY, limit reached, owner stop): never a silently eternal episode.
                    state.blocked = "wall_bound"
                    self._day_start_last_terminal = (state.episode_key, state.blocked)
                    self._journal({"ev": "day_start_blocked", "reason": "wall_bound", "menu_type": None,
                                   "kind": None, "actions": state.actions,
                                   "elapsed_s": round(now - state.started_at, 2), "preconditions": ok})
                    return DayStartStep("blocked", "wall_bound")
                if not ok:
                    if state.active:
                        self._day_start_wait_journal("preconditions_unmet", None, now)
                    return None
                date = self._day_start_track_date(pid, now)
                rec, age = self._ui_key_record(pid)
                facts = rec.facts if rec is not None else None
                view = self._ui_key_view(pid)
                was_active = state.active
                ctx = self._ui_key_context(pid)
                identity = self._store_identity(ctx) if ctx is not None else None
                if state.active and state.identity is not None and identity is not None and state.identity != identity:
                    state.blocked = "day_start_identity_changed"
                    self._journal(dict(ev="day_start_blocked", reason=state.blocked, actions=state.actions))
                    return DayStartStep("blocked", state.blocked)
                if facts and isinstance(facts.get("ui_pad"), dict) and facts["ui_pad"].get("generic") is True:
                    # The generic UI controller chooses every menu input; the day-start planner still
                    # owns the no-menu host synchronization barrier. Do not auto-select a profession.
                    state.reset()
                    return DayStartStep("idle", "generic_ui_handoff", menu_type=facts["ui_pad"].get("menu_type"))
                plan_revision = rec.domain_revision if rec is not None else None
                step = plan_day_start_step(facts, view, self._day_start_policy, state, now=now,
                                           context_revision=rec.domain_revision if rec is not None else None, date=date,
                                           agent_acted=self._day_start_agent_acted())
                if step.kind == "act":
                    self._action_busy = True  # no model action may start while the rule owns this menu step
                if state.active and state.identity is None and identity is not None:
                    state.identity = identity
                if state.active and not was_active:
                    day = (facts or {}).get("day_transition") or {}
                    self._journal({"ev": "day_start_detected", "date": list(date) if date else None,
                                   "menu_type": step.menu_type, "showing_end_of_night_stuff": day.get("showing_end_of_night_stuff"),
                                   "pending_menus": day.get("end_of_night_menus_pending")})
                if step.kind == "done" and was_active:
                    # Same lock hold as the plan that just ended the episode (RLock; the resume takes it too):
                    # otherwise the trial runner can observe "day start inactive, nothing pending" in the gap
                    # and HOLD continuation_not_ready (integration 2026-10-07, WS3b bed trial, ~1 in 10 under load).
                    self._journal({"ev": "day_start_cleared", "actions": state.actions,
                                   "elapsed_s": round(now - (state.started_at or now), 2),
                                   "date": list(date) if date else None, "reason": step.reason, "menus": state.menus,
                                   "host_waited": state.host_sync_pending})
                    self._resume_after_pause(reason="day_start_cleared")
            if step.kind == "act":
                self._journal({"ev": "day_start_action", "step": state.actions + 1, "menu_type": step.menu_type,
                               "kind": step.ui_kind, "key": step.ui_key_args["key"],
                               "option_index": step.ui_key_args.get("option_index")})
                if step.profession is not None:
                    self._journal({"ev": "profession_policy_applied", "skill": step.profession["skill"],
                                   "level": step.profession["level"], "options": step.profession["options"],
                                   "chosen_id": step.profession["chosen"], "source": "stardew_profession_policy"})
                try:
                    self._day_start_publishing = True  # its own publications never close the wake-up window
                    run = self._run_ui_key(dict(step.ui_key_args), origin="day_start")
                finally:
                    with self._lock:
                        self._day_start_publishing = False
                        self._action_busy = False
                with self._lock:
                    if run.submitted:
                        self._day_start_submitted += 1
                    # Spacing: the next step needs a ui.context strictly newer than the one this act was planned on.
                    note_result(state, step, outcome=run.outcome if run.applied else (run.outcome or run.lifecycle),
                                uncertain=run.uncertain, context_revision=plan_revision, submitted=run.submitted,
                                max_uncertain=self._day_start_policy.max_uncertain,
                                verified=run.verified, effect=run.effect)
                    if state.host_sync_pending and state.host_wait_deadline is None:
                        state.host_wait_deadline = time.monotonic() + self._day_start_policy.host_wait_s
                    if state.blocked is not None:
                        self._day_start_last_terminal = (state.episode_key, state.blocked)
                        self._journal({"ev": "day_start_blocked", "reason": state.blocked, "menu_type": step.menu_type,
                                       "kind": step.ui_kind, "actions": state.actions,
                                       "elapsed_s": round(now - (state.started_at or now), 2)})
            elif step.kind == "wait":
                with self._lock:
                    if state.host_sync_pending and state.host_wait_deadline is None:  # WP P1: native shipping OK
                        state.host_wait_deadline = time.monotonic() + self._day_start_policy.host_wait_s
                self._day_start_wait_journal(step.reason, step.menu_type, now)
            elif step.kind == "blocked":
                key = (state.episode_key, step.reason)
                if key != self._day_start_last_terminal:
                    self._day_start_last_terminal = key
                    self._journal({"ev": "day_start_blocked", "reason": step.reason, "menu_type": step.menu_type,
                                   "kind": step.ui_kind, "actions": state.actions,
                                   "elapsed_s": round(now - (state.started_at or now), 2)})
            return step
        finally:
            self._day_start_lock.release()

    def _day_start_wait_journal(self, reason: str, menu_type: str | None, now: float) -> None:
        state = self._day_start_state
        last = state.last_wait
        if last is None or last[0] != reason or now - last[1] >= _DAY_START_WAIT_JOURNAL_S:
            state.last_wait = (reason, now)
            self._journal({"ev": "day_start_waiting", "menu_type": menu_type, "reason": reason,
                           "waited_s": round(now - (state.started_at or now), 2),
                           "host_wait": state.host_sync_pending, "phase": "day_start",
                           "readback_reason": "ui_context_unavailable"
                                              if reason == "waiting_for_host_or_players" else reason,
                           "message": "Owner, finish HOST end-of-night screens; waiting for all players/save sync."
                                      if state.host_sync_pending else None})

    def process_day_start(self) -> int:
        """Worker hook (before continuations). Returns 1 when an act was attempted."""
        try:
            step = self._day_start_tick()
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            self._journal({"ev": "day_start_error", "error": type(exc).__name__})
            return 0
        return 1 if step is not None and step.kind == "act" else 0

    def day_start_pending(self) -> bool:
        """Read-only: a day-start episode is active, or a day-start menu is up now."""
        if not getattr(self, "_day_start_policy", None) or not self._day_start_policy.enabled:
            return False
        with self._lock:
            if self._day_start_state.active:
                return True
            binding, pid = self._binding_view()
            rec, _ = self._ui_key_record(pid)
            if rec is None:
                return False
            facts = rec.facts
            interactive = facts.get("interactive") if isinstance(facts.get("interactive"), dict) else {}
            day = facts.get("day_transition") if isinstance(facts.get("day_transition"), dict) else {}
            return (interactive.get("menu_type") in ("LevelUpMenu", "ShippingMenu")
                    or (day.get("showing_end_of_night_stuff") is True and bool(interactive)))

    def run_day_start_handler(self, *, deadline_s: float | None = None, poll_s: float = 0.1) -> dict:
        """Trial pre-phase: drive the rule until done/blocked/deadline. No provider calls."""
        started = game_now(self)
        if deadline_s is None:
            deadline_s = self._day_start_policy.wall_s + self._day_start_policy.host_wait_s
        status, reason = "deadline", "deadline"
        idle_since = None
        while game_now(self) - started < deadline_s:
            if self._worker_stop.is_set():
                status, reason = "stopped", "worker_stopped"
                break
            step = self._day_start_tick(explicit=True)
            state = self._day_start_state
            if state.blocked is not None:
                status, reason = "blocked", state.blocked
                break
            if state.finished is not None or (step is not None and step.kind in ("done", "idle") and not state.active):
                status, reason = "done", state.finished or (step.reason if step else "idle")
                break
            if step is None and not state.active:
                idle_since = idle_since if idle_since is not None else game_now(self)
                if game_now(self) - idle_since >= 2.0:
                    status, reason = "unavailable", "preconditions_unmet"
                    break
            else:
                idle_since = None
            W.call('poll', 97, time.sleep, poll_s)
        state = self._day_start_state
        return {"actions": state.actions, "elapsed_s": round(game_now(self) - started, 2), "status": status,
                "reason": reason, "menus": list(state.menus)}

    # ------------------------------------------------------------------ B8 store close
    def _store_close_input_available(self, reserve: int = 1) -> bool:
        """True when the action/trial envelope still has ``reserve`` inputs (D31/D33: keep one for the close)."""
        limit = self._action_session_limit
        return limit is None or limit - self._physical_action_publications >= reserve

    def _store_close_target(self, goal, ctx, *, failsafe: bool = False) -> dict | None:
        """Published ordinary chest close of THIS goal's store chest.

        Strict (default): the verified paired store (transferred >= requested, tokens match the last paired
        transfer). Fail-safe: any open receiver of this goal's own store chest (D33: never leave it open).
        """
        view = self._container_view(ctx)
        progress = goal.transfer_progress if goal is not None else None
        if view is None or view.get("cursor") is not None or view.get("ready_to_close") is not True:
            return None
        if failsafe:
            if goal is None or goal.store_container != (view.get("container_token"), view.get("source_fact_id")):
                return None
        elif (progress is None or progress.get("status") != "PAIRED_TRANSFER"
                or type(progress.get("transferred")) is not int or type(progress.get("requested")) is not int
                or progress["transferred"] < progress["requested"]
                or any(view.get(k) != progress.get(k) for k in ("receiver_token", "container_token", "source_fact_id",
                                                                 "readback_token"))):
            return None
        rec = self._obs.get(ctx.player_id, "ui.context")
        if rec is None or rec.facts.get("capabilities", {}).get("container_exit", {}).get("available") is not True:
            return None
        close = view.get("close_target") or {}
        try:
            if close.get("kind") != "ordinary_chest_close":
                return None
            x, y, w, h = close["bounds"]
            vw, vh = close["viewport"]
            if (any(type(n) is not int for n in (x, y, w, h, vw, vh)) or min(x, y) < 0 or min(w, h, vw, vh) <= 0
                    or max(vw, vh) > 16384 or x + w > vw or y + h > vh):
                return None
            args = {k: view[k] for k in ("receiver_token", "container_token", "source_fact_id", "readback_token")}
            args["target_token"] = close["target_token"]
            command.validate_container_exit_args(args)
            return args
        except (KeyError, TypeError, ValueError, command.KesalahanPerintah):
            return None

    def _store_close_diagnostic(self, pid, args, ctx, binding) -> dict:
        """Why no close was offered (live B1W1b): the mod's own reason and each Python term. Names only."""
        rec = self._obs.get(pid, "ui.context") if pid is not None else None
        facts = rec.facts if rec is not None else {}
        container = facts.get("container") if isinstance(facts.get("container"), Mapping) else {}
        capability = (facts.get("capabilities") or {}).get("container_exit") or {}
        reason = container.get("close_unavailable_reason") or container.get("reason")
        return {"mod_close_reason": reason if isinstance(reason, str) and len(reason) <= 96 else None,
                "close_target_published": isinstance(container.get("close_target"), Mapping),
                "capability_available": capability.get("available") is True,
                "target_matches_store": args is not None,
                "session_ready": bool(ctx is not None and self._water_snapshot_available(binding, ctx, container_exit=True))}

    def _ui_shows_no_menu(self, ctx) -> bool:
        """Current authoritative ui.context of the same observation session shows no menu and no open container."""
        rec = self._obs.get(ctx.player_id, "ui.context")
        if (rec is None or not rec.is_complete or not rec.authoritative or bool(getattr(rec, "stale", False))
                or rec.session_id != ctx.observation_session_id or rec.screen_id != ctx.screen_id
                or rec.player_id != ctx.player_id
                or getattr(rec, "requires_resync", False)
                or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
            return False
        facts = rec.facts
        container = facts.get("container")
        motor = self._state.latest(ctx.player_id)
        return (motor is not None and not self._state.is_stale(ctx.player_id)
                and getattr(motor, "active_menu", None) is None
                and motor.is_player_free is True and motor.can_player_move is True
                and facts.get("mode") == "gameplay" and not isinstance(facts.get("interactive"), Mapping)
                and not (isinstance(container, Mapping) and container.get("available") is True))

    # Chain stops that leave the store in doubt: never add another input then (fail closed, HOLD instead).
    _CHAIN_STOPS_WITHOUT_CLOSE = frozenset({"not_completed", "cleanup_unconfirmed", "superseded", "not_applied",
                                            "effect_not_observed", "exited"})

    def _store_chain_failsafe_close(self, goal, stop: str, *, last_published=None, last_attempt=None,
                                    refused_target=None, request=None) -> dict | None:
        """Close this chain's own chest on every settled, safe stop, including a full deposit.

        Cleanup uses the published ordinary close, independent of the multi-episode exit offer.
        The paired ledger is captured before closing and revalidated against the released read.
        """
        # A publication-context refusal is discarded, but did not supersede the owner.
        # The prior open/transfer still owns a chest lock. Settle that debt using a
        # fresh ordinary close, never by replaying the refused transfer or its ticket.
        previous = getattr(last_published, "action_evidence", None)
        refused = getattr(last_attempt, "action_evidence", None)
        unpublished = (last_attempt is not last_published and refused is not None
                       and refused.lifecycle == "not_taken" and refused.request_id is None
                       and refused.world_effect_status == "zero_effect" and refused.cleanup_confirmed
                       and previous is not None and previous.lifecycle == "completed"
                       and previous.cleanup_confirmed and previous.world_effect_status in {
                           "observed_container_open", "observed_paired_transfer"})
        if unpublished and stop in {"superseded", "not_completed"}:
            with self._lock:
                safe_owner = (goal is not None and self._active is goal and not self._action_cancel.is_set()
                              and not self._worker_stop.is_set() and not self._owner_voice_busy
                              and not getattr(self, "_owner_play_stopped", False)
                              and request is not None and self._request_current(request)
                              and self._logical_request is request)
                _, pid = self._binding_view()
                ctx = self._water_context(pid, container=True) if safe_owner else None
                view = self._container_view(ctx) if ctx is not None else None
                inventory = self._inventory_contents(ctx) if ctx is not None else None
                previous_inventory = getattr(getattr(request, "continuation", None), "store_inventory_basis", None)
                inventory_stable = (inventory is not None and (previous_inventory is None
                    or inventory["revision"] >= previous_inventory["revision"]
                    and inventory["capture_seq"] >= previous_inventory["capture_seq"]
                    and {k: v for k, v in inventory.items() if k not in ("revision", "capture_seq")}
                    == {k: v for k, v in previous_inventory.items() if k not in ("revision", "capture_seq")}))
                same_receiver = (view is not None and inventory_stable
                                 and isinstance(refused_target, Mapping)
                                 and all(view.get(k) == refused_target.get(k) for k in (
                                     "receiver_token", "container_token", "source_fact_id")))
            if same_receiver:
                stop = "unpublished_followup"
        if (goal is None or not goal.gather_store_required or stop in self._CHAIN_STOPS_WITHOUT_CLOSE
                or str(stop).startswith("exception:")):
            return
        return self._store_close(goal, origin="chain_end_failsafe", failsafe=True, chain_stop=stop)

    def _store_close(self, goal, *, origin: str, failsafe: bool, chain_stop: str | None = None,
                     post_finish: bool = False, receiver_scope: tuple | None = None) -> dict | None:
        """One leased container_exit of this goal's store chest; returns the journaled record or None."""
        from arti_stardew_runtime import READY
        with self._lock:
            if (goal.pending_container_exit is not None or self._active is not goal or self._status != READY
                    or self._worker_stop.is_set() or self._gather_mutation_unresolved or goal.gather_uncertain):
                return None
            binding, pid = self._binding_view()
            ctx = self._water_context(pid, container=True)
            args = self._store_close_target(goal, ctx, failsafe=failsafe) if ctx is not None else None
            if args is not None and receiver_scope is not None and (
                    args["receiver_token"], self._store_identity(ctx)) != receiver_scope:
                return None  # a handoff cannot close a different opening of the same chest
            available = args is not None and self._water_snapshot_available(binding, ctx, container_exit=True)
            if not available:
                if ctx is not None or not failsafe:
                    self._journal({"ev": "store_close_rule", "status": "not_offered", "origin": origin,
                                   "container_open": ctx is not None, "chain_stop": chain_stop,
                                   **self._store_close_diagnostic(pid, args, ctx, binding)})
                return None
            inventory = self._inventory_contents(ctx)
            paired_observed = self._paired_store_observed(goal, allow_closed=False)
            paired_proof = (dict(transfer=copy.deepcopy(goal.transfer_progress), observed=paired_observed,
                                 identity=self._store_identity(ctx), inventory=copy.deepcopy(inventory))
                            if paired_observed is not None and inventory is not None else None)
            was_busy = self._action_busy
            self._action_busy = True
            finished_at = getattr(goal, "store_finish_at", None)
            if (post_finish and finished_at is not None and self._action_cancel.is_set()
                    and getattr(self, "_action_cancel_at", None) is not None and self._action_cancel_at < finished_at
                    and not getattr(self, "_owner_play_stopped", False) and not self._owner_voice_busy
                    and time.monotonic() - getattr(self, "_owner_voice_at", -1e9) > self.OWNER_VOICE_CANCEL_HOLD_S):
                # FIXB22 D-B19-2 (live V2B19ACC1b: `store_close_rule not_taken action_cancelled` right after the
                # store_rule finish; the chest stayed open): a cancel set BEFORE the goal finished belonged to an action
                # whose purpose ended with the finish; it never refuses the post-finish close (an owner stop still does;
                # a cancel between store steps of an unfinished goal still stops the chain, unchanged)
                self._action_cancel.clear()
                self._journal({"ev": "store_close_rule", "status": "stale_cancel_cleared", "origin": origin})
        close_proof: dict = {}
        try:
            def revalidate():
                if self._store_close_target(goal, self._water_context(pid, container=True), failsafe=failsafe) != args:
                    raise ValueError("close_target_changed")
            try:
                run = self._run_leased_once("container_exit", dict(args), ctx, origin="store-close",
                                            revalidate=revalidate)
            except Exception as exc:  # noqa: BLE001
                run = LeasedRun("not_taken", type(exc).__name__)
            # Live B3W1b (D12): closed = the mod's verified exit + the current ui.context showing no menu
            # (newer readback or not) + no observed inventory change; a newer inventory capture is recorded,
            # not required.
            inventory_check = "unavailable"
            if inventory is not None and run.applied:
                after = self._inventory_contents(ctx)
                inventory_check = ("unavailable" if after is None
                                   else "mismatch" if (after["contents"] != inventory["contents"]
                                                       or after["active_slot"] != inventory["active_slot"])
                                   else "match_newer" if after["capture_seq"] > inventory["capture_seq"]
                                   else "match_not_newer")
            ui_no_menu = self._ui_shows_no_menu(ctx)
            close_proof = {"readback_newer": run.verified, "ui_no_menu": ui_no_menu, "inventory_check": inventory_check}
            run = replace(run, verified=bool(run.verified and run.applied and run.cleanup and run.lifecycle == "completed"
                                             and ui_no_menu and inventory_check in {"match_newer", "match_not_newer"}))
        finally:
            with self._lock:
                self._action_busy = was_busy
        status = "closed" if run.verified else "not_taken" if not run.submitted else "uncertain"
        with self._lock:
            record = {"request_id": run.request_id, "status": status, "outcome": run.outcome, "origin": origin,
                      "reason": run.reason}
            if run.verified and paired_proof is not None:
                record["paired_proof"] = paired_proof
            goal.store_close = record
            if run.submitted and not run.verified:
                self._gather_mutation_unresolved = True  # an unverified close is an unresolved input
            if run.hard_uncertain:
                from arti_stardew_runtime import PAUSED_RECOVERY
                self._status = PAUSED_RECOVERY
                self._revoke_play()
            self._journal({"ev": "store_close_rule", **record, "reason": run.reason, "verified": run.verified,
                           "cleanup": run.cleanup, "chain_stop": chain_stop, **(close_proof if run.submitted else {})})
        return record

    STORE_CLOSE_RETRIES_MAX = 2
    OWNER_VOICE_CANCEL_HOLD_S = 2.0  # FIXB22 review: a "[recorded remark omitted]" transcript may follow a voice-ingress cancel

    def store_close_retry_due(self) -> bool:
        """Caller holds the lock (runner idle check): a terminal store goal whose post-finish close was not taken (nothing
        submitted) still has a bounded retry."""
        try:
            goal = self._active
            rec = getattr(goal, "store_close", None) if goal is not None else None
            return (goal is not None and goal.gather_store_required and goal.progress_marker == "terminal"
                    and isinstance(rec, Mapping) and rec.get("status") == "not_taken" and rec.get("request_id") is None
                    and rec.get("reason") == "action_cancelled"
                    and goal.store_close_retries < self.STORE_CLOSE_RETRIES_MAX + 1 and self._store_close_enabled)
        except Exception:  # noqa: BLE001 - an idle check never raises
            return False

    def process_store_close_retry(self) -> int:
        """Worker hook (FIXB22 D-B19-2): after the finish settled, retry a post-finish store close that was not taken
        (<= STORE_CLOSE_RETRIES_MAX; never after a submitted / uncertain close)."""
        try:
            with self._lock:
                if (not self.store_close_retry_due() or self._action_busy or self._inflight_generation is not None
                        or self._results_pending or self._worker_stop.is_set()):
                    return 0
                goal = self._active
                goal.store_close_retries += 1
                origin = str(goal.store_close.get("origin") or "store_close_rule").removesuffix("_retry")
                n = goal.store_close_retries
                # FIXB30 D-B27-2: <= STORE_CLOSE_RETRIES_MAX strict retries, then one fail-safe container_exit of this
                # goal's own chest (the D33 close) so a verified store never leaves the menu open
                fallback = n > self.STORE_CLOSE_RETRIES_MAX
            record = self._store_close(goal, origin=origin + ("_fallback" if fallback else "_retry"),
                                       failsafe=fallback or origin != "store_close_rule", post_finish=True)
            self._journal({"ev": "store_close_retry", "goal_id": goal.goal_id, "n": n,
                           "status": (record or {}).get("status")})
            return 1
        except Exception as exc:  # noqa: BLE001 - the worker loop must survive
            self._journal({"ev": "store_close_retry_error", "error": type(exc).__name__})
            return 0

    def _close_store_after_finish(self, result, goal):
        """B8 rule: after a GOAL finish of a store goal, close this goal's still-open chest once.

        Achieved: the verified paired store's close (strict target). Partial (D33: soak3-t3 ended partial and
        left the menu open): the fail-safe close of this goal's own chest. Uncertain finishes add no input.
        The multi-episode path (pending_container_exit) keeps offering container_exit to the model.
        """
        fe = getattr(result, "finish_evidence", None)
        if (not self._store_close_enabled or fe is None or fe.status not in ("achieved", "partial")
                or result.reason != "finish" or result.discarded or goal is None or not goal.gather_store_required):
            return result
        with self._lock:
            if self._action_busy or (self._paired_store_observed(goal) or {}).get("container_closed"):
                return result
        achieved = fe.status == "achieved"
        with self._lock:
            goal.store_finish_at = time.monotonic()  # FIXB22 D-B19-2: a cancel set before this is stale for the close
        record = self._store_close(goal, origin="store_close_rule" if achieved else "finish_failsafe",
                                   failsafe=not achieved, post_finish=True)
        if record is not None and record["status"] == "closed" and achieved:
            with self._lock:
                if goal.episode_outcomes:
                    last = dict(goal.episode_outcomes[-1])
                    last["exit"] = dict(request_id=record["request_id"], status="observed_container_exit",
                                        origin="store_close_rule")
                    goal.episode_outcomes = goal.episode_outcomes[:-1] + (last,)
        return result

    # ------------------------------------------------------------------ store rule (deterministic finish)
    @staticmethod
    def _store_identity(ctx):
        return tuple(getattr(ctx, k, None) for k in ("runtime_instance_id", "command_session_id",
            "observation_session_id", "player_id", "screen_id", "location"))

    def _paired_store_observed(self, goal, *, allow_closed=True):
        """A paired effect is a fact even below the requested amount; closure cannot erase it.

        Fresh open-container tokens/counts prove it directly. After our own verified close,
        the exact ledger, native identity and unchanged released inventory must still agree.
        """
        if (goal is None or not goal.gather_store_required or goal.gather_uncertain
                or self._gather_mutation_unresolved):
            return None
        progress, action = goal.transfer_progress, goal.last_action
        if (progress is None or progress.get("status") != "PAIRED_TRANSFER"
                or type(progress.get("transferred")) is not int or progress["transferred"] <= 0
                or type(progress.get("requested")) is not int or progress["requested"] <= 0
                or action is None or not action.cleanup_confirmed
                or action.lifecycle in {"uncertain", "interrupted"}
                or action.world_effect_status in {"uncertain", "possible_mutation", "unverified"}):
            return None
        binding, pid = self._binding_view()
        ctx = self._water_context(pid, container=True) if pid is not None else None
        view = self._container_view(ctx) if ctx is not None else None
        if view is None:
            proof = (goal.store_close or {}).get("paired_proof") if allow_closed else None
            ctx = self._water_context(pid) if pid is not None else None
            inventory = self._inventory_contents(ctx)
            motor = self._state.latest(pid) if pid is not None else None
            if (not proof or (goal.store_close or {}).get("status") != "closed"
                    or progress != proof["transfer"] or ctx is None
                    or self._store_identity(ctx) != proof["identity"]
                    or not self._water_snapshot_available(binding, ctx, observation_only=True)
                    or not self._ui_shows_no_menu(ctx) or motor is None
                    or self._state.is_stale(pid)
                    or motor.is_player_free is not True or motor.can_player_move is not True
                    or inventory is None or inventory["contents"] != proof["inventory"]["contents"]
                    or inventory["active_slot"] != proof["inventory"]["active_slot"]):
                return None
            return dict(proof["observed"], container_closed=True)
        if (self._store_identity(ctx) != self._store_identity(goal.gather_pin)
                or not self._water_snapshot_available(binding, ctx, observation_only=True)
                or progress.get("context_generation") != ctx.context_generation
                or (view["container_token"], view["source_fact_id"]) != goal.store_container
                or any(view[k] != progress.get(k) for k in ("receiver_token", "readback_token"))
                or view.get("cursor") is not None):
            return None
        qid, quality = progress["qualified_item_id"], progress["quality"]
        actor_count = self._container_count(view, "actor_slots", qid, quality)
        container_count = self._container_count(view, "container_slots", qid, quality)
        gain = container_count - progress["container_baseline"]
        decrease = progress.get("actor_baseline", actor_count + progress["transferred"]) - actor_count
        if not (gain == decrease == progress["transferred"]
                and container_count == progress["container_count"] and actor_count == progress["actor_count"]):
            return None
        one_tree = bool(goal.episode_plan) and goal.episode_plan[goal.episode_index].mode == "one_tree"
        deposit = goal.gather_progress.deposit_quantity if one_tree else progress["requested"]
        motor = self._state.latest(pid)
        return dict(origin="store_rule", transferred=progress["transferred"], requested=progress["requested"],
            chest_gain=gain, bag_decrease=decrease, deposit=deposit, cursor_empty=True,
            location=motor.location if motor else None, tile=list(motor.tile) if motor and motor.tile else None)

    def _store_transfer_receipt(self, goal, *, progress=None):
        """ADR 112: the paired native transfer already earned, independent of later menu tokens.

        This never admits input: _paired_store_observed keeps its fresh-state checks
        for that purpose. Only _execute_leased_action writes this goal's paired ledger.
        """
        if (goal is None or not getattr(goal, "gather_store_required", False)
                or getattr(goal, "gather_uncertain", False) or self._gather_mutation_unresolved):
            return None
        p = (getattr(goal, "transfer_progress", None) if progress is None else progress) or {}
        counts = tuple(p.get(k) for k in ("transferred", "requested", "actor_baseline", "actor_count",
                                         "container_baseline", "container_count"))
        requests = p.get("request_ids") or ()
        if (p.get("status") != "PAIRED_TRANSFER" or any(type(n) is not int or n < 0 for n in counts)
                or not isinstance(p.get("qualified_item_id"), str) or not p["qualified_item_id"]
                or p.get("quality") not in (0, 1, 2, 4)
                or counts[0] <= 0 or counts[1] <= 0 or not isinstance(requests, (tuple, list)) or not requests
                or any(not isinstance(r, str) or not r for r in requests) or len(set(requests)) != len(requests)
                or counts[2] - counts[3] != counts[0] or counts[5] - counts[4] != counts[0]
                or not self._store_receipt_standing(goal, p)):
            return None
        return dict(origin="store_rule", transferred=counts[0], requested=counts[1], chest_gain=counts[0],
                    bag_decrease=counts[0], deposit=counts[1], cursor_empty=True,
                    item=p["qualified_item_id"], quality=p["quality"], request_ids=list(requests))

    def _store_receipt_standing(self, goal, progress):
        """pkg69: an earned receipt survives a lost menu, not a new native session or a changed bag count of that item.

        Location is left out on purpose: walking off does not undo a paired store. An unreadable bag waits for the next read.
        """
        pin = goal.gather_pin
        binding, pid = self._binding_view()
        ctx = (self._water_context(pid, container=True) or self._water_context(pid)) if pid is not None else None
        inventory = self._inventory_contents(ctx)
        if (pin is None or ctx is None or inventory is None
                or self._store_identity(ctx)[:-1] != self._store_identity(pin)[:-1]
                or not self._water_snapshot_available(binding, ctx, observation_only=True)):
            return False
        wanted = progress["qualified_item_id"], progress["quality"]
        return sum(n for qid, quality, n in inventory["contents"] if (qid, quality) == wanted) == progress["actor_count"]

    def _store_rule_verdict(self, goal):
        """Only the complete objective auto-finishes; partial observed effects remain explicit facts."""
        if (goal is None or goal.progress_marker == "terminal" or goal.pending_container_exit is not None
                or (goal.episode_plan and goal.episode_index + 1 != len(goal.episode_plan))):
            return None
        observed = self._paired_store_observed(goal) or self._store_transfer_receipt(goal)
        if observed is None or observed["transferred"] < observed["requested"]:
            return None
        if (goal.episode_plan and goal.episode_plan[goal.episode_index].mode == "one_tree"
                and not (goal.gather_progress.one_tree_effect and observed["deposit"] > 0
                         and observed["chest_gain"] >= observed["deposit"])):
            return None
        p = goal.transfer_progress
        observed = dict(observed, original_objective_satisfied=True, actual_stored=[dict(
            item=p['qualified_item_id'], quality=p['quality'], quantity=observed['transferred'],
            request_ids=list(p.get('request_ids', ())) )])
        return observed

    def _store_rule_pending(self) -> bool:
        """Runner idle check: a provable store finish is about to be recorded (never HOLD in that gap), or (FIXB30
        D-B27-2) a recorded one is closing its chest / delivering its result."""
        try:
            if getattr(self, "_store_finish_closing", None) is not None:
                return True
            return self._store_rule_verdict(self._active) is not None
        except Exception:  # noqa: BLE001
            return False

    def process_store_rule(self) -> int:
        """Worker hook: record the deterministic store finish once nothing is in flight, then close the chest.

        Like tool_work_rule / routine_sleep_rule: no provider call, no model finish needed (a store that
        completed but was never followed by a model finish left the menu open)."""
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech, READY
        try:
            with self._lock:
                goal = self._active
                if (goal is None or self._status != READY or self._worker_stop.is_set() or self._action_busy
                        or self._inflight_generation is not None or self._results_pending):
                    return 0
                observed = self._store_rule_verdict(goal)
                if observed is None:
                    return 0
                verdict = FinishEvidenceView("achieved", "observed_paired_transfer_objective", observed)
                goal.last_finish = verdict
                goal.progress_marker = "terminal"
                goal.revision += 1
                self._pending_continuation = None
                self._pending_event_reason = None
                self._journal({"ev": "finish_verified", "goal_id": goal.goal_id, "goal_revision": goal.revision,
                               "verdict": asdict(verdict), "origin": "store_rule"})
                # FIXB30 D-B27-2 (live V2B27WOOD-c: `finish_verified store_rule` then `store_close_rule not_taken
                # action_cancelled`, the chest stayed open, report HELD continuation_not_ready): between this lock and the
                # close's own busy mark the runner's idle check saw nothing pending (goal terminal, not busy, no result
                # yet), ended the trial HELD and its stop cancelled the close. The finish + close + result delivery is
                # one pending unit for the idle check until the result is delivered.
                self._store_finish_closing = goal.goal_id
            try:
                result = OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                                         speech=StatusSpeech("Kayu sudah tersimpan; terverifikasi dari isi chest."),
                                         finish_evidence=verdict)
                self._close_store_after_finish(result, goal)
                if self._on_generation_result is not None:
                    from types import SimpleNamespace
                    self._on_generation_result(SimpleNamespace(reason="store_rule_finish", generation_id=None), result)
            finally:
                with self._lock:
                    self._store_finish_closing = None
            return 1
        except Exception as exc:  # noqa: BLE001 — the worker loop must survive
            self._journal({"ev": "store_rule_error", "error": type(exc).__name__})
            return 0
