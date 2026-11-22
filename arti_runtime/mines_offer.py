"""ADR 156: the measured Mines allowance and the reasons an offer is absent."""
from dataclasses import replace

# Attempt 63: 1,223 active seconds / 8 lawan_mine starts = 152.9 s per run;
# the native-clock analysis measured about 210 game minutes (a63 analysis §2).
# This is a coarse work estimate, not a prediction that a trip will finish.
RUN_GAME_MINUTES = 210
UP_LABEL = "naik ke pintu masuk tambang"


def work_minutes(location, full_run):
    """ADR 160: a complete run includes its exit; a floor's home ETA includes it too.

    Retain the full measured allowance at the entrance. On a floor charge the
    native ladder/Leave leg only in home_eta, not again in the work allowance.
    No additional entrance-walk discount is assumed without a measurement.
    """
    from arti_runtime.travel_time import EXIT_MINUTES
    if str(location or "").startswith("UndergroundMine"):
        return max(0, full_run - EXIT_MINUTES)
    return full_run


def up_label(skill, arg, origin):
    return UP_LABEL if skill == "pergi_ke" and arg == "mine" and str(origin or "").startswith("UndergroundMine") else None


def offer_fact(facts, context, policy, calls):
    """Explain the existing gates, without choosing or granting an action."""
    import arti_stardew_live7 as L
    import arti_stardew_skills as S
    from arti_stardew_combat import is_weapon
    from arti_stardew_skills_mines import survival_low, SURVIVAL_HP_PCT
    from arti_runtime.mines_rules import entry_open, energy_due, EXIT_ENERGY_RESERVE

    location = str(facts.location or "")
    inside = location.startswith("UndergroundMine")
    if location != "Mine" and not inside:
        return None
    from arti_runtime.travel_time import admission_budget
    budget = admission_budget(S.SkillCall("lawan_mine"), facts, policy)
    cost, eta, cutoff = budget["work_min"], budget["home_min"], budget["cutoff_min"]
    clock = lambda m: "%02d:%02d" % (m // 60 % 24, m % 60)
    now = clock(L.minutes(facts.time_of_day)) if type(facts.time_of_day) is int else "jam belum terbaca"
    exempt = policy.home_blocked_here(location) is not None
    line = ("lawan_mine muat sampai %s; sekarang %s (perkiraan kerja %dm + pulang %dm + margin %dm)."
            % (clock(cutoff), now, cost, eta, budget["margin_min"]))
    line += " " + budget["basis"] + "; batas " + budget["deadline"] + "."
    if exempt:
        line += " Cadangan pulang tidak menyaring: jalan pulang terblokir."
    if any(c.name == "lawan_mine" for c in calls):
        from arti_runtime.mines_survival import escape_offered
        if inside and escape_offered(context, location):
            line += " Pertarungan jalan keluar ditawarkan; batas HP/waktu kerja dan karantina kerja tidak berlaku untuk keluar."
        return line
    reasons = []
    if not facts.inventory_current or not context.get("inventory_complete") or not context.get("ui_complete"):
        reasons.append("fakta tas/UI belum lengkap dan terkini")
    if not facts.player_free or facts.menu_open:
        reasons.append("pemain belum bebas/menu terbuka")
    if policy.sleep_only and (inside or not exempt):
        reasons.append("cadangan pulang aktif")
    combat = context.get("combat") or {}
    hp, top = combat.get("hp"), combat.get("max_hp")
    if type(hp) is not int or inside and (type(top) is not int or top <= 0):
        reasons.append("HP native belum lengkap")
    elif hp < L.LOW_HP or facts.health is not None and facts.health < L.LOW_HP or inside and survival_low(combat):
        reasons.append("HP %s/%s; syarat HP >=%d%s" % (hp, top or "?", L.LOW_HP,
                        " dan >=%d%%" % SURVIVAL_HP_PCT if inside else ""))
    if not inside and facts.energy is not None and facts.energy < L.VERY_LOW_ENERGY:
        reasons.append("energi %s <%d" % (facts.energy, L.VERY_LOW_ENERGY))
    elif energy_due(facts.energy):
        reasons.append("energi %s <=%d; cadangan bersihkan jalan keluar" % (facts.energy, EXIT_ENERGY_RESERVE))
    inventory = {q: n for q, n in facts.inventory.items() if q not in policy.uncertain_items}
    equipment = replace(facts, inventory=inventory,
                        toolbar=tuple(q for q in facts.toolbar if q not in policy.uncertain_items),
                        equipped=None if facts.equipped in policy.uncertain_items else facts.equipped)
    if not S._has(equipment, *S.PICKAXES):
        reasons.append("beliung belum tersedia")
    if not any(is_weapon(q) and n for q, n in inventory.items()):
        reasons.append("senjata yang diterima belum tersedia")
    if not inside:
        kinds = {e.get("kind") for e in L._entries(context) if e.get("ref") not in policy.refs}
        if not kinds & {"ladder_down", "mine_elevator"}:
            reasons.append("tangga turun/lift belum ditawarkan game")
        if not entry_open(facts.time_of_day, policy):
            reasons.append("waktu keluar lantai sebelum pingsan 02:00 tidak cukup/jam belum terbaca")
        if policy.last_failed == ("lawan_mine", None):
            reasons.append("percobaan terakhir gagal; tidak diulang langsung")
    if policy.blocked(S.SkillCall("lawan_mine"), location):
        reasons.append("karantina kegagalan hari ini")
    if not exempt and (reason := L.admission(S.SkillCall("lawan_mine"), facts, policy)) and reason.startswith("skipped_time:"):
        reasons.append("jam belum terbaca" if reason.endswith("unknown") else "cadangan waktu pulang tidak cukup")
    return (line + " Tidak ditawarkan: " + "; ".join(reasons or ["prasyarat skill belum terpenuhi"])
            + ". Pulihnya HP/energi, fakta/peralatan terkini atau jalan keluar yang teramati membuka penilaian baru; makan/naik tetap dinilai terpisah.")


def label_rows(rows):
    """Preserve call tokens; label only a witnessed floor-to-entrance journey."""
    out = []
    for row in rows:
        row = dict(row)
        evidence = row.get("evidence") or {}
        origin = row.get("from_location") or evidence.get("from_location")
        if label := up_label(row.get("skill"), row.get("arg"), origin):
            row["label"] = label
        out.append(row)
    return out
