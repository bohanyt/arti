"""Generic buttons and literal observation deltas. No harvest/machine/animal rules."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping

KINDS = ("generic_action", "generic_use")
CHANGED, UNCHANGED = "WorldDeltaObserved", "WorldUnchanged"
SKILLS = {"aksi": KINDS[0], "pakai": KINDS[1]}
LANGKAH_RESENDS_MAX = 2  # fix58A: a move refused before publication is re-sent at most twice


def inventory_slots(facts):
    meta = (facts or {}).get("meta:inventory") or {}
    size = meta.get("slot_count")
    if type(size) is not int or not 1 <= size <= 48:
        return ()
    out = []
    for slot in range(size):
        row = facts.get("slot:%d" % slot)
        if not isinstance(row, Mapping) or row.get("slot") != slot or type(row.get("occupied")) is not bool:
            return ()
        if row["occupied"]:
            if (not isinstance(row.get("qualified_item_id"), str) or not row["qualified_item_id"]
                    or type(row.get("stack")) is not int or row["stack"] < 1
                    or row.get("quality") not in (0, 1, 2, 4)):
                return ()
            item = dict(qid=row["qualified_item_id"], stack=row["stack"], quality=row["quality"],
                        name=str(row.get("display_name") or row.get("name") or row["qualified_item_id"])[:80])
        else:
            item = dict(qid="", stack=0, quality=0, name="empty slot")
        out.append(dict(slot=slot, active=row.get("is_active") is True, **item))
    return tuple(out)


def inventory(runtime, pin):
    if pin is None:
        return ()
    rec = runtime._obs.get(pin.player_id, "progress.inventory")
    if (rec is None or not getattr(rec, "is_complete", False) or not getattr(rec, "authoritative", False)
            or getattr(rec, "stale", True) or getattr(rec, "requires_resync", True)
            or getattr(rec, "session_id", None) != pin.observation_session_id or getattr(rec, "screen_id", None) != pin.screen_id
            or not 0 <= time.monotonic()-rec.received_monotonic <= 2):
        return ()
    return inventory_slots(rec.facts)


def fingerprint(rows):
    return tuple((r["qid"], r["stack"], r["quality"]) for r in rows)


def menu_key_published(snapshot, context, *, button="Start", menu="GameMenu"):
    """PM-1 fix13b: native's own pad table (PadContextTable.Translate, the pad_button arm's term) admits Start = the
    game menu now, and no menu is up. The opened GameMenu's tabs and pages are then ordinary published affordances."""
    import arti_stardew_skills as S
    pad = (context.get("mines_ui") or {}).get("pad") or {}
    cell = (pad.get("buttons") or {}).get(button) or {}
    return (pad.get("enabled") is True and cell.get("family") == "pad_button"
            and str(cell.get("detail", "")).startswith(menu) and not S.menu_up(snapshot))


def calls(snapshot, context):
    """Buttons, any slot, and paths beside observed targets. No feature admission."""
    import arti_stardew_skills as S
    offered = S._g(snapshot, "generic_world_targets", ()) or parse_targets(context.get("mines_ui"))
    from scripts.live7.interactions import ordinary_targets
    named = ordinary_targets(context)
    named_calls = [S.SkillCall("aksi", row["ref"], tile=tuple(row["tile"])) for row in named
                   if row.get("status") in ("openable", "available")]
    from arti_runtime.festival_facts import action_calls as festival_calls
    named_calls.extend(festival_calls(snapshot))
    if menu_key_published(snapshot, context, button="Select", menu="QuestLog"):
        named_calls.append(S.SkillCall("cek_jurnal"))
    if not offered:
        block = ((context.get("mines_ui") or {}).get("ui_pad") or {}).get("cursor_settle") or {}
        menu = menu_key_published(snapshot, context) or block.get("complete") is True and bool(block.get("cursor"))
        return named_calls + ([S.SkillCall("inventori")] if menu else [])
    out = named_calls + [S.SkillCall(name) for name, kind in SKILLS.items() if any(t["kind"] == kind for t in offered)]
    out.append(S.SkillCall("inventori"))
    out.extend(S.SkillCall("langkah", d) for d in ("up", "right", "down", "left"))
    rows = S._g(snapshot, "generic_inventory", ())
    out.extend(S.SkillCall("pegang_slot", str(r["slot"])) for r in rows if not r["active"])
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    grid = context.get("nav_grid") or {}
    if here is None:
        return out
    adjacent = {(here[0]+dx, here[1]+dy) for dx, dy in ((0,-1),(1,0),(0,1),(-1,0))}
    observed = {S._wave1_xy(f.get("tile")) for f in context.get("targets", {}).values() if isinstance(f, Mapping)}
    observed |= set(grid)
    out.extend(S.SkillCall("hadap", "%d,%d" % t) for t in sorted(adjacent & observed))
    from arti_stardew_goal_routes import open_paths, open_path
    parents = open_paths(grid, here)
    destinations = set()
    for f in context.get("targets", {}).values():
        tile = S._wave1_xy(f.get("tile")) if isinstance(f, Mapping) else None
        if tile is None:
            continue
        x, y = tile
        stands = [(x,y+1),(x-1,y),(x,y-1),(x+1,y)]
        reachable = [open_path(parents, s) for s in stands if s in parents]
        reachable = [p for p in reachable if p and len(p) > 1]
        if reachable:
            destinations.add(tuple(min(reachable, key=lambda p: (len(p), p[-1]))[-1]))
    for tile in sorted(destinations, key=lambda t: (abs(t[0]-here[0])+abs(t[1]-here[1]), t))[:16]:
        out.append(S.SkillCall("jalan_ke", "%d,%d" % tile, tile=tile))
    return out


def hold_step(call, snap, state):
    import arti_stardew_skills as S
    rows = S._g(snap, "generic_inventory", ())
    if not rows or not str(call.arg or "").isdigit():
        return S.Step("fail", reason="inventory_slot_unobserved")
    slot = state.setdefault("hold_slot", int(call.arg))
    if not 0 <= slot < len(rows):
        return S.Step("fail", reason="inventory_slot_unobserved")
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if state.get("hold_shift"):
        if (S._g(last, "request_id") == state["hold_previous"]
                or S._g(last, "world_effect_status") != "observed_pad_button"
                or S._g(last, "cleanup_confirmed") is not True):
            return S._wait(state, "hold", "toolbar_shift_unverified", "Amati toolbar", S.WAIT_S)
        prior = state.pop("hold_shift")
        expected = prior[12:] + prior[:12]
        if fingerprint(rows) != expected:
            return S.Step("fail", reason="toolbar_shift_mismatch")
        slot = state["hold_slot"] = (slot - 12) % len(rows)
    wanted = fingerprint(rows)[slot]
    if "hold_item" not in state:
        state["hold_item"] = wanted
    if wanted != state["hold_item"]:
        return S.Step("fail", reason="inventory_slot_changed")
    if slot < 12:
        if rows[slot]["active"]:
            return S.Step("done", reason="observed_equipped_item")
        target = next((t for t in S._g(snap, "selectable_items", ()) if t.get("slot") == slot
                       and (t.get("qualified_item_id"), t.get("stack"), t.get("quality")) == wanted), None)
        return S._decision(S.act("select_item", dict(target))) if target else S.Step("fail", reason="slot_not_selectable")
    offer = S._g(snap, "pad_button_offer") or {}
    if offer.get("button") != "RB":
        return S.Step("fail", reason="toolbar_shift_not_offered")
    state.update(hold_shift=fingerprint(rows), hold_previous=S._g(last, "request_id"))
    return S._decision(S.act("pad_button", dict(button="RB")))


def targets(runtime, pin):
    """Current, complete, actor/session-bound native offers; never synthesize a click."""
    if pin is None:
        return ()
    rec = runtime._obs.get(pin.player_id, "ui.context")
    if (rec is None or not getattr(rec, "is_complete", False) or not getattr(rec, "authoritative", False)
            or getattr(rec, "stale", True) or getattr(rec, "requires_resync", True)
            or getattr(rec, "session_id", None) != pin.observation_session_id or getattr(rec, "screen_id", None) != pin.screen_id
            or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
        return ()
    return parse_targets(rec.facts)


def ordinary(runtime, pin):
    """Named interactions share the native UI observation's freshness/actor/session fence."""
    if pin is None or getattr(pin, "player_id", None) is None:
        return ()
    rec = runtime._obs.get(pin.player_id, "ui.context")
    if (rec is None or not getattr(rec, "is_complete", False) or not getattr(rec, "authoritative", False)
            or getattr(rec, "stale", True) or getattr(rec, "requires_resync", True)
            or getattr(rec, "session_id", None) != pin.observation_session_id or getattr(rec, "screen_id", None) != pin.screen_id
            or not 0 <= time.monotonic() - rec.received_monotonic <= 2):
        return ()
    if not getattr(rec, "available", False):
        return ()
    from scripts.live7.interactions import ordinary_targets
    known = getattr(runtime, "_live7_known_chests", None)
    scene = runtime._obs.get(pin.player_id, "scene.current_location")
    facts = (scene.facts if scene is not None and getattr(scene, "authoritative", False)
             and getattr(scene, "available", False) and not getattr(scene, "stale", True)
             and not getattr(scene, "requires_resync", True) and scene.session_id == pin.observation_session_id
             and scene.screen_id == pin.screen_id and 0 <= time.monotonic() - scene.received_monotonic <= 2 else {})
    described = (getattr(runtime, "_config", None) or {}).get("live7_fact_line_enabled") is True
    return tuple(ordinary_targets(dict(mines_ui=rec.facts, location_id=pin.location,
        scene_map_affordances=facts.get("affordance:map"), targets=facts, describe_affordances=described,
        known_chests=known(pin.location) if known else ())))


def parse_targets(facts):
    from arti_stardew_command import validate_world_interaction_args, KesalahanPerintah
    block = facts.get("world_buttons") if isinstance(facts, Mapping) else None
    out = []
    for target in (block or {}).get("targets", ()) if isinstance(block, Mapping) else ():
        if not isinstance(target, Mapping) or target.get("kind") not in KINDS:
            continue
        try:
            validate_world_interaction_args(dict(target))
        except (KesalahanPerintah, ValueError, TypeError):
            continue
        out.append(dict(target))
    return tuple(out) if len(out) <= 2 and len({t["kind"] for t in out}) == len(out) else ()


def receipt(envelope, args):
    """Validate the independently sampled native readback, not an expected gameplay outcome.

    The outer executor authenticates request/runtime/session and release. Deltas
    are observations over an interval, without causal credit or semantic success.
    """
    if (not isinstance(envelope, Mapping) or args.get("kind") not in KINDS
            or envelope.get("input_args") != args or envelope.get("status") != "completed"
            or envelope.get("cleanup_confirmed") is not True
            or type(envelope.get("press_count")) is not int or envelope["press_count"] != 1
            or envelope.get("outcome") not in (CHANGED, UNCHANGED)):
        return None
    detail = envelope.get("effect_detail")
    if not isinstance(detail, str) or len(detail) > 65536:
        return None
    try:
        delta = json.loads(detail)
        b, a, changes = delta["BeforeTick"], delta["AfterTick"], delta["Changes"]
        if (type(b) is not int or type(a) is not int or a <= b
                or not isinstance(changes, list) or len(changes) > 96
                or bool(changes) != (envelope["outcome"] == CHANGED)):
            return None
        seen, facts = set(), []
        for cell in changes:
            if not isinstance(cell, dict) or set(cell) != {"Path", "Before", "After"}:
                return None
            path = cell["Path"]
            if not isinstance(path, str) or not path or len(path) > 256 or path in seen:
                return None
            seen.add(path)
            values = []
            for k in ("Before", "After"):
                value = cell[k]
                if value is not None and not isinstance(value, str):
                    return None
                values.append(dict(present=value is not None, value=json.loads(value) if value is not None else None))
            if cell["Before"] == cell["After"]:
                return None
            facts.append(dict(path=path, before=values[0], after=values[1]))
        return dict(before_tick=b, after_tick=a, changes=facts, causal_credit="unattributed")
    except (ValueError, KeyError, TypeError):
        return None


def opened_menu(action):
    """A menu opened in a released generic ACTION receipt; no inferred world goal."""
    import arti_stardew_skills as S
    if (S._g(action, "lifecycle") != "completed" or not S._g(action, "request_id")
            or S._g(action, "cleanup_confirmed") is not True
            or S._g(action, "world_effect_status") != "observed_world_delta"):
        return None
    changes = (S._g(action, "after") or {}).get("changes", ())
    for c in changes:
        row = (c.get("after") or {}).get("value")
        if (c.get("path") == "menu" and (c.get("before") or {}).get("value") is None
                and isinstance(row, dict) and row.get("type") and row.get("receiver")):
            return row
    return None


def chest_open(action, state, view):
    """The chest-specific receipt already verifies target, receiver and post-release order."""
    import arti_stardew_skills as S
    pending = state.get("generic_pending_args") or {}
    after = (S._g(action, "after") or {}).get("container_open") or {}
    request = S._g(action, "request_id")
    if (state.get("generic_pressed") and request and request != state.get("generic_previous")
            and S._g(action, "lifecycle") == "completed" and S._g(action, "cleanup_confirmed") is True
            and S._g(action, "world_effect_status") == "observed_container_open"
            and S._g(action, "outcome") == "ContainerOpened"
            and pending.get("kind") == "chest" and after.get("target")
            and after["target"] == pending.get("target_token") and after.get("source")
            and after["source"] == pending.get("source_fact_id")
            and after.get("receiver") == view.receiver_token and view.receiver_token
            and view.menu_type == "ItemGrabMenu" and (view.menu_container or {}).get("kind") == "chest"):
        return dict(request_id=request, **after)
    return None


def remember_readback(runtime, request, args, delta, cleanup):
    """Keep the one world receipt in its existing run while the brain handles a menu.

    UI steps may replace goal.last_action. They must not erase or manufacture the
    original button's evidence. This is run scratch, not another world store.
    """
    session = getattr(runtime, "_skill_session", None)
    run = getattr(session, "current", None)
    if (run is not None and run.goal_id == request.goal_id and args.get("kind") == SKILLS.get(run.call.name)
            and delta is not None and cleanup is True):
        run.state["generic_readback"] = delta
        run.state["generic_receipt_observed"] = True


def inspect_refused(runtime, request, choice):
    """ADR 114: a pre-publication inspect gate did not send the planned button.

    Called only at the runtime's explicit pre-publication refusals: play_inspect_required, and (ADR 231 part E) a
    continuation act discarded before publication. Never infer permission to resend from an absent, delayed or
    uncertain receipt.
    """
    run = getattr(getattr(runtime, "_skill_session", None), "current", None)
    if (run is not None and run.goal_id == request.goal_id
            and getattr(choice, "operation", None) == "world_interact"
            and run.call.name in SKILLS
            and run.state.get("generic_pending_args") == choice.args
            and not run.state.get("generic_receipt_observed")):
        run.state.pop("generic_pressed", None)
        run.state.pop("generic_pending_args", None)


def mod_refusal(last, state):
    """ADR 243: the mod refused the press it was sent (a new receipt, lifecycle ``rejected``, nothing applied). Waiting
    for a readback cannot change that, so the run ends now with the game's own reason for her next page, instead of
    a wait and a generic ``*_unverified`` reason. A press discarded before publication is ``not_taken`` and is re-sent
    by the existing inspect path (ADR 114), never here."""
    import arti_stardew_skills as S
    if (last is None or not S._g(last, "request_id") or S._g(last, "request_id") == state.get("generic_previous")
            or S._g(last, "lifecycle") != "rejected" or S._g(last, "world_effect_status") != "zero_effect"):
        return None
    return S.Step("fail", reason=str(S._g(last, "reason") or "mod_refused")[:160])


def step(call, snap, state, *, context=None):
    """One chosen generic press, then return its observed delta to the brain."""
    import arti_stardew_skills as S
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if last is not None and (S._g(last, "cleanup_confirmed") is False
                             or S._g(last, "lifecycle") in ("uncertain", "interrupted")):
        return S.Step("fail", reason="action_unreconciled")
    if call.name == "aksi":
        from arti_runtime.festival_facts import press as festival_press
        if (hit := festival_press(call, snap, state)) is not None:
            return hit
    if call.name == "pegang_slot":
        return hold_step(call, snap, state)
    if call.name == "langkah":
        last = S._g(S._g(snap, "working_memory"), "last_action")
        if state.get("generic_pressed"):
            if (S._g(last, "request_id") and S._g(last, "request_id") != state.get("generic_previous")
                    and S._g(last, "cleanup_confirmed") is True
                    and S._g(last, "world_effect_status") in ("observed_change", "unchanged")):
                state["raw_effect"] = S._g(last, "world_effect_status")  # ADR 243: her next page says which
                return S.Step("done", reason="observed_movement")
            if (refused := mod_refusal(last, state)) is not None:
                return refused
            # fix58A (ADR 84, a39/a43: langkah 0 finished): the first act of a fresh skill goal is refused
            # before publication (play_inspect_required, nothing written) and the runtime takes one inspect.
            # No new receipt means the move was never sent; send her chosen move again, bounded. A sent move
            # always leaves a new last_action (completed or uncertain), so this can never press twice.
            if (S._g(last, "request_id") == state.get("generic_previous")
                    and state.get("generic_resends", 0) < LANGKAH_RESENDS_MAX):
                state["generic_resends"] = state.get("generic_resends", 0) + 1
                return S._decision(S.act("world_key.move_cardinal", dict(direction=call.arg, ticks=4)))
            return S._wait(state, "move", "movement_unverified", "Amati posisi", S.WAIT_S)
        if call.arg not in ("up", "right", "down", "left"):
            return S.Step("fail", reason="direction_invalid")
        state.update(generic_pressed=True, generic_previous=S._g(last, "request_id"))
        return S._decision(S.act("world_key.move_cardinal", dict(direction=call.arg, ticks=4)))
    if call.name in ("inventori", "cek_jurnal"):
        if state.get("generic_menu_opened") or S.menu_up(snap):
            return S.Step("done", reason="observed_menu_open")
        if state.get("generic_pressed"):
            return S._wait(state, "generic_menu", "menu_open_unverified", "Amati menu", S.WAIT_S)
        button = "Select" if call.name == "cek_jurnal" else "Start"
        if (S._g(snap, "pad_button_offer") or {}).get("button") != button:
            return S.Step("fail", reason="menu_open_not_offered")
        state["generic_pressed"] = True
        return S._decision(S.act("pad_button", dict(button=button)))
    if call.name == "hadap":
        try:
            tile = tuple(int(v) for v in call.arg.split(","))
        except (ValueError, AttributeError):
            return S.Step("fail", reason="tile_unobserved")
        here = S._wave1_xy(S._g(S._g(snap, "motor"), "tile"))
        if len(tile) != 2 or here is None or sum(abs(a-b) for a, b in zip(here, tile)) != 1:
            return S.Step("fail", reason="tile_not_adjacent")
        facing = S._g(S._g(snap, "motor"), "facing")
        direction = ((0, -1), (1, 0), (0, 1), (-1, 0)).index((tile[0]-here[0], tile[1]-here[1]))
        if facing == direction:
            state["raw_effect"] = "facing"
            return S.Step("done", reason="observed_facing")
        return S._decision(S.act("world_key.move_cardinal", dict(direction=("up", "right", "down", "left")[direction], ticks=1)))
    last = S._g(S._g(snap, "working_memory"), "last_action")
    if state.get("generic_pressed"):
        loc = S._g(S._g(snap, "motor"), "location")
        start = state.get("ladder_down_from")
        if start and loc and loc != start and str(loc).startswith("UndergroundMine"):
            after_loc = (S._g(last, "after") or {}).get("location") if last is not None else None
            if after_loc is None or after_loc == loc:
                return S.Step("done", reason="observed_generic_readback")
        if state.get("generic_menu_opened"):
            return S.Step("done", reason="observed_generic_readback")
        if state.get("generic_receipt_observed"):
            return S.Step("done", reason="observed_generic_readback")
        key = S._g(snap, "ui_key_target") or {}
        if (key.get("kind") == "question" and key.get("question_key") in ("Shaft", "ExitMine")
                and not state.get("ladder_down_answered")):
            from arti_stardew_world_extras import ExtrasObjective, plan_question_answer
            view = key if hasattr(key, "options") else None
            if view is None and isinstance(key, Mapping):
                options = key.get("options") or ()
                want = "Jump" if key.get("question_key") == "Shaft" else "Leave"
                match = next((o for o in options if isinstance(o, Mapping) and o.get("response_key") == want), None)
                if match is not None:
                    state["ladder_down_answered"] = True
                    args = dict(key.get("args") or {}, action="answer", option_index=match.get("index", 0))
                    return S._decision(S.act("ui_key", args))
            obj = ExtrasObjective(kind="shaft" if key.get("question_key") == "Shaft" else "exit_mines")
            step = plan_question_answer(obj, view)
            if step.kind == "act" and step.args:
                state["ladder_down_answered"] = True
                return S._decision(S.act("ui_key", step.args))
        if (call.name == "aksi" and (state.get("generic_pending_args") or {}).get("kind") in ("ladder_down", "shaft")
                and S._g(last, "request_id") and S._g(last, "request_id") != state.get("generic_previous")
                and S._g(last, "lifecycle") == "completed" and S._g(last, "cleanup_confirmed") is True
                and S._g(last, "world_effect_status") in ("observed_extras_effect", "observed_location_change")
                and ((S._g(last, "after") or {}).get("kind") in ("ladder_down", "shaft")
                     or S._g(last, "world_effect_status") == "observed_location_change")
                and (S._g(last, "after") or {}).get("location") == loc):
            # Her named ladder ACTION has the existing native descent receipt, not a generic delta.
            return S.Step("done", reason="observed_generic_readback")
        if ((state.get("generic_pending_args") or {}).get("kind") == "shipping_bin" and last is not None
                and S._g(last, "request_id") and S._g(last, "request_id") != state.get("generic_previous")
                and S._g(last, "lifecycle") == "completed" and S._g(last, "cleanup_confirmed") is True
                and S._g(last, "world_effect_status") == "observed_extras_effect" and S._g(last, "outcome") == "MenuOpened"
                and (S._g(last, "after") or {}).get("kind") == "shipping_bin"):
            # ADR 231 part A: the bin answers her ACTION with the mod's own MenuOpened receipt (attempt 87 W12 J4380).
            # Her next pages are its menu, as for a chest she opened.
            state["generic_menu_opened"] = True
            return S.Step("done", reason="observed_generic_readback")
        if (last is not None and S._g(last, "request_id")
                and S._g(last, "request_id") != state.get("generic_previous")
                and S._g(last, "cleanup_confirmed") is True
                and S._g(last, "world_effect_status") in ("observed_world_delta", "unchanged", "observed_container_open")):
            # FIX72D: a chest answers an ACTION with its own receipt, ContainerOpened. Her next pages are its menu.
            if S._g(last, "world_effect_status") == "observed_container_open":
                state["generic_menu_opened"] = True
            return S.Step("done", reason="observed_generic_readback")
        if (refused := mod_refusal(last, state)) is not None:
            return refused
        return S._wait(state, "generic", "generic_readback_unavailable", "Amati perubahan setelah tombol", S.WAIT_S)
    kind = SKILLS[call.name]
    if call.name == "aksi" and call.tile is not None and call.arg:
        # Named native target: approach/facing only. The press still requires a freshly published target.
        observed = next((t for t in S._g(snap, "ordinary_interactions", ()) if t.get("ref") == call.arg), None)
        if observed is None or tuple(observed.get("tile", ())) != tuple(call.tile):
            return S.Step("fail", reason="interact_target_disappeared")
        tile = tuple(call.tile)
        here = S._wave1_xy(S._g(S._g(snap, "motor"), "tile"))
        if observed.get("kind") in ("ladder_down", "shaft"):
            state.setdefault("ladder_down_from", S._g(S._g(snap, "motor"), "location"))
        ladder = observed.get("kind") in ("ladder_down", "ladder_up", "shaft")
        if observed.get("kind") == "ladder_up" and context is not None:
            from arti_runtime.mines_exit import ups, pose_step
            from arti_stardew_skills_mines import exit_route_step
            entries = [e for e in ups(context, S._g(snap.motor, "location")) if e["ref"] == call.arg]
            if not entries:
                return S.Step("fail", reason="mines_exit_not_observed")
            combat = context.get("combat") or {}
            route = state.setdefault("exit_route", {})
            hp = combat.get("hp")
            native = S._g(snap, "world_interaction_target") or {}
            if native.get("kind") != "ladder_up":
                if type(hp) is int and route.get("hp") is not None and hp < route["hp"]:
                    # ADR 174: falling HP keeps the up-ladder walk going (the guarded step flees contact).
                    route["hp_falling_steps"] = route.get("hp_falling_steps", 0) + 1
                route["hp"] = hp
                if "monsters" not in combat or not context.get("nav_grid"):
                    return S.Step("fail", reason="mines_exit_threat_geometry_unknown")
                walk = exit_route_step(snap, context["nav_grid"], entries, route, combat=combat)
                if walk is not None:
                    return walk
        if ladder:
            hand = S.plain_hand_step(snap, state)
            if hand is not None:
                return hand
        offered_target = S._g(snap, "world_interaction_target")
        matches = lambda t: isinstance(t, Mapping) and (t.get("tile_x"), t.get("tile_y")) == tile
        native = next((t for t in S._g(snap, "generic_world_targets", ()) if t.get("kind") == kind and matches(t)), None)
        if (matches(offered_target) and offered_target.get("kind") == "chest"
                and offered_target.get("source_fact_id") == call.arg):
            native = offered_target
        elif native is None and observed.get("kind") not in ("map_action", "scene_pick_up") and matches(offered_target):
            native = offered_target
        if native is None and observed.get("kind") == "shipping_bin":
            from scripts.live7.interactions import bin_capture_tile
            if bin_capture_tile(call.arg, offered_target) is not None:
                native = offered_target  # ADR 231 part A: the mod's faced capture of this bin, either of its two tiles
        if native is not None and observed.get("kind") in ("chest", "shipping_bin"):
            # The mod refuses A on a chest with a rod or an object in hand (ADR 98); the bin takes an object in hand.
            hand = S.plain_hand_step(snap, state, allow_object=observed.get("kind") == "shipping_bin")
            if hand is not None:
                return hand
        if native is not None:
            state.update(generic_pressed=True, generic_previous=S._g(last, "request_id"), generic_pending_args=dict(native))
            return S._decision(S.act("world_interact", dict(native)))
        if observed.get("kind") in ("map_action", "scene_pick_up"):
            approach = S._approach(snap, lambda t: t.get("ref") == call.arg)
            if approach is not None and S._arrived_ref(snap) != call.arg:
                return S._decision(approach)
            return S._wait(state, "map_action", "map_action_native_press_unavailable", "Amati tombol ACTION", S.WAIT_S)
        if observed.get("kind") == "shipping_bin":
            # ADR 210: approach owns its bounded grab flush; repeated one-tick
            # turns cannot get this building's grab pose onto the target.
            if S._arrived_ref(snap) == call.arg:
                return S._wait(state, "bin_open", "shipping_bin_native_open_not_offered",
                               "Amati tawaran pembuka native untuk kotak pengiriman ini", S.WAIT_S)
            entry = next((t for t in S._g(snap, "approach_targets", ()) if t.get("ref") == call.arg), None)
            if entry is not None:
                return S._decision(S.act("approach_target", {}, call.arg))
            return S.Step("fail", reason="shipping_bin_native_open_not_offered")
        if observed.get("kind") == "ladder_up" and context is not None:
            return pose_step(snap, entries[0], state)
        if ladder:
            # ADR 146: ARRIVED/facing is not the native grab pose. Let the offered approach finish its flush.
            entry = next((t for t in S._g(snap, "approach_targets", ()) if t.get("ref") == call.arg), None)
            if entry is not None:
                return S._decision(S.act("approach_target", {}, call.arg))
        turn = S._face(snap, tile) if here is not None and sum(abs(a-b) for a,b in zip(here,tile)) == 1 else None
        if turn is not None:
            return S._decision(turn)
        approach = S._approach(snap, lambda t: t.get("ref") == call.arg)
        if approach is not None:
            return S._decision(approach)
        if observed.get("kind") in ("ladder_down", "shaft"):
            from arti_runtime.mines_down import unreachable
            detail = unreachable(snap, context, observed, here)
            state["fail_detail"] = detail
            return S.Step("fail", reason=detail["reason"])
        # Gifts use the same native navigation grid as any other observed object.
        # A named tile is a location fact, never authorization for a blind press.
        return S.Step("fail", reason="interact_target_not_reachable_or_not_offered")
    if call.name == "aksi" and call.arg == "depan" and (hand := S.plain_hand_step(snap, state)) is not None:
        # ADR 243 SAFETY: her raw A is pressed with a plain hand. The mod's generic A has no pad denylist, so an object
        # in hand facing a villager would be a gift without confirmation (or eaten, or placed).
        return hand
    target = next((t for t in S._g(snap, "generic_world_targets", ()) if t.get("kind") == kind), None)
    faced = S._g(snap, "world_interaction_target") or {}
    if (call.name == "aksi" and target is not None and faced.get("kind") == "chest"
            and all(target[key] == faced.get(key) for key in ("tile_x", "tile_y", "actor_tile_x", "actor_tile_y", "facing"))):
        target = faced  # retain the native chest's target-specific receipt for the same faced ACTION
    if target is None and (call.name == "pakai" or call.arg == "depan"):
        # ADR 243: her raw hand waits (bounded) for the mod's fresh target of the faced tile; a lease or freshness
        # miss is bookkeeping, so it is reported with its reason when the wait ends, not refused at the first read.
        return S._wait(state, "generic_target", "generic_button_not_offered", "Amati tombol di petak depan", S.WAIT_S)
    if target is None:
        return S.Step("fail", reason="generic_button_not_offered")
    state.update(generic_pressed=True, generic_previous=S._g(last, "request_id"), generic_pending_args=dict(target))
    return S._decision(S.act("world_interact", dict(target)))
