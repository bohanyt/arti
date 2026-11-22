"""Generic observation/event chronology for persona context. Unknown stays unknown."""
import json
import re
import time
from copy import deepcopy


def current_state(row, *, now=None, max_age=45):
    row = row or {}
    clock = row.get("game_time") or {}
    stamp = row.get("observed_ts", row.get("ts"))
    now = time.time() if now is None else now
    fresh = type(stamp) in (int, float) and 0 <= now - stamp <= max_age
    known = isinstance(clock, dict) and fresh and any(clock.get(k) is not None for k in ("date", "time", "place"))
    change = row.get("location_change") or {}
    change_stamp = change.get("observed_ts")
    change_date = (change.get("game_time") or {}).get("date")
    recent_move = (known and type(change_stamp) in (int, float) and 0 <= now - change_stamp <= max_age
                   and (change_date is None or change_date == clock.get("date")))
    state = dict(current_place=clock.get("place") if known else None,
                 current_time={k: clock.get(k) for k in ("date", "time")} if known else None,
                 observed_ts=stamp, age_s=round(now-stamp, 1) if type(stamp) in (int, float) else None,
                 basis="latest_observation" if known else "unknown_or_stale",
                 moved_since=change if recent_move else None)
    if known:
        from arti_runtime.place_names import current_place_label
        native = row.get("native_facts") if isinstance(row.get("native_facts"), dict) else {}
        if name := (native.get("place_names") or {}).get(clock.get("place")) or current_place_label(clock.get("place"), native.get("calendar"), clock.get("date")):
            state["place_name"] = name  # ADR 110: a48 T61/T76 read SeedShop and Temp as somewhere else
    return state


def location_change(previous, clock, stamp):
    """A native place change, independent of decisions and discovery delivery (ADR 40)."""
    old = (previous.get("game_time") or {}).get("place")
    place = (clock or {}).get("place")
    if old and place and old != place:
        return dict(from_place=old, to_place=place, observed_ts=stamp,
                    game_time={k: clock.get(k) for k in ("date", "time")},
                    previous_observed_ts=previous.get("observed_ts"))
    if (previous.get("game_time") or {}).get("date") != (clock or {}).get("date"):
        return None
    return previous.get("location_change")


def delivered_native(native, game, current, known_date=None):
    """fix63B (ADR 95): now.json's native facts as of the delivered clock. Dated relations are recomputed, and the day
    facts (weather, kalender) and morning reads hold only for their own game date (a45 T115 woke on Spring 14 to a
    Spring 13 "today"). The weather and morning read follow the last known native date (``known_date``, even when the
    pose is stale, ADR 93); with no known date they are dropped. Relations on an unknown clock read "today unknown"."""
    if not isinstance(native, dict):
        return native
    from arti_runtime import plan_dates
    today = (current.get("current_time") or {}).get("date")
    result = plan_dates.rebase(dict(native), game=game, day=plan_dates.ordinal(game, today))
    for key in ("weather", "morning_read"):
        date = today or known_date
        if isinstance(result.get(key), dict) and (date is None or result[key].get("date") != date):
            result[key] = None
    scene = result.get("scene_state")
    if isinstance(scene, dict):
        stamp = scene.get("observed_ts")
        if (type(stamp) not in (int, float) or not 0 <= time.time() - stamp <= 45
                or scene.get("place") != current.get("current_place") or scene.get("date") != today):
            result["scene_state"] = None
    return result


def speech_facts(row):
    """Only native state participates in freshness; intent never invalidates a reply."""
    current = current_state(row)
    native = delivered_native((row or {}).get("native_facts") or {}, (row or {}).get("game"), current,
                              ((row or {}).get("game_time") or {}).get("date"))
    def fresh(fact):
        stamp = (fact or {}).get("observed_ts")
        return deepcopy(fact) if type(stamp) in (int, float) and 0 <= time.time() - stamp <= 45 else None
    from arti_runtime import plan_dates
    day = plan_dates.ordinal((row or {}).get("game"), (current.get("current_time") or {}).get("date"))
    from arti_runtime.goal_facts import intent_views
    plans = intent_views(row or {}, plan_dates.intent_views(row or {}, day=day), now=time.time())
    from arti_runtime.quest_facts import quest_effect
    completed, quest_status = deepcopy(native.get("completed_action")), deepcopy(native.get("quest_status"))
    if isinstance(completed, dict) and (effect := quest_effect(completed, quest_status)):
        completed["quest_effect"] = effect  # fix60A (ADR 87): a ship/sell/store never reads as helping an NPC
    # ADR 93: the day's weather/season facts hold for their game date; the progress lines are a dated memory.
    weather = deepcopy(native.get("weather"))
    scene = fresh(native.get("scene_state"))
    bag = ((scene or {}).get("value") or {}).get("receipt_item_in_bag")
    if (isinstance(completed, dict) and bag and bag["item"] == completed.get("item")
            and scene["observed_ts"] >= completed.get("observed_ts", 0)):
        completed["current_bag"] = dict(bag)
    result = dict(current=current, plans=plans, completed_action=completed,
                scene_state=scene,
                quest_status=quest_status,  # fix60A (ADR 87): newest native quest log, a dated memory
                weather=weather, progress_memory=deepcopy(native.get("progress_memory")),
                calendar=deepcopy(native.get("calendar")),  # fix63B (ADR 95): passed and future festivals, as of this clock
                morning_read=deepcopy(native.get("morning_read")),  # fix63B: the TV text she read today, verbatim
                native_story=deepcopy(native.get("native_story", [])),
                goal_targets=fresh(native.get("goal_targets")),
                own_state={k: fresh(v) for k, v in (native.get("own_state") or {}).items()},
                menu_container=fresh(native.get("menu_container")), last_container=native.get("last_container"),
                ground_items=deepcopy(native.get("ground_items")),  # fix59A (ADR 86): a memory, never a fresh read
                chest_memory=deepcopy(native.get("chest_memory")),  # FIX76A: her chests as she last read them, dated; a memory
                **{k: fresh(native.get(k)) for k in ("current_menu", "peer_sleep", "shop_availability", "own_crops", "ready_check", "harvest")})
    from arti_runtime.fact_names import projection
    return dict(projection({k: v for k, v in result.items() if k != "plans"}, native.get("names"), native.get("place_names")), plans=plans)


def fact_values(value):
    """Reception timestamps describe age, not a change in the observed values."""
    if isinstance(value, dict):
        return {k: fact_values(v) for k, v in value.items()
                if k not in {"observed_ts", "received_monotonic", "observation_session"}}
    return value


def _same_fact(key, a, b):
    """Values the refresh identity compares: reception stamps and the current row's age are not a change."""
    if key == "current":
        drop = {"observed_ts", "age_s"}
        a, b = ({k: v for k, v in (x or {}).items() if k not in drop} for x in (a, b))
    return fact_values(a) == fact_values(b)


def changed_facts(before, after):
    """ADR 113: (previous, latest) of the keys that moved. The rest is already in this prompt's [SPEECH_FACTS]."""
    keys = [k for k in after if not _same_fact(k, before.get(k), after.get(k))]
    return {k: before.get(k) for k in keys}, {k: after[k] for k in keys}


def without_repeats(system, facts):
    """ADR 113 (a50: [NATIVE_FACTS] p50 7148 B beside [SPEECH_FACTS] p50 7252 B, 5220 B of identical keys): a key with the
    same values in [SPEECH_FACTS] rides once. What only [NATIVE_FACTS] holds (a ledger past speech's 45 s, the action) stays."""
    found = re.search(r"(?m)^\[NATIVE_FACTS\] (.*)$", system)
    if found is None:
        return system
    try:
        native = json.loads(found.group(1))
    except ValueError:
        return system
    if not isinstance(native, dict):
        return system
    kept = {k: v for k, v in native.items() if k not in facts or not _same_fact(k, facts[k], v)}
    return system[:found.start(1)] + json.dumps(kept, ensure_ascii=False) + system[found.end(1):]


async def fresh_reply(ask, system, read_row, *, context=None, prepare=None):
    """One factual refresh if native location/outcome changes during generation.

    Both calls use the same speech path. No text matching, replacement, silence or
    action selection. The refresh is bounded to one additional call (ADR 40).
    Optional ``prepare(row, current)`` frames the caller's trigger from these
    same reads before each generation, without additional voice-path I/O (ADR 110).
    """
    row = read_row()
    def source_identity(row):
        # Human labels may coincide for different native locations/items. The
        # existing refresh still compares those machine identities privately.
        row = row or {}
        return ((row.get("game_time") or {}).get("place"), deepcopy((row.get("native_facts") or {}).get("completed_action")))
    original_identity = source_identity(row)
    before = speech_facts(row)
    if prepare is not None:
        prepare(row or {}, before["current"])
    if context is not None:
        context.update(facts_used=before, refreshes=0)
    system = without_repeats(system, before) + "\n[SPEECH_FACTS] " + json.dumps(before, ensure_ascii=False)
    result = await ask(system)
    row = read_row()
    after = speech_facts(row)
    def identity(facts):
        current = facts["current"]
        return (current["current_place"], (current["current_time"] or {}).get("date"), current["basis"], current.get("moved_since"),
                facts["completed_action"], fact_values(facts["own_state"]), fact_values(facts["menu_container"]),
                fact_values(facts["quest_status"]),
                fact_values(facts["last_container"]),
                fact_values(facts["native_story"]),
                fact_values({k: v for k, v in facts["scene_state"].items() if k != "time"}) if facts["scene_state"] else None,
                *(fact_values(facts[k]) for k in ("current_menu", "peer_sleep", "shop_availability", "own_crops", "goal_targets", "ready_check", "harvest")))
    def part(facts):
        return day_part((facts["current"]["current_time"] or {}).get("time"))
    # fix55B (a40 T40): a known day part that turned (night -> morning) is a change; a clock that only appeared is not.
    turned = None not in (part(before), part(after)) and part(before) != part(after)
    if identity(before) != identity(after) or original_identity != source_identity(row) or turned:
        if context is not None:
            context["refreshes"] = 1
        # fix55B (a40 T40): [KINI] and the turn trigger were built before generation (bed question, 22:00); the
        # refresh carried Spring 11 06:00 only inside the diff. The newest native clock is restated as its own line.
        system += "\n" + latest_line(after["current"])
        previous, latest = changed_facts(before, after)  # ADR 113: a50 refreshes tripled the facts (up to 23.9 KB)
        system += "\n[SPEECH_FACTS_UPDATED] " + json.dumps(dict(
            previous=previous, latest=latest, relation="native_facts_changed_during_generation; kunci lain sama dengan [SPEECH_FACTS]"),
            ensure_ascii=False)
        try:
            if prepare is not None:
                prepare(row or {}, after["current"])
            refreshed = await ask(system)
        except Exception as exc:  # failed fact refresh cannot remove an available reply
            refreshed = None
            if context is not None:
                context["refresh_error"] = type(exc).__name__
        if refreshed and (not isinstance(refreshed, tuple) or refreshed[0]):
            result = refreshed
            if context is not None:
                context["facts_used"] = after
    if context is not None:
        context["delivery_facts"] = speech_facts(read_row())
    return result


def day_part(clock):
    """Coarse native day part: a change during generation refreshes the reply; a 10-minute tick does not (ADR 79)."""
    if type(clock) is not int:
        return None
    return "pagi" if clock < 1200 else "siang" if clock < 1700 else "sore" if clock < 1900 else "malam"


def latest_line(current):
    return ("[KINI_TERBARU] Pengamatan native terbaru: " + json.dumps(current, ensure_ascii=False)
            + ". Ini menggantikan [KINI], status, dan pemicu giliran yang dibuat sebelum fakta berubah; "
              "klaim pagi/malam/hari mengikuti jam ini.")


def current_line(current):
    from arti_runtime.fact_names import projection
    return ("[KINI] Lokasi dan waktu SEKARANG menurut pengamatan terakhir: "
            + json.dumps(projection(current), ensure_ascii=False)
            + ". Kejadian lampau bukan posisi sekarang. Jika data kini unknown, bilang belum tahu; "
              "jangan mengisi lokasi/waktu kini dari kejadian lama.")


_ENVELOPE = re.compile(r"\A(?:[a-z_]+: .*?\. )?(?:Discovery (?:Y\d+ \w+ \d+|\d+:\w+:\d+): ?)?(.*)\Z", re.S)
_FIRST_VERB = {"location_entered": "masuk", "item_seen": "lihat", "npc_met": "ketemu"}
_EVENT_CLOCK = re.compile(r"\A(?:\d+:)?(\w+)[: ](\d+) (\d{1,2}):?(\d{2})\Z")


def readable_fact(event):
    """ADR 144: a first ledger sighting is only first in the available records.
    An empty/restarted observer cannot establish the farm's first-ever event.
    """
    situation = event.get("situation")
    facts = event.get("facts") or {}
    kind = facts.get("kind")
    if not isinstance(situation, str) or not kind:
        return situation
    body = _ENVELOPE.match(situation).group(1).strip()
    from arti_runtime.fact_names import text
    body = text(body)
    if kind == "activity":
        return body or situation
    subject = facts.get("subject")
    if kind == "location_entered":
        from arti_runtime.place_names import place_label
        from arti_runtime.fact_names import display_name
        body = display_name(facts.get("text")) or place_label(subject) or "tempat (nama belum diketahui)"
    elif kind == "item_obtained":
        return "Pertama tercatat dapat jenis ini: %s; riwayat lama belum diketahui" % (body or text(subject))
    return "Pertama tercatat %s: %s; riwayat lama belum diketahui" % (_FIRST_VERB.get(kind, "tercatat"), text(body or subject))


def game_minutes_ago(event_time, current):
    """Game minutes since a same-day event, on the native clock. Unknown (or another day) is None."""
    now = (current or {}).get("current_time") or {}
    found = _EVENT_CLOCK.match(event_time) if isinstance(event_time, str) else None
    if not found or type(now.get("time")) is not int or not isinstance(now.get("date"), str):
        return None
    if now["date"].casefold().split(" ", 1)[-1] != "%s %s" % (found.group(1).casefold(), found.group(2)):
        return None
    minutes = now["time"] // 100 * 60 + now["time"] % 100 - int(found.group(3)) * 60 - int(found.group(4))
    return minutes if minutes >= 0 else None


def event_frame(event, current):
    facts = event.get("facts") or {}
    from arti_runtime.place_names import place_label
    raw_place = event.get("event_place") or facts.get("event_place")
    place = facts.get("place_name") or (facts.get("text") if facts.get("kind") == "location_entered" else None)
    from arti_runtime.fact_names import display_name
    place = display_name(place) or place_label(raw_place) or "tempat (nama belum diketahui)"
    return dict(event_time=event.get("event_time") or facts.get("event_time") or event.get("game_time"),
                event_place=place,
                recorded_ts=event.get("ts"), relation="past_observation",
                current_place=current.get("current_place"), current_time=current.get("current_time"),
                current_observed_ts=current.get("observed_ts"), moved_since=current.get("moved_since"),
                fact=readable_fact(event))


def event_line(event, current, native=None):
    frame = event_frame(event, current)
    ago = game_minutes_ago(frame["event_time"], current)
    if ago is not None:
        frame["game_minutes_ago"] = ago
    facts = event.get("facts") or {}
    bag = (native or {}).get("possession")
    if facts.get("kind") == "item_obtained" and isinstance(bag, dict):  # a native read only; "unknown" stays absent
        frame["in_bag_now"] = bag.get(facts.get("subject"), 0)
    return "Kejadian LAMPAU (tadi/sebelumnya; bukan lokasi kini): " + json.dumps(frame, ensure_ascii=False)
