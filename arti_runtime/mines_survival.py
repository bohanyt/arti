"""ADR 169: one escape assessment over the current native floor facts."""
from types import SimpleNamespace as NS


def meal(context):
    from arti_stardew_skills_mines import eat_unsafe
    ui = context.get("mines_ui") or context
    if eat_unsafe(context.get("combat")):
        return False
    if ui.get("eat_candidates"):
        return True
    if ui.get("eat_backpack_candidates"):
        # The existing inventory stage has stricter guards than a toolbar
        # meal. A food item that cannot be staged must not veto the exit.
        import arti_stardew_combat as C
        combat = context.get("combat") or {}
        return (C.weapon_stage_unsafe(dict(ui, combat=combat), None) is None
                and type(combat.get("time_of_day")) is int and combat["time_of_day"] < C.MINES_LEAVE_TIME)
    return False


def assessment(context, location):
    """No input authority: choose the next existing controller, never a tile.

    Incomplete reads remain reads. A remote monster cannot retire repair.
    """
    from arti_stardew_skills_mines import exit_route_step, _live_within, CONTACT_RADIUS_TILES
    from arti_runtime.mines_exit import ups
    combat = context.get("combat") or {}
    if not context.get("ui_complete") or "monsters" not in combat:
        return "observe"
    if meal(context):
        return "eat"
    entries = ups(context, location)
    here = context.get("actor_tile")
    if here is None or not entries or not context.get("nav_grid"):
        return "observe"
    snap = NS(motor=NS(location=location, tile=here, pixel=context.get("actor_pixel"),
                       status="current", is_player_free=True, can_player_move=True))
    route = exit_route_step(snap, context["nav_grid"], entries, {}, combat=combat)
    if route is None or route.kind == "decision":
        return "exit"
    if route.reason == "mines_exit_monster_corridor":
        return "fight"
    if _live_within(combat, CONTACT_RADIUS_TILES):
        return "fight"  # cannot clear debris safely while in contact
    return "repair"


def escape_offered(context, location):
    combat = context.get("combat") or {}
    return (str(location or "").startswith("UndergroundMine")
            and type(combat.get("hp")) is int and combat["hp"] > 0
            and assessment(context, location) == "fight")


def escape_invoke(runtime, goal, request, leg):
    """One existing combat objective, nested in the existing exit budget.

    The kill is consumed here, never as an exit/skill finish. No fresh goal or
    renewed input allowance. Every slice needs the native escape capability.
    """
    import arti_stardew_combat as C
    objective = getattr(goal, "combat", None)
    if objective is None or not objective.escape:
        goal.combat = C.CombatObjective(goal.goal_id, kind=C.KIND_MONSTER, escape=True)
        leg["escape_combat"] = True
        runtime._journal(dict(ev="mines_escape_fight", status="started", goal_id=goal.goal_id))
        from arti_stardew_skills import INSPECT
        return runtime._rule_json(request, INSPECT, "mines_exit", "escape_refresh")
    if objective.status == "KILLED":
        runtime._journal(dict(ev="mines_escape_fight", status="killed", kills=objective.kills,
                              request_id=objective.killed_request_id, goal_id=goal.goal_id))
        goal.combat = None
        leg.pop("escape_combat", None)
        leg.pop("walk_hp", None)
        leg.pop("route", None)  # the killed monster changed the route, not its budget
        return None
    if not C.combat_open(objective) or objective.calls >= C.MAX_COMBAT_CALLS:
        goal.extras_objective.blocker = "mines_escape_fight_stopped:" + objective.reason
        leg.pop("escape_combat", None)
        return None
    picked = C.combat_auto_choice(objective, request.snapshot, approach_progress=goal.approach_progress)
    if picked is None:
        # The last swing's UsingTool/knockback can outlive its released input.
        # Reuse the exit's existing control-read bound, not a two-frame veto.
        from arti_stardew_world_extras_runtime import WorldExtrasRuntimeMixin
        reads = leg.get("escape_missing_reads", 0) + 1
        leg["escape_missing_reads"] = reads
        if reads >= WorldExtrasRuntimeMixin.MINES_EXIT_CONTROL_WAITS:
            goal.extras_objective.blocker = "mines_escape_native_unavailable"
            return None
        from arti_stardew_skills import INSPECT
        return runtime._rule_json(request, INSPECT, "mines_exit", "escape_native_refresh")
    leg.pop("escape_missing_reads", None)
    operation, ref, args, label = picked
    return runtime._rule_json(request, dict(kind="act", operation=operation, target_ref=ref,
                                           args=args, evidence_refs=[]), "mines_exit", label)


def risk_fact(context, history=None):
    """Facts only; missing native data is explicitly unknown, never a weapon table."""
    combat = context.get("combat") or {}
    weapon = combat.get("weapon_stats") or {}
    damage = ("%s–%s" % (weapon["min_damage"], weapon["max_damage"])
              if type(weapon.get("min_damage")) is int and type(weapon.get("max_damage")) is int
              else "belum terbaca")
    food = (context.get("mines_ui") or context).get("eat_candidates") or []
    backpack = (context.get("mines_ui") or context).get("eat_backpack_candidates") or []
    food_text = (", ".join(str(f.get("name") or f.get("qualified_item_id") or "makanan")
                            for f in food + backpack) if food or backpack else
                 "tidak ada makanan di tas" if combat.get("food_slot") == -1 else "belum terbaca")
    line = "Pegangan: %s; damage native %s. HP %s/%s. Makanan: %s." % (
        combat.get("held_name") or weapon.get("name") or combat.get("weapon") or "belum terbaca", damage,
        combat.get("hp", "?"), combat.get("max_hp", "?"), food_text)
    for owned in combat.get("weapons") or ():
        line += " Senjata di tas: %s; damage native %s–%s." % (owned.get("name", "?"),
                    owned.get("min_damage", "?"), owned.get("max_damage", "?"))
    if (food or backpack) and not meal(context):
        line += (" Makan belum tersedia dari kondisi ini: kontak menghalangi makan; tahap makanan ransel "
                 "memerlukan HP di atas 40%, tanpa monster dalam 8 tile, dan sebelum 24:00. "
                 "Makan dinilai lagi saat syarat native/tahap terpenuhi; jalan keluar tetap dinilai.")
    if history:
        line += " Pertemuan terakhir: " + history + "."
    if (ko := combat.get("last_knockout")):
        line += " KO native: bangun di %s; uang hilang %s; item hilang %s." % (
            ko.get("location", "belum terbaca"), ko.get("money_lost", "belum terbaca"),
            ko.get("items_lost", "belum terbaca"))
    return line


def observe(policy, facts, context):
    """Retain only encountered combat/loss facts in the existing day policy."""
    combat = context.get("combat") or {}
    seen = [m for m in combat.get("monsters", ()) if isinstance(m, dict)]
    if seen:
        policy.mines_last_encounter = "; ".join("%s HP%s" % (m.get("type", "?"), m.get("hp", "?"))
                                                     for m in seen[:3])
    count = combat.get("times_unconscious")
    old = getattr(policy, "mines_native_baseline", None)
    if type(count) is int:
        if old and count > old["count"]:
            money = context.get("money")
            loss = (max(0, old["money"] - money) if type(old["money"]) is int and type(money) is int else "belum terbaca")
            items = {q: n-facts.inventory.get(q, 0) for q, n in old["inventory"].items()
                     if n > facts.inventory.get(q, 0)} if facts.inventory_current else "belum terbaca"
            policy.mines_ko_fact = "KO native: bangun di %s; uang berkurang %s; item berkurang %s (selisih sejak bacaan sebelum KO)." % (facts.location, loss, items)
        policy.mines_native_baseline = dict(count=count, money=context.get("money"), inventory=dict(facts.inventory))
