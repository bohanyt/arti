"""ADR 181 (fix103A): the runtime side of a guard that holds the body until safe.

The mod-side root cause (a runtime lease release cancelling the guard; the guard's receipt overwriting the runtime's
release proof) is pinned by a C# test project that is not in this repository. These cover what her page and journal say.
"""
from types import SimpleNamespace as NS

from arti_runtime import mines_body as B
from arti_runtime import mines_contact as MC

FLOOR = "UndergroundMine2"


class _Rt:
    def __init__(self):
        self.rows = []

    def _journal(self, row):
        self.rows.append(dict(row))


def test_guard_journal_row_measures_reaction_and_speed():
    rt = _Rt()
    fact = dict(seq=3, status="settled", location=FLOOR, hp=86, hp_from=93, tick=28900, drop_tick=28338,
                verdict="Retreated(contact)", reflex=dict(detail="at_exit:contact", start_tick=28376,
                                                          first_press_tick=28376, ticks=520, tiles_moved=17.4,
                                                          hp_start=89, hp_end=86, defends=0, disengage_route="walk"))
    B.body_guard_seen(rt, dict(monsters=[], body_guard=fact), FLOOR)
    row = next(r for r in rt.rows if r["ev"] == "mines_body_guard")
    assert (row["first_press_tick"] - row["drop_tick"], row["tiles_moved"], row["ticks"]) == (38, 17.4, 520)
    assert row["hp_start"] == 89 and row["hp_end"] == 86
    assert not any(r["ev"] == "mines_body_guard_failed" for r in rt.rows)  # Retreated: no fallback turn


def test_page_never_shows_a_stale_batch_or_another_floor(monkeypatch):
    # a73 pages 009/010: "last batch 0 damage" from an older fight and the floor of an older act after a recovery.
    rt = NS()
    import time
    t0 = time.monotonic()
    MC.note_hp(rt, FLOOR, 90, t0)
    MC.keep_batch(rt, dict(location=FLOOR, damage_dealt=0, target=dict(type="RockCrab", hp_start=30, hp_end=30)))
    combat = dict(monsters=[dict(type="RockCrab", hp=30, distance=0.3)])
    MC.note_hp(rt, FLOOR, 85, t0 + 1)
    fresh = MC.page_fact(rt, combat, 85, FLOOR, now=t0 + 2)
    assert "memberi 0 damage" in fresh
    stale = MC.page_fact(rt, combat, 85, FLOOR, now=t0 + MC.LAST_BATCH_FRESH_S + 5)
    assert "memberi 0 damage" not in stale
    MC.memory(rt)["last_act"] = dict(location="UndergroundMine3", action="jalan ke tangga naik", attackers=[],
                                     hp_from=90, hp=85)
    import arti_stardew_combat as C
    objective = C.CombatObjective("g", disengage=True, status="RETREATED", last_verdict="Retreated(contact)",
                                  leg=dict(hp_start=85, hp_end=80, defends=0))
    B.body_fact(rt, objective, FLOOR, combat)
    assert MC.memory(rt)["last_act"]["location"] == FLOOR  # the floor where this leg settled, not the older act's
