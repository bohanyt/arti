"""ADR 177/179: every walk out of a Mines floor is one mod leg at game speed.

When the mod offers ``disengage_available`` the exit controller (``pergi_ke mine``, the named up action ``aksi
xt:mine:*``, the contact guard and the post-fight exit all reach it) hands the walk to the up-ladder stand to the mod's
leg (``reflex_combat`` with ``disengage=true``): a nav-grid path held in long straight holds, re-planned only at
turns or when blocked, defending only when cornered with an attacker in contact and the weapon held. The runtime keeps
what it already owns: the native up press, the Leave answer and the location readback.

Cornered (ADR 179): a leg that ends with no way (``no_route_to_exit`` / a stone) or an exhausted alternate route runs
the cornered rule for real: ADR 176 ``route_clear_step`` when the native facts name a breakable closure, else ADR 169's
escape fight against the attacker in contact (weapon staged by the existing combat rule), else a named fact. Every such
ending journals one bounded floor record (``mines_floor_record``) so the next analysis reads the floor.
"""
from arti_stardew_skills_mines import CONTACT_RADIUS_TILES

DISENGAGE_LEGS_MAX = 3   # mod legs per exit leg; each is bounded by REFLEX_COMBAT_DISENGAGE_MAX_S (30 s)
REFLEX_RADIUS_TILES = 8  # the mod's reflex radius (ReflexContracts.CombatRadiusTilesMax)
ROUTE_CLEAR_CAPS = (48, 40, 180)  # cumulative (decisions, inputs, seconds) of the run: the skill budget, never enlarged
DEFEND_RADIUS_TILES = 2.0  # an attacker this close to a cornered body is the one it defends against
FLOOR_RECORD_RADIUS = 12   # ADR 179's old radius (kept for tests and readers)
FLOOR_RECORD_MARGIN = 3    # ADR 180: tiles around the box of her, the attackers and the way up
FLOOR_RECORD_MAX = 64      # ADR 180: at most 64 tiles per side
MOD_GUARD_FALLBACK_S = 10.0  # ADR 180: a failed mod guard lets the runtime row interrupt act for this long

REASONS = {
    "no_route_to_exit": "tidak ada jalan terbuka ke tangga naik",
    "disengage_stuck": "badan tidak maju di jalan ke tangga",
    "disengage_timeout": "waktu jalan keluar habis",
    "exit_lost": "tangga naik tidak terbaca lagi",
}


def near(combat, radius=REFLEX_RADIUS_TILES):
    """Live monsters within ``radius``, nearest first."""
    out = []
    for m in (combat or {}).get("monsters") or ():
        d = m.get("distance") if isinstance(m, dict) else None
        if (isinstance(d, (int, float)) and not isinstance(d, bool) and d <= radius
                and not (type(m.get("hp")) is int and m["hp"] <= 0)):
            out.append(m)
    return sorted(out, key=lambda m: m["distance"])


def _note(runtime, goal, leg, status, **extra):
    runtime._journal(dict(ev="mines_body", goal_id=goal.goal_id, status=status, legs=leg.get("disengage_legs", 0),
                          **extra))


def reason_text(objective):
    """Her page's words for how a leg ended, from the native verdict/detail (never the objective's generic reason)."""
    detail = str(objective.last_detail or "")
    if objective.status == "RETREATED":
        defends = (objective.leg or {}).get("defends") or 0
        return "bertahan lalu jalan ke tangga naik" if defends else "jalan ke tangga naik"
    import arti_stardew_combat as C
    rock = C.route_blocked_rock(detail)
    if rock is not None:
        return "jalan keluar terhenti: batu menutup jalan di %d,%d" % rock
    for code, text in REASONS.items():
        if code in detail:
            return "jalan keluar terhenti: " + text
    return "jalan keluar terhenti: " + (detail or str(objective.last_verdict or "hasil belum terbaca"))


def body_fact(runtime, objective, location, combat=None):
    """Keep what the body did, with native HP numbers and the attacker, for her next page (ADR 174 memory)."""
    from arti_runtime import mines_contact
    leg = objective.leg or {}
    mem = mines_contact.memory(runtime)
    act = dict(mem.get("last_act") or {})
    attackers = mines_contact.attackers(combat) or act.get("attackers") or []
    hp_from, hp_to = leg.get("hp_start"), leg.get("hp_end")
    # ADR 181: the floor is where this leg settled now, never an older act's floor (a73 page 010).
    act.update(location=location or act.get("location"), action=reason_text(objective), body_leg=objective.last_verdict,
               attackers=attackers[:3])
    if type(hp_from) is int and type(hp_to) is int:
        act.update(hp_from=max(hp_from, act.get("hp_from") if type(act.get("hp_from")) is int else hp_from), hp=hp_to)
    act.pop("shown_after", None)
    act["visit_seq"] = mem.get("visit_seq", 0)  # ADR 183: the visit it settled in
    mem["last_act"] = act


def floor_record(runtime, context, location, reason, *, here=None):
    """ADR 179: one bounded journal snapshot of the local floor for the next analysis (never on her page).

    Rows of the native nav grid around her ('.' open, 's' breakable stone, '#' solid, '?' unread), her tile, monsters
    with kind/HP/tile, up-ladder stands, and the native debris pieces nearby with the tool that breaks them."""
    grid = context.get("nav_grid") or {}
    here = tuple(here or context.get("actor_tile") or ())
    if len(here) != 2:
        return
    combat = context.get("combat") or {}
    from arti_runtime.mines_exit import ups as _ups
    from arti_stardew_skills import _wave1_xy
    points = [here] + [p for p in (_wave1_xy(m.get("tile")) for m in (combat.get("monsters") or ())[:8]
                                   if isinstance(m, dict)) if p is not None]
    points += [tuple(s) for e in _ups(context, location, require_ready=False)
               for s in (e.get("stands") or [[e.get("x"), e.get("y")]]) if all(type(v) is int for v in s)]
    # ADR 180 (a72 J4023: her tile 24,18, the stand 4,5 outside a 25 x 25 window): the window is the bounding box of
    # her, the attackers and the way up plus FLOOR_RECORD_MARGIN, at most FLOOR_RECORD_MAX per side (around her).
    x0, x1 = min(p[0] for p in points) - FLOOR_RECORD_MARGIN, max(p[0] for p in points) + FLOOR_RECORD_MARGIN
    y0, y1 = min(p[1] for p in points) - FLOOR_RECORD_MARGIN, max(p[1] for p in points) + FLOOR_RECORD_MARGIN
    if x1 - x0 + 1 > FLOOR_RECORD_MAX:
        x0 = max(x0, here[0] - FLOOR_RECORD_MAX // 2)
        x1 = x0 + FLOOR_RECORD_MAX - 1
    if y1 - y0 + 1 > FLOOR_RECORD_MAX:
        y0 = max(y0, here[1] - FLOOR_RECORD_MAX // 2)
        y1 = y0 + FLOOR_RECORD_MAX - 1
    r = max(x1 - x0, y1 - y0)
    rows = ["".join(str(grid.get((x, y), "?"))[:1] for x in range(x0, x1 + 1)) for y in range(y0, y1 + 1)]
    monsters = [dict(type=m.get("type"), hp=m.get("hp"), tile=m.get("tile"), distance=m.get("distance"))
                for m in (combat.get("monsters") or ())[:8] if isinstance(m, dict)]
    from arti_runtime.mines_exit import ups
    from arti_stardew_world_extras import stand_tiles
    exits = [dict(ref=e["ref"], tile=[e.get("x"), e.get("y")], stands=[list(s) for s in (stand_tiles(e) or ())])
             for e in ups(context, location, require_ready=False)][:4]
    pieces = []
    try:
        from arti_stardew_debris_route import debris_kind
        for ref, f in (context.get("targets") or {}).items():
            tile = (f.get("tile") or {}) if isinstance(f, dict) else {}
            tx, ty = tile.get("x"), tile.get("y")
            if type(tx) is int and type(ty) is int and x0 <= tx <= x1 and y0 <= ty <= y1:
                kind = debris_kind(f)
                if kind:
                    pieces.append(dict(kind=kind, tile=[tx, ty], name=f.get("name"),
                                       tool={"stone": "Pickaxe", "boulder": "Pickaxe", "meteorite": "Pickaxe",
                                             "tree": "Axe", "stump": "Axe", "hollow_log": "Axe", "twig": "Axe",
                                             "weeds": "Scythe"}.get(kind)))
                    if len(pieces) >= 24:
                        break
    except Exception:  # noqa: BLE001 - a diagnostic record never blocks the body
        pieces = None
    runtime._journal(dict(ev="mines_floor_record", reason=str(reason)[:80], location=location, tile=list(here),
                          origin=[x0, y0], rows=rows, monsters=monsters, exits=exits,
                          pieces=pieces, hp=combat.get("hp"), max_hp=combat.get("max_hp")))


def route_clear(runtime, run, goal, request, leg, location):
    """ADR 176's entry from the exit: the next clearing answer, or None with a named blocker / no repair."""
    from arti_stardew_skills import INSPECT
    if run is None or not hasattr(runtime, "route_clear_step"):
        return None
    from arti_runtime.route_clear import clearing_reaches
    from arti_runtime.mines_exit import ups
    context = runtime._skill_wave1_context(request.snapshot)
    step = runtime.route_clear_step(run, goal, request, ROUTE_CLEAR_CAPS,
                                    reaches=clearing_reaches(context, ups(context, location)))
    if step is None:
        leg.pop("route_clearing", None)
        return None
    _note(runtime, goal, leg, "route_clear", step=step.kind, reason=step.reason or None)
    if step.kind == "decision":
        leg["route_clearing"] = True
        return runtime._rule_json(request, step.choice, "mines_exit", "route_clear")
    leg.pop("route_clearing", None)
    if step.kind == "fail":
        goal.extras_objective.blocker = "mines_exit_route_clear_failed:" + str(step.reason)
        return None
    # Removal verified or a clearing leg selected: the next fresh read re-plans the exit from the new grid.
    return runtime._rule_json(request, INSPECT, "mines_exit", "route_clear_reobserve")


def cornered(runtime, run, goal, request, leg, location, why):
    """ADR 179: no way out was found. Break through, else defend, else a named fact. Never a page with a wrong reason."""
    context = runtime._skill_wave1_context(request.snapshot)
    floor_record(runtime, context, location, "cornered:" + str(why))
    raw = route_clear(runtime, run, goal, request, leg, location)
    if raw is not None:
        _note(runtime, goal, leg, "cornered", action="route_clear", why=why)
        return raw
    if goal.extras_objective.blocker:
        return None
    combat = context.get("combat") or {}
    if near(combat, DEFEND_RADIUS_TILES) and not leg.get("cornered_defend_done"):
        from arti_runtime.mines_survival import escape_invoke
        leg["cornered_defend_done"] = True
        _note(runtime, goal, leg, "cornered", action="defend", why=why,
              attackers=[[m.get("type"), m.get("hp"), m.get("distance")] for m in near(combat, DEFEND_RADIUS_TILES)[:2]])
        return escape_invoke(runtime, goal, request, leg)
    import arti_stardew_combat as C
    rock = C.route_blocked_rock(why)
    goal.extras_objective.blocker = ("mines_exit_route_needs_clear:%d,%d" % rock if rock is not None
                                     else "mines_exit_no_route" if "no_route" in str(why) else "mines_exit_cornered:" + str(why))
    _note(runtime, goal, leg, "cornered", action="named_fact", why=why)
    return None


def mod_guard_failed_recently(runtime, location):
    """ADR 180: the mod guard failed on this floor within MOD_GUARD_FALLBACK_S (the runtime's row interrupt acts)."""
    import time as _time
    from arti_runtime import mines_contact
    f = mines_contact.memory(runtime).get("mod_guard_failed")
    return bool(f and f.get("location") == location and _time.monotonic() - f["at"] <= MOD_GUARD_FALLBACK_S)


def body_guard_seen(runtime, combat, location, context=None):
    """ADR 179: the mod's own leg (combat.body_guard) reaches the journal and her next page, with native numbers."""
    from arti_runtime import mines_contact
    from collections.abc import Mapping as _Mapping
    fact = combat.get("body_guard") if isinstance(combat, _Mapping) else None
    if not isinstance(fact, _Mapping) or type(fact.get("seq")) is not int:
        return None
    mem = mines_contact.memory(runtime)
    if mem.get("body_guard_seq") == fact["seq"]:
        return None
    mem["body_guard_seq"] = fact["seq"]
    reflex = fact.get("reflex") if isinstance(fact.get("reflex"), _Mapping) else {}
    runtime._journal(dict(ev="mines_body_guard", status=fact.get("status"), verdict=fact.get("verdict"),
                          location=fact.get("location"), hp_from=fact.get("hp_from"), hp=fact.get("hp"),
                          tick=fact.get("tick"), drop_tick=fact.get("drop_tick"), starts=fact.get("starts"),
                          route=reflex.get("disengage_route"), defends=reflex.get("defends"), detail=reflex.get("detail"),
                          # ADR 181: reaction and speed from the journal alone.
                          start_tick=reflex.get("start_tick"), first_press_tick=reflex.get("first_press_tick"),
                          ticks=reflex.get("ticks"), tiles_moved=reflex.get("tiles_moved"),
                          hp_start=reflex.get("hp_start"), hp_end=reflex.get("hp_end")))
    verdict_now = str(fact.get("verdict") or "")
    if (fact.get("status") == "settled" and not verdict_now.startswith("Retreated")) or str(fact.get("status", "")).startswith("refused"):
        # ADR 180 (a72 SMAPI 11226-11236 + J3901-J4024): the mod's leg could not move; the runtime acts at once.
        import time as _time
        mem["mod_guard_failed"] = dict(at=_time.monotonic(), location=fact.get("location") or location,
                                       verdict=verdict_now or fact.get("status"), detail=reflex.get("detail"))
        runtime._journal(dict(ev="mines_body_guard_failed", verdict=verdict_now or fact.get("status"),
                              detail=reflex.get("detail"), location=fact.get("location") or location,
                              action="runtime_turn"))
        if context is not None:
            floor_record(runtime, context, fact.get("location") or location, "mod_guard:" + (verdict_now or str(fact.get("status"))))
    import time as _t
    if fact.get("status") == "started":
        mem["guard_started_at"], mem["guard_started_seq"] = _t.monotonic(), fact["seq"]
    elif fact.get("status") == "settled":
        mem.pop("guard_started_at", None)
        if verdict_now.startswith("Retreated") and "at_exit" in str(reflex.get("detail") or ""):
            # ADR 185: on the stand with the way up under her feet; the runtime's exit presses up now (no page).
            mem["at_stand_turn"] = dict(at=_t.monotonic(), location=fact.get("location") or location)
            runtime._journal(dict(ev="mines_body_guard_at_stand", location=fact.get("location") or location,
                                  hp=fact.get("hp"), action="runtime_up_press"))
    if fact.get("status") in ("started", "settled"):
        import arti_stardew_combat as C
        verdict = str(fact.get("verdict") or "")
        settled = C.CombatObjective("guard", disengage=True,
                                    status="RETREATED" if verdict.startswith("Retreated") else "BLOCKED",
                                    last_verdict=verdict or None, last_detail=str(reflex.get("detail") or "") or None,
                                    leg=dict(hp_start=fact.get("hp_from") if type(fact.get("hp_from")) is int and fact["hp_from"] >= 0
                                             else reflex.get("hp_start"), hp_end=fact.get("hp"),
                                             defends=reflex.get("defends")))
        if fact.get("status") == "started":
            settled = C.CombatObjective("guard", disengage=True, status="RETREATED", leg=dict(
                hp_start=settled.leg["hp_start"], hp_end=fact.get("hp")))
        body_fact(runtime, settled, fact.get("location") or location, combat)
        if fact.get("status") == "started":
            mem["last_act"]["action"] = "menjauh dan jalan ke tangga naik (tubuh sendiri)"
    return fact


GUARD_FACT_FRESH_S = 5.0  # ADR 183: a ui.context read older than this cannot say the guard is running


GUARD_HOLD_WINDOW_S = 35.0  # ADR 185: after a seen guard start, busy until its settle (leg bound 30 s + margin)


def body_guard_running(runtime):
    """ADR 183/185: the mod's body guard holds her input now.

    True when the fresh ui.context ``combat.body_guard`` says ``started``, or when this runtime journaled a guard
    start (``body_guard_seen``) within GUARD_HOLD_WINDOW_S and has not seen its settle: an unknown or stale read
    after a start is busy, never free. Facts are read-only mappings (MappingProxyType), never only dicts.
    (a75 J1415: the fix103B predicate required ``dict`` and was always False.)"""
    import time as _time
    from collections.abc import Mapping as _Mapping
    from arti_runtime import mines_contact
    mem = mines_contact.memory(runtime)
    seen = mem.get("guard_started_at")
    held = seen is not None and _time.monotonic() - seen <= GUARD_HOLD_WINDOW_S
    try:
        rec = runtime._obs.get(runtime._binding_view()[1], "ui.context")
        facts = getattr(rec, "facts", None)
        received = getattr(rec, "received_monotonic", None)
        combat = facts.get("combat") if isinstance(facts, _Mapping) else None
        fact = combat.get("body_guard") if isinstance(combat, _Mapping) else None
        fresh = type(received) in (int, float) and 0 <= _time.monotonic() - received <= GUARD_FACT_FRESH_S
        if isinstance(fact, _Mapping) and fresh:
            if type(fact.get("seq")) is int and fact["seq"] != mem.get("body_guard_seq"):
                # ADR 189 (rule 3): the predicate itself takes the native fact into memory at cleanup/settle time, so a
                # start one poll old (never journaled by a page build yet) is busy and its stale reads stay busy.
                body_guard_seen(runtime, combat, fact.get("location"))
            if fact.get("status") == "started":
                mem["guard_busy_at"] = _time.monotonic()
                return True
            if fact.get("status") == "settled" and fact.get("seq") == mem.get("guard_started_seq"):
                return False  # its own settle: the hold is over
    except Exception:  # noqa: BLE001 - unreadable: fall back to the journaled start window
        pass
    if held:
        mem["guard_busy_at"] = _time.monotonic()
    return held


def note_body_busy(runtime, skill, site):
    """ADR 183: one journal row per site while the guard runs: nothing started, the caller waits and re-observes."""
    import time as _time
    from arti_runtime import mines_contact
    mem = mines_contact.memory(runtime)
    key = ("body_busy", site, skill)
    last = mem.get(key)
    now = _time.monotonic()
    if last is None or now - last > 5.0:
        mem[key] = now
        runtime._journal(dict(ev="body_guard_busy", site=site, skill=skill, action="not_taken_wait"))


def _offered(ui):
    import arti_stardew_combat as C
    caps = ui.get("capabilities") if isinstance(ui.get("capabilities"), dict) else {}
    cap = caps.get(C.OPERATION_COMBAT)
    return isinstance(cap, dict) and cap.get("disengage_available") is True


def disengage_step(runtime, goal, request, leg, ui, run=None):
    """The exit's next answer: a mod leg, its settlement, the cornered rule, or None (the controller's own step)."""
    import arti_stardew_combat as C
    from arti_stardew_skills import INSPECT
    combat = ui.get("combat") or {}
    objective = getattr(goal, "combat", None)
    motor = getattr(request.snapshot, "motor", None)
    location = combat.get("location") or getattr(motor, "location", None)
    here = tuple(getattr(motor, "tile", None) or ())
    if leg.get("route_clearing") and not (objective is not None and objective.disengage):
        raw = route_clear(runtime, run, goal, request, leg, location)
        if raw is not None or goal.extras_objective.blocker:
            return raw
    if objective is not None and objective.disengage:
        if objective.status == "OPEN" and objective.calls == 0 and objective.last_verdict != "refused":
            picked = C.combat_auto_choice(objective, request.snapshot, approach_progress=goal.approach_progress)
            if picked is None or picked[0] != C.OPERATION_COMBAT:
                reads = leg.get("disengage_missing_reads", 0) + 1
                leg["disengage_missing_reads"] = reads
                if reads >= 3:
                    goal.combat = None
                    leg["disengage_unavailable"] = True
                    _note(runtime, goal, leg, "unavailable")
                    return None  # the guarded short steps remain (older path, still moving)
                return runtime._rule_json(request, INSPECT, "mines_exit", "disengage_refresh")
            operation, ref, args, label = picked
            leg["disengage_legs"] = leg.get("disengage_legs", 0) + 1
            _note(runtime, goal, leg, "leg", args=dict(args))
            return runtime._rule_json(request, dict(kind="act", operation=operation, target_ref=ref, args=args,
                                                    evidence_refs=[]), "mines_exit", "disengage")
        if objective.status == "OPEN" and objective.last_verdict == "refused":
            goal.combat = None
            refusals = leg.get("disengage_refusals", 0) + 1
            leg["disengage_refusals"] = refusals
            _note(runtime, goal, leg, "refused", detail=objective.last_detail, n=refusals)
            if refusals < 3:
                # ADR 179: the mod's own body guard may hold the slot right now (its leg is the same walk); re-read.
                return runtime._rule_json(request, INSPECT, "mines_exit", "disengage_refused_reread")
            leg["disengage_unavailable"] = True
            return None
        # Settled: one leg ran.
        body_fact(runtime, objective, location, combat)
        goal.combat = None
        leg.pop("walk_hp", None)
        leg.pop("route", None)
        detail = str(objective.last_detail or "")
        _note(runtime, goal, leg, "settled", verdict=objective.last_verdict, detail=objective.last_detail,
              vitals=objective.leg)
        if objective.status == "RETREATED":
            # On the stand facing the ladder: the next fresh read offers the native up press. No new leg here.
            if len(here) == 2:
                leg["leg_stand_tile"] = list(here)
            return runtime._rule_json(request, INSPECT, "mines_exit", "disengage_at_stand")
        if objective.status == "DOWNED":
            return None
        if C.route_blocked_rock(detail) is not None or "no_route_to_exit" in detail:
            return cornered(runtime, run, goal, request, leg, location, detail)
        if "stuck" in detail or "timeout" in detail:
            runtime_context = runtime._skill_wave1_context(request.snapshot)
            floor_record(runtime, runtime_context, location, "leg:" + detail, here=here or None)
        return None  # the next read starts another bounded leg or the controller's own step
    if len(here) == 2 and leg.get("leg_stand_tile") == list(here):
        return None  # on the stand after a leg: the controller's pose/press owns the ladder, not another leg
    if (leg.get("disengage_unavailable") or leg.get("disengage_legs", 0) >= DISENGAGE_LEGS_MAX or not _offered(ui)):
        return None  # an older mod without the leg keeps the guarded short steps
    goal.combat = C.CombatObjective(goal.goal_id, kind=C.KIND_MONSTER, disengage=True)
    _note(runtime, goal, leg, "started", near=[[m.get("type"), m.get("distance")] for m in near(combat)[:3]],
          contact=any(m["distance"] <= CONTACT_RADIUS_TILES for m in near(combat)))
    return runtime._rule_json(request, INSPECT, "mines_exit", "disengage_refresh")
