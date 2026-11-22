"""ADR 174: a place she can walk into is a place she can leave alive.

One safety rule for every state on a Mines floor that is not a fight batch in
flight: a live monster in contact range plus a native HP drop means her body
moves away and up, without waiting for a model page. The rule never fights for
her, never sends her home, never forbids going back down; it only picks the
already offered way up and reports, as plain facts, what her body did.

Facts only; the exit controller, route guard and native up press own the motion.
"""
from collections.abc import Mapping
import time

from arti_stardew_skills_mines import CONTACT_RADIUS_TILES  # 1.5 tiles: reflex reach 1.35 plus half a tile of drift

# A native HP drop observed this many wall seconds ago still counts as "HP is falling" (a69: hits every ~1-1.5 s,
# boundary reads 4.7-5.0 s apart across one model call).
HP_FALLING_WINDOW_S = 10.0
LAST_BATCH_FRESH_S = 60.0  # ADR 181: a settled batch older than this is not "the last batch" on her page
# Guard starts per floor visit. The exit's own four-failure bound (mines_exit.MAX_FAILED_ATTEMPTS) still applies
# inside each start; this bounds the boundary loop itself.
GUARD_STARTS_MAX = 8
MEMORY_KEY = "_mines_contact_memory"


def _floor(location):
    return str(location or "").startswith("UndergroundMine")


def attackers(combat):
    """Live monsters within CONTACT_RADIUS_TILES, nearest first: [{type, distance, hp, id}]."""
    out = []
    for m in (combat or {}).get("monsters") or () if isinstance(combat, Mapping) else ():
        d = m.get("distance") if isinstance(m, Mapping) else None
        if (isinstance(d, (int, float)) and not isinstance(d, bool) and d <= CONTACT_RADIUS_TILES
                and not (type(m.get("hp")) is int and m["hp"] <= 0)):
            out.append(dict(type=str(m.get("type") or "monster")[:40], distance=round(float(d), 1),
                            hp=m.get("hp") if type(m.get("hp")) is int else None, id=m.get("id")))
    return sorted(out, key=lambda m: m["distance"])


def memory(runtime):
    mem = getattr(runtime, MEMORY_KEY, None)
    if not isinstance(mem, dict):
        mem = {}
        setattr(runtime, MEMORY_KEY, mem)
    return mem


def note_hp(runtime, location, hp, now=None):
    """Record one native HP read. A drop on the same floor stamps ``drop`` (from, to, at)."""
    if type(hp) is not int or not location:
        return
    now = time.monotonic() if now is None else now
    mem = memory(runtime)
    last = mem.get("last")
    if mem.get("visit") != location:
        # ADR 177: what the body did survives the ladder; ADR 180: so does a failed mod guard (location-scoped).
        kept = {k: mem[k] for k in ("last_act", "mod_guard_failed", "body_guard_seq") if k in mem}
        seq = mem.get("visit_seq", 0) + 1
        mem.clear()
        mem.update(kept)
        mem["visit"] = location
        mem["visit_seq"] = seq  # ADR 183: a body fact belongs to the visit it happened in
        mem["starts"] = 0
    elif last is not None and hp < last[0]:
        first = mem.get("drop")
        start = first["from"] if first and now - first["at"] <= HP_FALLING_WINDOW_S else last[0]
        mem["drop"] = {"from": start, "to": hp, "at": now, "since": first["since"] if first and
                       now - first["at"] <= HP_FALLING_WINDOW_S else last[1]}
    mem["last"] = (hp, now)


def falling(runtime, location, now=None):
    """The current HP drop on ``location`` within HP_FALLING_WINDOW_S, or None."""
    now = time.monotonic() if now is None else now
    mem = memory(runtime)
    drop = mem.get("drop")
    if mem.get("visit") != location or not drop or now - drop["at"] > HP_FALLING_WINDOW_S:
        return None
    return drop


def due(runtime, combat, hp, location, *, fight_in_flight=False, now=None):
    """The guard's fact when it must act now, else None.

    In flight fights stay hers (ADR 153/171). HP zero cannot act (the native KO/death outcome wins).
    """
    if not _floor(location) or fight_in_flight or type(hp) is not int or hp <= 0:
        return None
    note_hp(runtime, location, hp, now)
    near = attackers(combat)
    drop = falling(runtime, location, now)
    now = time.monotonic() if now is None else now
    turn = memory(runtime).get("at_stand_turn")
    if near and drop is None and leave_owed(runtime, location, now):
        # ADR 185: the guard left her on the stand with contact; leaving is due without waiting for the next hit.
        last = memory(runtime).get("last") or (hp, now)
        drop = {"from": last[0], "to": hp, "at": now, "since": turn["at"]}
    if not near or drop is None:
        return None
    mem = memory(runtime)
    if mem.get("starts", 0) >= GUARD_STARTS_MAX:
        return None
    now = time.monotonic() if now is None else now
    seconds = max(now - drop["since"], 0.1)
    max_hp = combat.get("max_hp") if isinstance(combat, Mapping) and type(combat.get("max_hp")) is int else None
    return dict(location=location, hp_from=drop["from"], hp=hp, max_hp=max_hp, attackers=near[:3],
                hp_lost_per_s=round((drop["from"] - hp) / seconds, 1))


def leave_owed(runtime, location, now=None):
    """ADR 185/189: a leave upward is owed on this floor (an at-stand settle, or the contact guard's own way up that a
    running guard made wait). Its HP_FALLING_WINDOW_S counts from the last moment it could not run: a guard holding the
    body (``guard_busy_at``) or the exit row that owns it (``refresh_owed``) never use it up (a76 d3 J2470-J2609)."""
    mem = memory(runtime)
    turn = mem.get("at_stand_turn")
    if not turn or turn.get("location") != location:
        return False
    now = time.monotonic() if now is None else now
    since = max(turn["at"], mem.get("guard_busy_at") or turn["at"])
    return now - since <= HP_FALLING_WINDOW_S


def owe_leave(runtime, location, why, now=None):
    """ADR 189: the way up the contact guard chose is owed, not lost, when something it cannot overrule held it."""
    memory(runtime)["at_stand_turn"] = dict(at=time.monotonic() if now is None else now, location=location, why=why)


def refresh_owed(runtime, location, now=None):
    """ADR 189: the exit row that owns the leave is running; its own time never expires the owed leave."""
    turn = memory(runtime).get("at_stand_turn")
    if turn and turn.get("location") == location:
        turn["at"] = time.monotonic() if now is None else now


def exit_call(calls, location):
    """The already offered way up from this floor: a published native up ACTION first, then the floor exit
    ``pergi_ke mine``, then the escape-only ``lawan_mine`` (ADR 169 rule 4 pins the exit). None: nothing offered."""
    import arti_stardew_skills as S
    calls = list(calls or ())
    for c in calls:
        if c.name == "aksi" and str(c.arg or "").startswith("xt:mine:%s:" % location):
            return c
    for c in calls:
        if c == S.SkillCall("pergi_ke", "mine"):
            return c
    return None


def started(runtime, fact, call, via):
    """Count one guard start and keep its fact for her next page."""
    mem = memory(runtime)
    mem["starts"] = mem.get("starts", 0) + 1
    mem["last_act"] = dict(fact, action=via, skill=[call.name, call.arg] if call is not None else None,
                           at=time.time(), visit_seq=mem.get("visit_seq", 0))
    return mem["last_act"]


def keep_batch(runtime, batch):
    """The last settled fight batch on this floor, for the page fact (cleared with the floor visit)."""
    if isinstance(batch, Mapping) and _floor(batch.get("location")):
        mem = memory(runtime)
        if mem.get("visit") not in (None, batch["location"]):
            return
        mem["visit"] = batch["location"]
        mem.setdefault("starts", 0)
        mem["last_batch"] = dict(batch, at=time.monotonic())


def page_fact(runtime, combat, hp, location, inventory_food=None, way_up=None, last_batch=None, now=None):
    """``facts.diserang`` for her page while she is on a floor and either in contact, losing HP, or just moved by
    the guard. Plain facts, no advice. None when none of these hold. Off the floor (ADR 177) it states once what the
    body did there and where she is now."""
    if not _floor(location):
        mem = memory(runtime)
        act = mem.get("last_act")
        if (act is None or act.get("shown_after") or act.get("location") == location
                or act.get("visit_seq") is not None and act["visit_seq"] != mem.get("visit_seq", 0) - 1):
            return None
        act["shown_after"] = True
        return _body_text(act) + " Sekarang kamu di %s, HP %s." % (location, hp if type(hp) is int else "belum terbaca")
    now = time.monotonic() if now is None else now
    near = attackers(combat)
    drop = falling(runtime, location, now)
    act = memory(runtime).get("last_act")
    if act is not None and (act.get("location") != location or act.get("visit_seq") is not None
                            and act["visit_seq"] != memory(runtime).get("visit_seq", 0)):
        act = None  # ADR 183: an act from an earlier visit (or day) of this floor is not current
    if not near and drop is None and act is None:
        return None
    if last_batch is None:
        # ADR 181 (a73 pages 009/010): a batch is a fact only on its own floor and while it is recent.
        kept = memory(runtime).get("last_batch")
        if (isinstance(kept, Mapping) and kept.get("location") == location
                and now - kept.get("at", now) <= LAST_BATCH_FRESH_S):
            last_batch = kept
    parts = []
    if near:
        parts.append("Dekat dan bisa memukulmu sekarang: " + ", ".join(
            "%s %.1f petak%s" % (m["type"], m["distance"], (" HP %d" % m["hp"]) if m["hp"] is not None else "")
            for m in near[:3]) + ".")
    if drop is not None:
        secs = max(now - drop["since"], 0.1)
        parts.append("HP-mu turun %d -> %d dalam %.0f detik." % (drop["from"], hp if type(hp) is int else drop["to"],
                                                                 secs))
    elif type(hp) is int:
        parts.append("HP-mu %d." % hp)
    if isinstance(last_batch, Mapping) and type(last_batch.get("damage_dealt")) is int:
        target = last_batch.get("target") or {}
        parts.append("Batch tempur terakhir memberi %d damage ke %s (HP %s -> %s)." % (
            last_batch["damage_dealt"], target.get("type") or "monster", target.get("hp_start"), target.get("hp_end")))
    if inventory_food is False:
        parts.append("Tidak ada makanan di tas.")
    elif inventory_food is True:
        parts.append("Ada makanan di tas.")
    parts.append("Di co-op game tidak berhenti saat menu atau tas terbuka; monster tetap bisa memukul.")
    if isinstance(way_up, Mapping) and way_up.get("distance") is not None:
        parts.append("Tangga naik %.0f petak dari posisimu." % way_up["distance"])
    if act is not None:
        parts.append(_body_text(act))
    return " ".join(parts)


def _body_text(act):
    attackers = [m for m in act.get("attackers", ()) if isinstance(m, dict)]
    touching = [m for m in attackers if isinstance(m.get("distance"), (int, float)) and m["distance"] <= CONTACT_RADIUS_TILES]
    who = ", ".join("%s%s" % (m["type"], " HP %d" % m["hp"] if m.get("hp") is not None else "")
                    for m in (touching or attackers)[:2])
    # ADR 180: the cause is what the record says (a72: "monster menyentuhmu" for a leg with near=[] / contact=false).
    cause = ("%s menyentuhmu" % who if touching else "%s di dekat, tidak dalam jarak kontak" % who if attackers
             else "tidak ada monster tercatat dalam jarak kontak")
    return "Tubuhmu tadi bergerak sendiri (%s) di %s; %s; HP %s -> %s." % (
        act.get("action"), act.get("location"), cause, act.get("hp_from"), act.get("hp"))
