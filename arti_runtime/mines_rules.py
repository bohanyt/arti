"""The Mines rules that still decide for her in free play, as one plain fact, and the rules themselves (FIX84B, ADR 149).

Drift review 2026-11-10 §2: an entry window (09:00-18:00), a forced exit at 22:00, an auto-exit at 60 HP while mining rocks and a
"partial" label for floor < 5 or no kill decided for her above the safety line, and none was on her page. In free play (Live7):
- no entry window: the Mines are open whenever the game lets her in;
- the one time rule left is the game's own 02:00 pass-out: a floor is left once the estimated way home (`live7.home_eta`, a
  coarse estimate) no longer fits before 02:00; when she leaves before that is hers (the bedtime reserve facts stay as they are);
- the non-combat Mines skill (`tambang_bijih`) leaves at 60% HP only while under attack (the Live7 threat rule: a monster in
  contact, or native HP lost with a monster near), and at the reflex HP floor (40%) always;
- lawan_mine's 80% reserve, meal, contact defence and fix82A/83B survival exits are unchanged;
- a verified alive exit is a finished trip on any floor, with or without a kill.
ADR 160 additionally reserves 60 game minutes and exits at energy <=60, leaving
40 energy above the existing tool floor for blocked-path repair; both are page facts.
Wave-1 / Part B callers keep their own task rules; they are not free play.
"""
from __future__ import annotations

from collections.abc import Mapping

CURFEW_REASON = "mines_curfew:pass_out_0200"
CURFEW_HOLD = "partial:mines_curfew_exit"      # left because the way home no longer fits before 02:00
BUDGET_HOLD = "partial:mines_budget_exit"      # left because the skill's own allowance is nearly spent
FLOOR = "UndergroundMine1"                     # where an entry from the lobby lands
EXIT_CLEAR_MARGIN = 60  # ADR 160: game minutes reserved for blocked-path clearing/re-survey
EXIT_ENERGY_RESERVE = 60  # leave 40 energy above the existing 20-energy tool floor for route repair
ENERGY_HOLD = "partial:mines_energy_exit"


def energy_due(energy):
    return type(energy) in (int, float) and energy <= EXIT_ENERGY_RESERVE


def _live7():
    import arti_stardew_live7 as L
    return L


def curfew_due(location, t, policy=None) -> bool:
    """The estimated way home from ``location`` no longer fits before the game's 02:00 pass-out."""
    L = _live7()
    repair = EXIT_CLEAR_MARGIN if str(location or "").startswith("UndergroundMine") else 0
    from arti_runtime.travel_time import SAFETY_MARGIN
    return type(t) is int and L.minutes(t) + L.home_eta(location, policy) + max(repair, L.HOME_MARGIN, SAFETY_MARGIN) >= L.minutes(L.PASS_OUT_CLOCK)


def entry_open(t, policy=None) -> bool:
    """Free play: a Mines entry is offered/admitted whenever a floor could still be left in time for 02:00."""
    return type(t) is int and not curfew_due(FLOOR, t, policy)


def curfew_clock(policy=None, location=FLOOR) -> str:
    """The latest floor time including path repair, e.g. ``20:30``."""
    L = _live7()
    from arti_runtime.travel_time import SAFETY_MARGIN
    m = L.minutes(L.PASS_OUT_CLOCK) - L.home_eta(location, policy) - max(EXIT_CLEAR_MARGIN, L.HOME_MARGIN, SAFETY_MARGIN)
    return "%02d:%02d" % (m // 60 % 24, m % 60)


def _monsters(vitals):
    return sorted((m for m in (vitals or {}).get("monsters") or () if isinstance(m, Mapping)
                   and isinstance(m.get("distance"), (int, float)) and not isinstance(m["distance"], bool)
                   and not (type(m.get("hp")) is int and m["hp"] <= 0)), key=lambda m: m["distance"])


def under_attack(state, vitals, hp, t) -> bool:
    """The Live7 threat rule on a Mines floor: a monster in contact, or a native HP loss in the last 30 game minutes with a
    monster within the reflex radius. The HP read is remembered on the run state so a loss can be seen."""
    L = _live7()
    if type(hp) is int and type(t) is int:
        last = state.get("mines_rules_hp")
        if isinstance(last, list) and type(last[0]) is int and hp < last[0]:
            state["mines_rules_damaged_at"] = t
        state["mines_rules_hp"] = [hp, t]
    near = _monsters(vitals)
    if not near or near[0]["distance"] > L.THREAT_NEAR_TILES:
        return False
    hit = state.get("mines_rules_damaged_at")
    recent = type(hit) is int and type(t) is int and 0 <= L.minutes(t) - L.minutes(hit) <= L.DAMAGE_RECENT_MIN
    return near[0]["distance"] <= L.THREAT_CONTACT_TILES or recent


def ore_hp_low(state, vitals, hp, max_hp, t) -> bool:
    """`tambang_bijih`'s survival exit: below 60% while under attack, at or below the 40% reflex floor always."""
    L = _live7()
    if type(hp) is not int or hp <= 0:
        return False
    top = max_hp if type(max_hp) is int and max_hp > 0 else 100
    attacked = under_attack(state, vitals, hp, t)
    return hp * 100 <= L.CRITICAL_HP_PCT * top or (attacked and hp * 100 < L.RETREAT_HP_PCT * top)


def rules_line(policy=None, location=FLOOR) -> str:
    """One plain `day_rules` sentence for a Mines page: the rules that still apply, the game's first."""
    from arti_stardew_skills_mines import SURVIVAL_HP_PCT
    from arti_runtime.travel_time import home, SAFETY_MARGIN
    L = _live7()
    floor = location if str(location).startswith("UndergroundMine") else FLOOR
    return ("Di Mines: HP 0 = pingsan, bangun di klinik, barang bisa hilang (game). Program: lawan_mine mulai HP %d, saat bertarung di "
            "bawah %d%% makan/keluar; tambang_bijih keluar bila diserang dan HP <%d%%; HP <=%d%% keluar; dari lantai keluar %s "
            "(pulang ~%dm + cadangan bersihkan jalan %dm, pingsan 02:00); energi <=%d keluar "
            "(cadangan bersihkan jalan %d di atas batas tool %d); jatah skill habis = keluar. Sebelum itu pilihanmu."
            % (L.LOW_HP, SURVIVAL_HP_PCT, L.RETREAT_HP_PCT, L.CRITICAL_HP_PCT, curfew_clock(policy, floor), L.home_eta(floor, policy),
               max(EXIT_CLEAR_MARGIN, L.HOME_MARGIN, SAFETY_MARGIN), EXIT_ENERGY_RESERVE,
               EXIT_ENERGY_RESERVE-L.VERY_LOW_ENERGY, L.VERY_LOW_ENERGY))


def mines_page(location, offered_names=()) -> bool:
    """A page where the Mines rules apply: on a Mines map, or offering a Mines skill."""
    return str(location or "").startswith(("Mine", "UndergroundMine")) or bool(
        {"lawan_mine", "tambang_bijih"} & set(offered_names or ()))
