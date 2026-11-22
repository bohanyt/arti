"""FIXB53: a bounded native event movement offer; never infer authority from a control flag."""
from collections.abc import Mapping
import re


def token_shape(value):
    return isinstance(value, str) and re.fullmatch(r"ecm:[0-9a-f]{64}", value) is not None


def movement_offer(facts):
    block = facts.get("event_movement") if isinstance(facts, Mapping) else None
    if (not isinstance(block, Mapping) or block.get("available") is not True
            or not token_shape(block.get("event_token")) or type(block.get("max_ticks")) is not int
            or block["max_ticks"] != 4 or not _reviewed(block)
            or type(block.get("command_index")) is not int or block["command_index"] < 0):
        return None
    return dict(block)


# fix58A (a43 D9:J1061/J1098): `nearby` is ModEntry.EventNearbyTargets, a distance-ordered window
# around the farmer's current tile. It changes when she walks, so it is a view, not event authority.
_POSITION_VIEW_KEYS = frozenset({"nearby"})


def movement_authority(offer):
    """The native authority of an event movement offer: token, sequence, command, festival phase, bound.

    Two offers with the same authority are the same native control grant even after ARTI moved."""
    if not isinstance(offer, Mapping):
        return None
    return {k: v for k, v in offer.items() if k not in _POSITION_VIEW_KEYS}


def same_movement_authority(a, b):
    return movement_authority(a) == movement_authority(b)


def _reviewed(block):
    if block.get("generic") is True and block.get("player_control") is True:
        return True  # authority is the native instance/command token, revalidated on every consumed frame
    if not isinstance(block.get("sequence"), str):
        return False
    if block.get("sequence") in ("haleyBeach", "haleyBeach2"):
        return block.get("is_festival", False) is False
    pairs = {("festival_spring13", "eggFestival"), ("festival_spring24", "flowerFestival"),
             ("festival_summer11", "luau"), ("festival_summer28", "jellies"),
             ("festival_winter8", "iceFestival"), ("festival_winter25", "christmas")}
    if block.get("is_festival") is not True:
        return False
    if not isinstance(block.get("festival_id"), str):
        return False
    pair = (block.get("festival_id"), block.get("sequence"))
    return (pair in pairs and block.get("main_event_running") is False and block.get("timer_positive") is False
            or pair == ("festival_spring13", "eggHunt") and block.get("main_event_running") is True
            and block.get("timer_positive") is True)


OUTCOMES = {"event_target": "EventTargetActivated", "festival_npc": "DialogueOpened",
            "festival_shop": "MenuOpened", "festival_seat": "FestivalSeated",
            "festival_egg": "FestivalEggCollected", "festival_egg_route": "FestivalEggCollected",
            "event_interact": "EventInteractionObserved"}
APPROACH_KINDS = {"event_target_route", "festival_npc_route", "festival_shop_route", "festival_seat_route", "event_interact_route"}
ROUTE_KINDS = APPROACH_KINDS | {"festival_egg_route"}
OUTCOMES.update({kind: OUTCOMES[kind[:-6]] for kind in APPROACH_KINDS})
MAX_ROUTE_INPUTS = 40


def route_cost(offer, target):
    if target.get("kind") in APPROACH_KINDS:
        return offer["route_inputs"][target["source_fact_id"]]
    return MAX_ROUTE_INPUTS if target.get("kind") == "festival_egg_route" else 1


def receipt_target_id(args):
    """Native source grammar: event:kind:object:tile[:immutable descriptor]."""
    parts = args["source_fact_id"].split(":")
    if (len(parts) not in (4, 5) or parts[:2] != ["event", args["kind"]]
            or re.fullmatch(r"ei[1-9][0-9]*", parts[2]) is None
            or parts[3] != f'{args["tile_x"]},{args["tile_y"]}'
            or len(parts) == 5 and re.fullmatch(r"[0-9a-f]{64}", parts[4]) is None):
        return None
    return parts[2]


def effect_valid(kind, before, after, target_id=None):
    """Independent check of the newer native effect receipt, including its exact prop identity."""
    native_kind = kind[:-6] if kind in ROUTE_KINDS else kind
    kind = native_kind
    if (not isinstance(before, Mapping) or not isinstance(after, Mapping)
            or before.get("scope_valid") is not True or after.get("scope_valid") is not True
            or before.get("kind") != native_kind or after.get("kind") != native_kind
            or not isinstance(before.get("target_id"), str) or before.get("target_id") != after.get("target_id")
            or target_id is not None and after.get("target_id") != target_id
            or type(before.get("command")) is not int or type(after.get("command")) is not int):
        return False
    if (before["command"] < 0 or before.get("control") is not True
            or any(type(row.get(key)) is not bool for row in (before, after)
                   for key in ("control", "timer_positive", "present", "expected_menu", "sitting"))):
        return False
    if kind == "event_target":
        from arti_stardew_native_targets import xy
        before_tile = xy(before.get("target_tile"))
        if before.get("sequence") not in ("haleyBeach", "haleyBeach2") or before_tile is None:
            return False
        if before.get("sequence") == "haleyBeach":
            return (after.get("sequence") == "haleyBeach2" and after.get("control") is True
                    and after["command"] == before["command"]
                    and xy(after.get("target_tile")) is not None and xy(after.get("target_tile")) != before_tile)
        return (before.get("sequence") == "haleyBeach2" and after.get("sequence") is None
                and after.get("control") is False and after["command"] == before["command"] + 1)
    if kind == "event_interact":
        return (before.get("expected_menu") is False and (after.get("expected_menu") is True
                and isinstance(after.get("menu_type"), str) or after["command"] > before["command"]
                or after.get("sequence") != before.get("sequence") or after.get("control") is False))
    if any(before.get(key) != after.get(key) for key in ("sequence", "control", "command", "target_tile", "timer_positive")):
        return False
    if kind in ("festival_npc", "festival_shop"):
        return (before.get("expected_menu") is False and after.get("expected_menu") is True
                and after.get("menu_type") == ("DialogueBox" if kind == "festival_npc" else "ShopMenu"))
    if after.get("menu_type") is not None:
        return False
    if kind == "festival_seat":
        return before.get("sitting") is False and after.get("sitting") is True
    return (kind in ("festival_egg", "festival_egg_route") and before.get("timer_positive") is True
            and after.get("timer_positive") is True and before.get("present") is True and after.get("present") is False
            and type(before.get("score")) is int and type(after.get("score")) is int
            and after["score"] == before["score"] + 1)


def action_offer(facts):
    """Request-local bounded offers copied from an authoritative native publication."""
    import arti_stardew_command as command
    block = facts.get("event_actions") if isinstance(facts, Mapping) else None
    move = movement_offer(facts)
    if (not isinstance(block, Mapping) or move is None or block.get("event_token") != move["event_token"]
            or block.get("sequence") != move["sequence"]):
        return None
    targets = block.get("targets")
    # ADR 240: with the menu reader on, one faced target, up to six egg routes and sixteen map routes.
    if not isinstance(targets, list) or len(targets) > 32:
        return None
    offered = {}
    for index, target in enumerate(targets):
        if not isinstance(target, dict) or target.get("kind") not in OUTCOMES:
            return None
        try:
            command.validate_world_interaction_args(target)
        except command.KesalahanPerintah:
            return None
        kind = target["kind"]
        if kind in ("event_target", "event_target_route"):
            from arti_stardew_native_targets import xy
            if (move["sequence"] not in ("haleyBeach", "haleyBeach2")
                    or (target["tile_x"], target["tile_y"]) != xy(block.get("target_tile"))):
                return None
        if kind.startswith("festival_") and move.get("is_festival") is not True and (
                move.get("generic") is not True or kind not in ("festival_npc", "festival_npc_route", "festival_shop", "festival_shop_route")):
            return None
        if kind in ("festival_egg", "festival_egg_route") and move.get("sequence") != "eggHunt":
            return None
        offered[f"e{index + 1}"] = dict(target)
    costs = block.get("route_inputs", {})
    if any(not isinstance(costs, Mapping) or type(costs.get(t["source_fact_id"])) is not int
           or not 12 <= costs[t["source_fact_id"]] <= 512 for t in offered.values() if t["kind"] in APPROACH_KINDS):
        return None
    result = {"event_token": move["event_token"], "sequence": move["sequence"],
              "target_tile": block.get("target_tile"), "targets": offered}
    if any(t["kind"] in APPROACH_KINDS for t in offered.values()):
        result["route_inputs"] = dict(costs)
        labels = block.get("route_labels", {})
        if isinstance(labels, Mapping):
            result["route_labels"] = {k: v[:80] for k,v in labels.items() if isinstance(k,str) and isinstance(v,str)}
    people = block.get("people")
    if isinstance(people, Mapping):
        result["people"] = {key: name[:80] for key, name in list(people.items())[:128]
                            if isinstance(key, str) and isinstance(name, str) and name.strip()}
    if isinstance(block.get("festival_id"), str) and "people" in result:
        result["event_id"] = block["festival_id"]
    seen = seen_offer(block) if "people" in result else None
    if seen is not None:
        result["seen"] = seen
    # ADR 240: what the event map shows is a fact before any route exists; a choice waits for its route.
    return result if offered or seen and (seen["people"] or seen["things"]) else None


SEEN_MAX = 24  # EventControlAction.MaxEventSeen


def seen_offer(block):
    """ADR 240: people (actor id, tile, the game's talk check) and press cells (route source, kind, tile), nearest first."""
    seen = block.get("seen") if isinstance(block, Mapping) else None
    if not isinstance(seen, Mapping):
        return None
    tile = lambda x, y: [x, y] if all(type(v) is int and 0 <= v <= 4096 for v in (x, y)) else None
    people, things = [], []
    for row in seen.get("people") or ():
        if (isinstance(row, (list, tuple)) and len(row) == 4 and isinstance(row[0], str) and row[0]
                and tile(row[1], row[2]) and type(row[3]) is bool):
            people.append(dict(id=row[0], tile=tile(row[1], row[2]), talk=row[3]))
    for row in seen.get("things") or ():
        if (isinstance(row, (list, tuple)) and len(row) == 4 and isinstance(row[0], str) and row[0].startswith("event:")
                and row[1] in APPROACH_KINDS and tile(row[2], row[3])):
            things.append(dict(source=row[0][:300], kind=row[1], tile=tile(row[2], row[3])))
    total = lambda key, floor: seen[key] if type(seen.get(key)) is int and seen[key] >= floor else floor
    return dict(people=people[:SEEN_MAX], things=things[:SEEN_MAX],
                people_total=total("people_total", len(people)), things_total=total("things_total", len(things)),
                things_known=seen.get("things_known") is True, routes_ready=seen.get("routes_ready") is True)


class EventActionsRuntimeMixin:
    def _festival_reflex_view(self, player_id):
        rec, _ = self._ui_key_record(player_id)
        if rec is None:
            return None
        row = rec.facts.get("festival_reflex")
        if (not isinstance(row, Mapping) or row.get("kind") not in {"eggHunt", "iceFishing"}
                or row.get("state") not in {"intro", "ready", "live", "stopping", "end"}
                or not isinstance(row.get("run_id"), str)
                or re.fullmatch(r"(?:ecm|iff):[0-9a-f]{64}", row["run_id"]) is None
                or type(row.get("timer_ms")) is not int):
            return None
        # Facts only, equally useful outside trials. No owner-permission coaching or trace dump.
        return {k: row[k] for k in ("run_id", "kind", "state", "auto_started", "start_score", "score",
                "verified_pickups", "timer_ms", "cleanup", "reason", "result", "win_threshold", "replans",
                "lifecycle_id", "command_index", "final_verified", "winner_ids", "winner_membership", "prize") if k in row}

    def festival_lifecycle_due(self):
        """Bounded native festival work counts as work in the ordinary trial's idle check."""
        import time
        goal = self._active
        if (goal is None or goal.play_mode != "local_activity" or goal.progress_marker == "terminal"
                or self._worker_stop.is_set() or self._action_cancel.is_set() or self._owner_voice_busy
                or self._status != "READY"):
            return False
        lifecycle = getattr(self, "_festival_lifecycle", None)
        if lifecycle is not None and lifecycle["goal"] is goal:
            return lifecycle["goal"] is goal and not lifecycle.get("finished")
        binding, pid = self._binding_view()
        if binding.status != "bound" or binding.is_main_player is not False or binding.is_split_screen is not False:
            return False
        row = self._festival_reflex_view(pid)
        return (row is not None and row["state"] in {"intro", "ready"} and token_shape(row.get("lifecycle_id"))
                and not (self._trial_deadline_unix_ms is not None
                         and int(time.time() * 1000) >= self._trial_deadline_unix_ms))

    def _process_festival_lifecycle(self):
        """Intro/result dialogue uses the existing pinned native A path, never a model answer or HOST input."""
        import time
        from dataclasses import asdict
        from types import SimpleNamespace
        import arti_stardew_command as command
        from arti_stardew_runtime import FinishEvidenceView, OwnerTurnResult, StatusSpeech, PAUSED_RECOVERY
        if getattr(self, "_festival_lease", None) is not None:
            return 0
        with self._lock:
            lifecycle = getattr(self, "_festival_lifecycle", None)
            if lifecycle is not None and lifecycle["goal"] is not self._active:
                self._festival_lifecycle = lifecycle = None
            if lifecycle is not None and lifecycle.get("finished"):
                return 0
            if lifecycle is None and not self.festival_lifecycle_due():
                return 0
            goal = self._active
            _, pid = self._binding_view()
            row = self._festival_reflex_view(pid)
            ctx = self._ui_key_context(pid)
            if lifecycle is None:
                binding, _ = self._binding_view()
                if binding.is_main_player is not False or binding.is_split_screen is not False or ctx is None:
                    return 0
                lifecycle = self._festival_lifecycle = dict(goal=goal, pin=row["lifecycle_id"], kind=row["kind"],
                    identity=(ctx.runtime_instance_id, ctx.command_session_id, ctx.observation_session_id, ctx.player_id,
                              ctx.screen_id, ctx.location), phase="intro", deadline=time.monotonic() + 120,
                    presses=0, lease_cleanup=False, last_target=None)
                # Take ownership before another model continuation can inspect a stable box.
                self._pending_continuation = None
                goal.no_progress_blocked = False
                goal.no_progress_until = None
                goal.no_progress_count = goal.no_progress_rechecks = 0
                self._journal({"ev": "festival_lifecycle_start", "kind": row["kind"], "lifecycle_id": lifecycle["pin"]})
            failure = None
            if (goal is not lifecycle["goal"] or self._worker_stop.is_set() or self._action_cancel.is_set()
                    or self._owner_voice_busy or self._status != "READY"):
                failure = "festival_lifecycle_interrupted"
            elif (row is None or ctx is None or row.get("lifecycle_id") != lifecycle["pin"] or row["kind"] != lifecycle["kind"]
                    or (ctx.runtime_instance_id, ctx.command_session_id, ctx.observation_session_id, ctx.player_id,
                        ctx.screen_id, ctx.location) != lifecycle["identity"]):
                failure = "festival_lifecycle_stale_or_changed"
            elif (time.monotonic() >= lifecycle["deadline"] or self._trial_deadline_unix_ms is not None
                    and int(time.time() * 1000) >= self._trial_deadline_unix_ms):
                failure = "festival_lifecycle_timeout:" + lifecycle["phase"]
            if failure is None and (self._action_busy or self._inflight_generation is not None or self._results_pending
                                    or self._trial_result_pending()):
                return 1
            if failure is None:
                try:
                    folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
                    session = self._commands.baca_sesi(folder, retry_detik=0.1)
                    raw = session.mentah
                    request_id = getattr(goal.last_action, "request_id", None) or getattr(self, "_last_input_request_id", None)
                    released = command.input_release_state(raw, request_id=request_id,
                        result=command._baca_json(folder / command.NAMA_RESULT))
                    if released is not True:
                        if not lifecycle.get("release_fault"):
                            self._journal({"ev": "FAULT", "type": "live7_recovery", "cause": "festival_release_unproven" if released is None else "festival_input_held",
                                "refs": {"request_id": request_id}, "disposition": "RECOVER", "outcome_class": "UNCERTAIN", "verified": False})
                            lifecycle["release_fault"] = True
                        if released is None:
                            return 1  # re-observe; no effect credit or next input on missing W1
                        failure = "festival_input_held"
                        active_skill = self._skill_session.current if self._skill_session else None
                        if active_skill and active_skill.mode == "part_live":
                            self._skill_end_locked(active_skill, "held", "SAFETY_STOP", "live7:uncontrolled_input")
                    if (not command.lease_state(raw).matches(ctx.command_session_id, ctx.runtime_instance_id, pid, ctx.screen_id, ctx.location)
                            or session.inflight_request_id is not None or command.lease_state(raw).owner
                            or command.admission_state(raw, (("gamepad_input_enabled", True), ("ui_pad_enabled", True), ("release_acked", True)), idle=False)
                            or any((folder / name).exists() for name in (command.NAMA_STOP, command.NAMA_CONTROL, command.NAMA_REQUEST))):
                        failure = "festival_lifecycle_session_unavailable"
                except (OSError, ValueError, TypeError, command.KesalahanPerintah):
                    failure = "festival_lifecycle_session_unavailable"
            if failure:
                lifecycle["finished"] = True
                self._pending_continuation = None
                self._status = PAUSED_RECOVERY
                self._journal({"ev": "festival_lifecycle_error", "reason": failure})
                return 1
            if row["state"] == "ready":
                return 0  # the existing automatic lease admission owns the timed phase
            if row["state"] in {"live", "stopping"}:
                return 0
            if self._action_busy or self._inflight_generation is not None or self._results_pending or self._trial_result_pending():
                return 1
            if row["state"] == "end" and row.get("final_verified") is True:
                complete = (row.get("auto_started") is True and row.get("cleanup") is True and lifecycle["lease_cleanup"]
                    and type(row.get("verified_pickups")) is int and row["verified_pickups"] >= 1
                    and row.get("result") in {"win", "lose"} and type(row.get("score")) is int
                    and isinstance(row.get("winner_ids"), list) and bool(row["winner_ids"])
                    and type(row.get("winner_membership")) is bool and isinstance(row.get("prize"), Mapping))
                verdict = FinishEvidenceView("achieved" if complete else "partial", "festival_final_verified" if complete
                    else "festival_final_incomplete", dict(row, lease_cleanup=lifecycle["lease_cleanup"], origin="festival_rule"))
                goal.last_finish = verdict
                goal.progress_marker = "terminal"
                goal.revision += 1
                lifecycle["finished"] = True
                self._pending_continuation = self._pending_event_reason = None
                self._journal({"ev": "finish_verified" if complete else "festival_final_incomplete", "goal_id": goal.goal_id,
                    "goal_revision": goal.revision, "verdict": asdict(verdict), "origin": "festival_rule"})
                result = OwnerTurnResult(admitted=True, skip_reply_tags=True, applied=True, reason="finish",
                    speech=StatusSpeech(f"Festival selesai: skor {row.get('score')}, {row.get('result')}."), finish_evidence=verdict)
                if self._on_generation_result is not None:
                    self._on_generation_result(SimpleNamespace(reason="festival_finish", generation_id=None), result)
                return 1
            view = self._ui_pad_view(pid)
            dialogue = (((view.g3 or {}).get("event") or {}).get("dialogue") or {}) if view else {}
            owner = dialogue.get("festival_owner") or {}
            if (view is None or not view.available or view.context != "event" or view.menu_type != "DialogueBox"
                    or dialogue.get("kind") != "dialogue" or not view.press_allowed("A")
                    or owner.get("lifecycle_id") != lifecycle["pin"] or not owner.get("dialogue_id")
                    or type(owner.get("command_index")) is not int or owner["command_index"] < 0
                    or not owner.get("command_hash")):
                return 1  # typing / scripted movement / other-player barrier; all remain bounded
            if view.target_token == lifecycle["last_target"]:
                return 1  # never replay a verified A against a stale publication
            if lifecycle["presses"] >= 24:
                lifecycle["deadline"] = 0
                return 1
            lifecycle["presses"] += 1
            lifecycle["last_target"] = view.target_token
            self._action_busy = True
            expected = lifecycle["pin"]
        def revalidate():
            current = self._festival_reflex_view(pid)
            fresh = self._ui_pad_view(pid)
            fresh_dialogue = (((fresh.g3 or {}).get("event") or {}).get("dialogue") or {}) if fresh else {}
            if (self._active is not goal or current is None or current.get("lifecycle_id") != expected
                    or current["state"] not in {"intro", "end"} or fresh is None
                    or fresh_dialogue.get("kind") != "dialogue" or fresh_dialogue.get("festival_owner") != owner):
                raise ValueError("festival_dialogue_pin_changed")
        try:
            run, _ = self._run_ui_pad(command.PRIMITIVE_UI_PRESS, view.press_args("A"), origin="festival_lifecycle",
                                      revalidate_extra=revalidate)
        finally:
            with self._lock:
                self._action_busy = False
        if not run.verified or not run.cleanup:
            with self._lock:
                if not run.submitted and run.cleanup:
                    # No physical request was written. A new publication may
                    # offer a different box after a script/typing transition.
                    return 1
                lifecycle["finished"] = True
                self._status = PAUSED_RECOVERY
            self._journal({"ev": "festival_lifecycle_error", "reason": run.reason, "cleanup": run.cleanup})
        return 1

    def process_festival_reflex(self):
        """Keep the existing lease alive for one native timed game; no egg/cast decision or input inbox request."""
        if self._process_festival_lifecycle():
            return 1
        import time
        import arti_stardew_command as command
        from arti_stardew_runtime import _WaterLeasePin, _new_id, READY, PAUSED_RECOVERY
        lease = getattr(self, "_festival_lease", None)
        pin = lease[0] if lease else None
        try:
            with self._lock:
                goal = self._active
                _, pid = self._binding_view()
                row = self._festival_reflex_view(pid)
                ctx = self._ui_key_context(pid)
                if lease is None:
                    if (goal is None or goal.play_mode != "local_activity" or goal.progress_marker == "terminal"
                            or self._status != READY or self._worker_stop.is_set() or self._action_cancel.is_set()
                            or self._action_busy or self._inflight_generation is not None or self._results_pending
                            or self._trial_result_pending() or row is None or ctx is None or row["state"] != "ready"
                            or row["timer_ms"] <= 0 or row["run_id"] in getattr(self, "_festival_attempts", set())):
                        return 0
                    folder = self._commands.folder_perintah(self._config.get("stardew_command_dir"))
                    session = self._commands.baca_sesi(folder, retry_detik=0.1)
                    raw = session.mentah
                    binding, _ = self._binding_view()
                    if (binding.status != "bound" or binding.is_main_player is not False or binding.is_split_screen is not False
                            or not command.lease_state(raw).matches(ctx.command_session_id, ctx.runtime_instance_id, pid, ctx.screen_id, ctx.location)
                            or command.admission_state(raw, (("gamepad_input_enabled", True), ("release_acked", True)))
                            or not command.lease_state(raw).can_acquire(self._lease_generation + 1) or any((folder / name).exists() for name in
                                (command.NAMA_STOP, command.NAMA_CONTROL, command.NAMA_REQUEST))):
                        return 0
                    self._festival_attempts = (list(getattr(self, "_festival_attempts", ())) + [row["run_id"]])[-32:]
                    self._lease_generation += 1
                    pin = _WaterLeasePin(folder, session, _new_id("festival"), self._lease_generation)
                    self._water_action_pin = pin
                    self._action_busy = True
                    self._publish_action(lambda: self._commands.tulis_kontrol(command.KIND_ACQUIRE, folder=folder,
                        sesi=session, owner_id=pin.owner, generation=pin.generation, control_seq=1,
                        expected_screen_id=session.screen_id))
                    self._festival_lease = (pin, row["run_id"], goal, time.monotonic(), row)
            if lease is None:
                self._wait_water_acquire(pin)
                lifecycle = getattr(self, "_festival_lifecycle", None)
                if lifecycle and not lifecycle.get("finished"):
                    lifecycle.update(phase="timed", deadline=time.monotonic() + 150)
                self._journal({"ev": "festival_reflex_start", "kind": row["kind"], "run_id": row["run_id"]})
                return 1
            pin, run_id, original_goal, started, last = lease
            if (self._worker_stop.is_set() or self._action_cancel.is_set() or goal is not original_goal
                    or row is None or row["run_id"] != run_id or time.monotonic() - started > 150
                    or self._trial_deadline_unix_ms is not None and int(time.time()*1000) >= self._trial_deadline_unix_ms):
                raise ValueError("festival_interrupted_or_stale")
            current = self._water_bound_session(pin)
            raw = current.mentah
            lease_state = command.lease_state(raw)
            if not lease_state.owns(pin.owner, pin.generation) or lease_state.blocked:
                raise ValueError("festival_owner_changed")
            if row.get("score") != last.get("score") or row["state"] != last["state"]:
                self._journal({"ev": "festival_reflex_live", **row})
                self._festival_lease = (pin, run_id, original_goal, started, row)
            if row["state"] == "end":
                cleanup = self._cleanup_water(pin) and row.get("cleanup") is True
                with self._lock:
                    self._festival_lease = None
                    self._water_action_pin = None
                    self._action_busy = False
                    self._pending_continuation = None
                    if not cleanup:
                        self._status = PAUSED_RECOVERY
                self._journal({"ev": "festival_reflex_end", **row, "lease_cleanup": cleanup})
                if cleanup:
                    lifecycle = getattr(self, "_festival_lifecycle", None)
                    if lifecycle and not lifecycle.get("finished"):
                        lifecycle.update(phase="result", deadline=time.monotonic() + 90, lease_cleanup=True, last_target=None)
                    else:
                        self._resume_after_pause(reason="festival_reflex_end")
                return 1
            expiry = lease_state.expiry
            if not lease_state.owns(pin.owner, pin.generation, live=True):
                raise ValueError("festival_lease_expired")
            if expiry - int(time.time()*1000) <= command.RENEW_HINT_MS:
                self._renew_water(pin, None, allow_idle=True)
            return 1
        except Exception as exc:  # fix10f: recover-and-log (no persistent STOP); the release proof owns every failure
            if pin is not None:
                try:
                    self._recover_lease_fault(pin, "festival_reflex:" + str(exc)[:80])
                except Exception:
                    pass
                cleanup = self._cleanup_water(pin)
            else:
                cleanup = True
            with self._lock:
                self._festival_lease = None
                self._water_action_pin = None
                self._action_busy = False
                self._status = PAUSED_RECOVERY
            self._journal({"ev": "festival_reflex_error", "reason": str(exc)[:120], "cleanup": cleanup})
            return 1

    def _event_actions_offer(self, player_id):
        rec, age = self._ui_key_record(player_id)
        if rec is None or not rec.authoritative or not rec.is_complete or age > 2.5:
            return None
        offer = action_offer(rec.facts)
        ctx = self._ui_key_context(player_id)
        if offer is None or ctx is None:
            return None
        offer["identity"] = {key: getattr(ctx, key) for key in (
            "runtime_instance_id", "command_session_id", "observation_session_id", "context_generation",
            "player_id", "screen_id", "location")}
        return offer

    def _event_action_reason(self, request, args):
        if (not isinstance(args, dict) or set(args) != {"handle", "btn"} or args.get("btn") != "A"
                or not isinstance(args.get("handle"), str)):
            return "event_action_args_invalid"
        offer = request.snapshot.event_actions
        if offer is None or args.get("handle") not in offer["targets"]:
            return "event_action_not_offered"
        _, pid = self._binding_view()
        current = self._event_actions_offer(pid)
        if "people" not in offer:  # legacy event callers keep the original whole-offer fence
            return None if current == offer else "event_action_changed"
        if current is None or any(current.get(k) != offer.get(k) for k in ("identity", "event_token", "sequence")):
            return "event_action_changed"
        chosen = offer["targets"][args["handle"]]
        if chosen not in current["targets"].values() or route_cost(current, chosen) != route_cost(offer, chosen):
            return "event_action_changed"
        return None

    def _execute_event_action(self, decision, request, goal):
        from dataclasses import asdict
        from arti_stardew_runtime import ActionEvidenceView, OwnerTurnResult, PAUSED_RECOVERY
        from arti_stardew_ui_key_runtime import LeasedRun
        args = decision.choice.args
        with self._lock:
            reason = self._event_action_reason(request, args)
            native = None if reason else dict(request.snapshot.event_actions["targets"][args["handle"]])
            _, pid = self._binding_view()
            ctx = self._ui_key_context(pid)
        if reason or ctx is None:
            run = LeasedRun("not_taken", reason or "event_context_unavailable")
        else:
            def guard():
                reason = self._event_action_reason(request, args)
                if reason or not self._request_current(request) or self._active is not goal:
                    raise ValueError(reason or "event_action_superseded")
            run = self._run_leased_once("world_interact", native, ctx, origin="event-A", revalidate=guard,
                expect_outcome=OUTCOMES[native["kind"]],
                publication_cost=route_cost(request.snapshot.event_actions, native))
        after = {"native_outcome": run.outcome}
        if run.verified and run.cleanup and run.context_revision is not None and "people" in (request.snapshot.event_actions or {}):
            # Bind the answer to the same observation that witnessed this action.
            rec, age = self._ui_key_record(pid)
            if rec is not None and rec.domain_revision == run.context_revision and age <= 2:
                receipt = rec.facts.get("event_action_receipt") or {}
                effect = receipt.get("after") or {}
                menu = rec.facts.get("ui_pad") or {}
                if (receipt.get("request_id") == run.request_id and receipt.get("verified") is True
                        and effect.get("expected_menu") is True and effect.get("menu_type") == menu.get("menu_type")
                        and isinstance(menu.get("receiver_token"), str)):
                    after.update(receiver_token=menu["receiver_token"], menu_type=menu["menu_type"])
        no_input = not run.submitted or run.prearm_rejected and run.input_count == 0 and run.cleanup
        evidence = ActionEvidenceView(run.lifecycle, run.reason, run.request_id, run.outcome,
            before={"event_action": native}, after=after,
            world_effect_status="observed_event_effect" if run.verified else "zero_effect" if no_input else "uncertain",
            cleanup_confirmed=run.cleanup)
        with self._lock:
            superseded = not self._request_current(request) or self._active is not goal or self._action_cancel.is_set()
            if not superseded and goal is not None: goal.last_action = evidence
            if run.uncertain and run.submitted:
                self._status = PAUSED_RECOVERY
                self._revoke_play()
            self._action_busy = False
            if self._inflight_generation == request.generation_id: self._inflight_generation = None
            self._journal({"ev": "event_action", "action": asdict(evidence), "reserved_inputs":
                route_cost(request.snapshot.event_actions, native) if native else 1,
                "consumed_inputs": run.input_count})
            if not superseded and not run.uncertain and not run.verified: self._schedule_continuation(decision, request)
        if not superseded and run.verified and run.cleanup:
            if run.outcome == "DialogueOpened" and (getattr(self, "_config", None) or {}).get("voice_game_memory_enabled") is True:
                from arti_runtime.festival_facts import event_choices
                from arti_runtime import day_record
                actor = next((r["label"] for r in event_choices(request.snapshot).values()
                              if r["handle"] == args["handle"] and r["label"]), None)
                motor = self._state.latest(pid)
                if actor and motor is not None and not self._state.is_stale(pid):
                    day_record.live(self, self._date_tuple(motor), motor.time_of_day, "event_talk",
                                    f"Aku berbicara dengan {actor}.", place=motor.location, witness=run.request_id)
            # Native A may change sequence/receiver/pose. Refresh read-only facts;
            # never carry the old event/actor offer into another mutation.
            self._resume_after_pause(reason="event_action_verified")
        return OwnerTurnResult(admitted=True, applied=run.verified and not superseded, discarded=superseded,
            skip_reply_tags=True, reason=run.reason, action_evidence=evidence)


NATIVE_SLIDE_PX_PER_TICK = 2  # fix63A: mirrors EventControlMove.NativeSlidePxPerTick


def displacement(direction, ticks, before, after, *, max_ticks=4, slide_px_per_tick=NATIVE_SLIDE_PX_PER_TICK):
    """Independent motor readback; 16 px/tick is a conservative cap, not a speed promise.

    fix63A (a45 D20:J1226): Farmer.MovePosition slides a blocked step around a corner, so a step past a tree
    moves a little on the other axis (down: dx -1.25, dy 15.84 over four ticks). ADR 97 requires at least 1 px
    forward per tick. Mirrors EventControlMove.Displacement, including strict reviewed-route readback:
    slide_px_per_tick=0, with max_ticks=40 for route-authority segments (EventControlAction.SegmentTicks).
    Python submits routes as world_interact; C# owns their per-step readback, not the free-move caller."""
    import math
    try:
        dx, dy = after["pixel"][0] - before["pixel"][0], after["pixel"][1] - before["pixel"][1]
        along, across = {"up": (-dy, dx), "down": (dy, dx), "left": (-dx, dy), "right": (dx, dy)}.get(direction, (-1, 0))
        return (all(math.isfinite(v) for v in (dx, dy)) and type(ticks) is int and 1 <= ticks <= max_ticks
                and 0 < abs(dx) + abs(dy) <= ticks * 16
                and along >= ticks and abs(across) <= ticks * slide_px_per_tick)
    except (KeyError, TypeError, IndexError):
        return False


FESTIVAL_LEAVE_DETAIL = "festivalLeave"  # PadContextTable.FestivalLeaveDetail


def festival_free_roam(offer):
    """A pinned festival offer she walks freely in, before the main event."""
    return (isinstance(offer, Mapping) and offer.get("is_festival") is True
            and offer.get("main_event_running") is False and offer.get("player_control") is True)


def festival_leave_offered(facts):
    """fix63A (ADR 94): ui.context says Select asks the native leave question now. An older DLL has no such cell."""
    if not isinstance(facts, Mapping) or not festival_free_roam(movement_offer(facts)):
        return False
    pad = facts.get("pad")
    cell = pad.get("buttons", {}).get("Select") if isinstance(pad, Mapping) and pad.get("enabled") is True else None
    return isinstance(cell, Mapping) and cell.get("detail") == FESTIVAL_LEAVE_DETAIL
