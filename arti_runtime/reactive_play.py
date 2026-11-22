"""Optional play voice projections from existing pages and native row receipts.

No game reads, decisions, or decision-page writes (A7/A8 voice contract).
"""
from copy import deepcopy
import math
import time

PAGE_KEYS = ("di_sini", "bisa_di_sini", "kunjungan", "quest_lanjut", "festival_hari",
             "jalan_terhalang", "route_home_blocked", "ruang_tas", "semalam", "warga")
MENU_SKILLS = frozenset({"inventori", "tutup_menu", "cek_jurnal", "cek_pagi", "pegang",
                         "siapkan_pancing", "langkah"})


def number(config, key, default):
    try:
        value = float(config.get(key, default))
        return max(0.0, value) if math.isfinite(value) else default
    except (ValueError, TypeError, OverflowError):
        return default


def page_values(offer):
    out = {}
    for section in (offer, offer.get("day") or {}, offer.get("facts") or {}):
        if isinstance(section, dict):
            out.update({k: deepcopy(section[k]) for k in PAGE_KEYS if k in section})
    return out


def capture_page(runtime, session, request):
    """Copy an already built page; rendering and its byte budget are untouched."""
    from arti_stardew_skills import _g
    motor = _g(_g(request, "snapshot"), "motor")
    received = _g(motor, "diterima_pada")
    stamp = time.time() - max(0.0, time.monotonic() - received) if type(received) in (int, float) else time.time()
    runtime._live7_voice_page = dict(value=page_values(request.skill_offer or {}),
                                    location=_g(motor, "location"), date=tuple(session.campaign.date),
                                    session_id=session.session_id,
                                    observed_ts=stamp)


def native_page(runtime, session, facts, *, fresh):
    """Separate now.json facts, never a cache of an old place under a new pose."""
    from arti_stardew_live7 import mind_label
    cached = getattr(runtime, "_live7_voice_page", {})
    location = getattr(facts, "location", None)
    date = tuple(session.campaign.date)
    out = {}
    stamp = time.time() if fresh and location else None
    if (fresh and location and cached.get("location") == location and cached.get("date") == date
            and cached.get("session_id") == session.session_id
            and 0 <= time.time() - cached.get("observed_ts", 0) <= 45):
        out = deepcopy(cached["value"])
        stamp = cached["observed_ts"]
    if fresh and location:
        from arti_runtime.place_page import here_name
        from arti_runtime.quest_next import lines
        from arti_runtime.festival_facts import day_line
        names = getattr(runtime, "_live7_native_place_names", None)
        calendar = getattr(runtime, "_live7_calendar", ())
        out.setdefault("di_sini", here_name(location, names=names))
        if quest := lines(getattr(runtime, "_live7_quest_records", ()),
                          getattr(runtime, "_live7_raw_knowledge", None), location):
            out["quest_lanjut"] = quest
        if festival := day_line(calendar, mind_label(date), clock=getattr(facts, "time_of_day", None), names=names):
            out["festival_hari"] = festival
    night = getattr(getattr(session.campaign, "live", None), "previous_night", None)
    if night:
        from arti_runtime.day_facts import night_fact
        if line := night_fact(night):
            out["semalam"] = line
    scope = getattr(runtime, "_live7_discovery_scope", None)
    if scope:
        out["farm_scope"] = scope
    # Source timestamps travel with the page, independently of heartbeat freshness.
    if out:
        out["play_page_stamp"] = dict(observed_ts=stamp, location=location, date=mind_label(date))
    return out


def is_long(skill, eta, config):
    return skill in ("pergi_ke", "jalan_ke") or number({"eta": eta}, "eta", 0) > number(
        config, "live7_long_action_sec", 20)


def outcome_facts(row, before, after, context, seen_places, page=None):
    """(grounded lines, notable lines), with no command-success inference."""
    from arti_stardew_moments import row_activity
    from arti_runtime.fact_names import inventory_gains, source_names
    from arti_runtime.place_page import EMPTY, here_name
    page = dict(page or {})
    page.update(page_values(context))
    lines, new = [], []
    if before.get("location"):
        seen_places.add(before["location"])
    location = after.get("location")
    if location and location not in seen_places:
        seen_places.add(location)
        new.append("kunjungan: " + here_name(location, context.get("world_knowledge")))
    for key in ("kunjungan", "jalan_terhalang", "route_home_blocked", "festival_hari"):
        if page.get(key):
            new.append(key + ": " + str(page[key]))
    for who in (context.get("targets") or {}).values():
        if isinstance(who, dict) and who.get("kind") == "npc":
            name = who.get("display_name") or who.get("internal_name") or who.get("name")
            if isinstance(name, str) and name:
                new.append("warga: " + name)
    if page.get("bisa_di_sini") in ("", [], EMPTY):
        new.append("bisa_di_sini: " + EMPTY)
    names = {**(before.get("names") or {}), **source_names(context),
             **((row.get("evidence") or {}).get("names") or {})}
    gains = inventory_gains(before, after, names, (row.get("evidence") or {}).get("chest_moves"))
    if gains:
        new.append("masuk tas selama itu: " + ", ".join(
            f"{g['name']} +{g['quantity']} (tas sekarang {after['inventory'][g['item']]})" for g in gains))
    failed = row.get("outcome_class") in ("CLEAN_FAIL", "FAULT", "UNCERTAIN") or row.get("outcome") in ("fail", "failed")
    if row.get("skill") == "tidur":
        # The caller's day-boundary row is the sleep verifier; native Yes alone is not a save receipt.
        if row.get("verified") is True and row.get("outcome_class") == "VERIFIED":
            from arti_runtime.sleep_truth import night_kind, page_line
            observed = (row.get("evidence") or {}).get("observed")
            lines.append(context.get("semalam") or page_line(night_kind(observed=observed)))
        elif failed:
            from arti_live7_status_facts import _outcome_text
            lines.append(_outcome_text(row, names))
    else:
        activity = row_activity(row, before, after, context)
        if activity:
            lines.append(activity)
        elif failed:
            from arti_live7_status_facts import _outcome_text
            lines.append(_outcome_text(row, names))
    if failed:
        new.extend(lines)
    return list(dict.fromkeys(lines + new)), list(dict.fromkeys(new))
