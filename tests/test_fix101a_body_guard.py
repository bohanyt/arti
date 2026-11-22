"""ADR 179 (fix101A): the runtime side of the mod's body guard and of "one escape motor for every way out".

C# Fix101aBodyGuardTests prove the trigger, the reaction time in ticks, straight-run speed and the cornered verdicts.
These prove: the guard's fact reaches the journal and her next page with native numbers; the named up action walks
with the exit's mod leg; the cornered rule acts (break through / defend / named fact) with the true reason; the
floor record; no relaunch on the stand. Replays use attempt 71 shapes (refs in the receipt).
"""
from types import SimpleNamespace as NS

import pytest

import arti_stardew_combat as C
import arti_stardew_skills as S
from arti_runtime import mines_body as B
from arti_runtime import mines_contact as MC

FLOOR = "UndergroundMine2"
UP = "xt:mine:%s:4,4" % FLOOR


class _Rt:
    def __init__(self, clear=(), context=None):
        self.rows, self.rules, self.cleared = [], [], []
        self._clear = list(clear)
        self._context = context or _context()

    def _journal(self, row):
        self.rows.append(dict(row))

    def _rule_json(self, request, choice, rule, label):
        self.rules.append((choice, label))
        return "rule:" + label

    def _skill_wave1_context(self, snapshot):
        return self._context

    def route_clear_step(self, run, goal, request, caps, *, reaches, path=None):
        self.cleared.append(reaches)
        return self._clear.pop(0) if self._clear else None


def _context(monsters=None):
    grid = {(x, y): ("#" if x in (0, 9) or y in (0, 6) else ".") for x in range(10) for y in range(7)}
    grid[(3, 3)] = "s"
    return dict(actor_tile=[6, 3], nav_grid=grid,
                extras=[dict(kind="ladder_up", ref=UP, x=4, y=4, ready=True, stands=[[4, 5]])],
                targets={"obj:%s:3,3:(O)32" % FLOOR: dict(kind="object", tile=dict(x=3, y=3), name="Stone",
                                                         qualified_item_id="(O)32")},
                combat=dict(hp=43, max_hp=100, in_mines=True, location=FLOOR,
                            monsters=monsters if monsters is not None else [
                                dict(id="m61", type="GreenSlime", hp=23, tile=[7, 3], distance=1.0)]))


def _goal():
    return NS(goal_id="g", combat=None, approach_progress=None, extras_objective=NS(blocker=None))


def _request(tile=(6, 3)):
    return NS(snapshot=NS(motor=NS(location=FLOOR, tile=list(tile)), reflex_combat_target=None,
                          combat_objective={}, approach_targets=()))


# ---------------------------------------------------------------- hole 1: the mod's own leg is told afterwards

def test_mod_guard_fact_reaches_journal_and_page_with_native_numbers():
    rt = _Rt()
    MC.note_hp(rt, FLOOR, 75, 0.0)
    combat = dict(monsters=[dict(id="m2", type="RockCrab", hp=25, distance=0.3)],
                  body_guard=dict(seq=4, status="settled", location=FLOOR, hp=70, hp_from=75, tick=1200, drop_tick=1197,
                                  starts=1, verdict="Retreated(contact)",
                                  reflex=dict(detail="at_exit:contact", disengage_route="walk_in_contact", defends=0)))
    assert B.body_guard_seen(rt, combat, FLOOR)["status"] == "settled"
    row = next(r for r in rt.rows if r["ev"] == "mines_body_guard")  # ADR 185 adds an at-stand row after it
    assert row["ev"] == "mines_body_guard" and row["hp_from"] == 75 and row["hp"] == 70 and row["drop_tick"] == 1197
    assert B.body_guard_seen(rt, combat, FLOOR) is None  # one row per native seq
    MC.note_hp(rt, "Mine", 70, 9.0)
    text = MC.page_fact(rt, {}, 70, "Mine")
    assert "HP 75 -> 70" in text and "RockCrab HP 25 menyentuhmu" in text and "None" not in text
    assert "jalan ke tangga naik" in text and "Sekarang kamu di Mine, HP 70" in text


# ---------------------------------------------------------------- hole 2: the named up action uses the exit's leg

def _recovery_rt(native_press, offered=True):
    pinned, invoked = [], []
    caps = {"reflex_combat": dict(disengage_available=offered)}
    rt = NS(_skill_wave1_context=lambda snap: _context(),
            _combat_ui_facts=lambda pin: dict(capabilities=caps), _water_context=lambda pid: object(),
            mines_exit_pin=lambda goal, loc, why: pinned.append(loc),
            _mines_exit_invoke=lambda request: invoked.append(1) or "rule:exit_controller")
    run = NS(call=S.SkillCall("aksi", UP, tile=(4, 4)), state={})
    goal = NS(mines_exit=None, interact_objective=None, extras_objective=NS(blocker=None))
    request = NS(snapshot=NS(motor=NS(location=FLOOR, tile=[15, 9]), binding=NS(player_id=7),
                             world_interaction_target=dict(kind="ladder_up") if native_press else None))
    return rt, run, goal, request, pinned, invoked


def test_named_up_action_walks_with_the_exit_controller_not_the_tap_walk(monkeypatch):
    # a71 B J1594-J1811: the guard picked `aksi` at HP 91; 23 tap inputs, 0.43 tiles/s, HP 6.
    from arti_stardew_skills_runtime import SkillRuntimeMixin
    import arti_stardew_generic_world as G
    monkeypatch.setattr(G, "step", lambda *a, **k: "generic_tap_walk")
    rt, run, goal, request, pinned, invoked = _recovery_rt(native_press=False)
    assert SkillRuntimeMixin._skill_mine_recovery_step(rt, run, goal, request) == "rule:exit_controller"
    assert run.state["mines_up_via_exit"] is True and pinned == [FLOOR]
    # On the stand with the native press published, the action presses directly (no walk to do).
    rt, run, goal, request, pinned, invoked = _recovery_rt(native_press=True)
    assert SkillRuntimeMixin._skill_mine_recovery_step(rt, run, goal, request) == "generic_tap_walk" and not pinned
    # An older mod without the leg keeps the existing named-action path.
    rt, run, goal, request, pinned, invoked = _recovery_rt(native_press=False, offered=False)
    assert SkillRuntimeMixin._skill_mine_recovery_step(rt, run, goal, request) == "generic_tap_walk"


# ---------------------------------------------------------------- hole 4: cornered acts, with the true reason

def _settled(detail, verdict="Aborted(route_blocked)", **leg):
    goal = _goal()
    goal.combat = C.CombatObjective("g", kind=C.KIND_MONSTER, disengage=True, calls=1, last_verdict=verdict,
                                    last_detail=detail, reason="monster_not_killed_yet",
                                    status="RETREATED" if verdict.startswith("Retreated") else "OPEN",
                                    leg=dict(dict(hp_start=43, hp_end=33, defends=0), **leg))
    return goal


def test_no_route_leg_breaks_through_when_the_native_facts_name_a_breakable_closure():
    swing = S.Step("decision", S.act("world_key.use_tool", dict(direction="left")))
    rt, goal, leg = _Rt(clear=[swing]), _settled("no_route_to_exit"), {}
    assert B.disengage_step(rt, goal, _request(), leg, dict(combat=_context()["combat"]), run=NS()) == "rule:route_clear"
    assert any(r["ev"] == "mines_floor_record" and r["reason"] == "cornered:no_route_to_exit" for r in rt.rows)
    assert any(r.get("status") == "cornered" and r.get("action") == "route_clear" for r in rt.rows)


def test_no_route_leg_defends_against_the_attacker_in_contact(monkeypatch):
    # a71 A J3281-J3304: no_route_to_exit, defends 0, HP 43 -> 33 -> 9; nothing acted.
    import arti_runtime.mines_survival as MS
    monkeypatch.setattr(MS, "escape_invoke", lambda runtime, goal, request, leg: "rule:escape_fight")
    rt, goal, leg = _Rt(), _settled("no_route_to_exit"), {}
    assert B.disengage_step(rt, goal, _request(), leg, dict(combat=_context()["combat"]), run=NS()) == "rule:escape_fight"
    assert leg["cornered_defend_done"] is True
    # Her page states the native reason, never the objective's generic "monster_not_killed_yet".
    act = MC.memory(rt)["last_act"]
    assert act["action"] == "jalan keluar terhenti: tidak ada jalan terbuka ke tangga naik"
    assert act["hp_from"] == 43 and act["hp"] == 33


def test_nothing_possible_is_a_named_fact_and_a_floor_record():
    rt, goal, leg = _Rt(context=_context(monsters=[])), _settled("no_route_to_exit"), {}
    assert B.disengage_step(rt, goal, _request(), leg, dict(combat=dict(location=FLOOR, monsters=[])), run=NS()) is None
    assert goal.extras_objective.blocker == "mines_exit_no_route"
    rec = next(r for r in rt.rows if r["ev"] == "mines_floor_record")
    # ADR 180: the window is the box of her tile, the attackers and the way up plus a margin.
    x0, y0 = rec["origin"]
    width = len(rec["rows"][0])
    assert all(len(row) == width for row in rec["rows"])
    assert rec["rows"][3 - y0][6 - x0] == "." and rec["exits"][0]["stands"] == [[4, 5]]
    assert x0 <= 4 < x0 + width and y0 <= 5 < y0 + len(rec["rows"])
    assert rec["pieces"][0]["kind"] == "stone" and rec["pieces"][0]["tool"] == "Pickaxe"


def test_leg_ended_on_the_stand_never_relaunches_there():
    # a71 A floor 5 J1317-J1395: three legs of 10/1/1 ticks spent the leg bound beside the ladder.
    rt, goal, leg = _Rt(), _settled("at_exit:contact", verdict="Retreated(contact)"), {}
    ui = dict(capabilities={"reflex_combat": dict(disengage_available=True)}, combat=_context()["combat"])
    assert B.disengage_step(rt, goal, _request(tile=(4, 5)), leg, ui, run=NS()) == "rule:disengage_at_stand"
    assert B.disengage_step(rt, goal, _request(tile=(4, 5)), leg, ui, run=NS()) is None and goal.combat is None
    assert B.disengage_step(rt, goal, _request(tile=(6, 3)), leg, ui, run=NS()) == "rule:disengage_refresh"


def test_busy_refusal_rereads_instead_of_falling_back_to_tap_steps():
    rt, goal, leg = _Rt(), _goal(), {}
    goal.combat = C.CombatObjective("g", kind=C.KIND_MONSTER, disengage=True, last_verdict="refused",
                                    last_detail="REFLEX_COMBAT:busy", reason="reflex_refused")
    ui = dict(capabilities={"reflex_combat": dict(disengage_available=True)}, combat=_context()["combat"])
    assert B.disengage_step(rt, goal, _request(), leg, ui, run=NS()) == "rule:disengage_refused_reread"
    assert not leg.get("disengage_unavailable")


@pytest.mark.parametrize("detail,text", [
    ("route_blocked rock=3,3", "batu menutup jalan di 3,3"), ("disengage_stuck", "badan tidak maju"),
    ("disengage_timeout", "waktu jalan keluar habis")])
def test_true_reason_text(detail, text):
    assert text in B.reason_text(_settled(detail).combat)
