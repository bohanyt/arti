"""ADR 153 (fix87B): why a Mines floor offers only the way out, said as a fact on the page where she chooses.

a63 day 2 (J1281, J1423, J1558, J1689, J1830, J2021, J2140): after a fight she walked out of floor 2 at HP 76/100.
Below the existing 80 HP floor reserve (live7 LOW_HP / skills_mines.SURVIVAL_HP_PCT) a floor offers only
``pergi_ke mine`` (and a meal when food is in the bag); her bag held no food. The Mine entrance still offered the
down-ladder, she took it, arrived on floor 1 with the same HP, saw only the exit, and read ``pergi_ke mine`` as going
on ("[recorded remark omitted]", J1286). Eight round trips that afternoon, the same on days 3-7.

The rule is unchanged. These lines only tell her what the rule does, what her HP is, and what restores it.
Nothing is hidden, ordered or chosen for her.
"""
from collections.abc import Mapping


def _hp(combat, health):
    if isinstance(combat, Mapping) and type(combat.get("hp")) is int and type(combat.get("max_hp")) is int \
            and combat["max_hp"] > 0:
        return combat["hp"], combat["max_hp"]
    if type(health) is int:
        return health, None
    return None, None


def below_reserve(combat, health, low_hp, reserve_pct):
    """The same terms live7._day_plan uses for the floor branch: absolute HP and the survival percentage."""
    hp, max_hp = _hp(combat, health)
    if hp is None:
        return False
    return hp < low_hp or (type(health) is int and health < low_hp) or (
        max_hp is not None and hp * 100 < reserve_pct * max_hp)


def floor_offer_fact(location, combat, health, *, edible, offered, low_hp, reserve_pct):
    """One line for the offer's ``facts`` block, or None. ``offered`` = the (name, arg) pairs on this page."""
    location = str(location or "")
    on_floor = location.startswith("UndergroundMine")
    if not on_floor and location != "Mine":
        return None
    hp, max_hp = _hp(combat, health)
    hp_text = "HP %s/%s" % (hp, max_hp) if max_hp else "HP %s" % hp
    restore = "HP pulih hanya dengan makan (%s) atau tidur." % ("ada makanan di tas" if edible else "tidak ada makanan di tas")
    exit_line = "pergi_ke mine dari lantai = naik keluar ke pintu masuk Mine, bukan turun."
    if not below_reserve(combat, health, low_hp, reserve_pct):
        return exit_line if on_floor and ("pergi_ke", "mine") in offered else None
    if on_floor:
        threshold = "<%d" % low_hp if hp < low_hp else "<%d%%" % reserve_pct
        if ("lawan_mine", None) in offered:
            return ("%s (%s): lawan_mine di sini = pertarungan pembuka jalan keluar, lalu naik; "
                    "cadangan kerja/pulang tidak menutup pertarungan keluar. %s %s"
                    % (hp_text, threshold, exit_line, restore))
        return ("%s (%s): di lantai hanya keluar%s yang ditawarkan, tanpa bertarung/menambang/turun. %s %s"
                % (hp_text, threshold, " dan makan" if edible else "", exit_line, restore))
    if any(name == "aksi" and str(arg or "").startswith("xt:mine:Mine:") for name, arg in offered):
        return ("%s (<%d): tangga turun ke lantai 1; di lantai dengan HP ini hanya keluar%s yang ditawarkan. %s"
                % (hp_text, low_hp, " dan makan" if edible else "", restore))
    return None
