"""Festival calendar, in-map, and leave facts from the game's own data (ADR 197). Generic for every festival."""
from arti_runtime.native_rows import native_rows
from collections.abc import Mapping

LEAVE = ("Peta festival. pergi_ke home artinya meninggalkan festival; pergi_ke tujuan mana pun juga: "
         "game menanyakan semua pemain, festival berakhir untuk semua, jam menjadi malam, kamu ada di depan rumah. "
         "Tetap di sini juga boleh.")


def _clock(value):
    if type(value) is not int or value < 0:
        return None
    return "%02d:%02d" % (value // 100 % 24, value % 100)


def _dates(row):
    return row.get("dates") or ()


def today_rows(calendar, today):
    """Native festival rows whose dates include ``today`` (Y1 spring 13 form, or already-grounded date_fact)."""
    out = []
    for row in calendar or ():
        if not isinstance(row, Mapping) or not row.get("name"):
            continue
        if row.get("kind") and row.get("kind") != "festival":
            continue
        dates = _dates(row)
        if today and (today in dates or any(str(d) == str(today) for d in dates)):
            out.append(row)
        elif today and row.get("kind") == "festival" and ": today" in str(row.get("date_fact") or ""):
            out.append(row)
    return out


def day_line(calendar, today, *, clock=None, names=None, catalog=None, here=None, policy=None):
    """Which festival, where, from when to when, what is closed. None when the calendar has no festival today."""
    rows = today_rows(calendar, today)
    if not rows:
        return None
    parts = []
    for row in rows[:2]:
        name = str(row.get("name") or "festival")
        loc = row.get("location")
        where = None
        if isinstance(loc, str) and loc:
            from arti_runtime.place_names import place_label
            native = next((p.get("label") for p in native_rows(((catalog or {}).get("world_graph") or {}).get("places", ()))
                           if p.get("id") == loc), None)
            where = native or (names or {}).get(loc) or place_label(loc) or loc
        start, end = _clock(row.get("starts_at")), _clock(row.get("ends_at"))
        bit = name
        if where:
            bit += " di " + where
        if start and end:
            bit += " %s–%s" % (start, end)
        elif start:
            bit += " dari " + start
        if type(row.get("starts_at")) is int and row["starts_at"] < 1900 and row.get("passive") is not True:
            bit += "; toko di kota tutup sementara festival ini"
        if loc:
            from arti_stardew_world import location_arg
            bit += "; pergi_ke " + location_arg(loc)
            graph = (catalog or {}).get("world_graph") or {}
            place = next((p for p in native_rows(graph.get("places", ())) if p.get("id") == loc), {})
            route = [] if here == loc else place.get("motor_route", place.get("route"))
            if here != loc and not route:
                route = None
            if route is not None and type(clock) is int:
                from arti_runtime.travel_time import estimate
                from arti_stardew_live7 import minutes
                eta = estimate(here, route, policy)["minutes"]
                at = minutes(clock) + eta
                bit += "; ~%dm, tiba %s" % (eta, _clock(at // 60 * 100 + at % 60))
                if type(row.get("ends_at")) is int:
                    bit += " (masih sempat)" if at < minutes(row["ends_at"]) else " (lewat jam masuk)"
            else:
                bit += "; waktu jalan belum diketahui"
        if type(clock) is int and type(row.get("starts_at")) is int and clock < row["starts_at"]:
            bit += "; belum mulai"
        parts.append(bit)
    return "hari ini festival: " + "; ".join(parts)


def leave_line(location, calls, movement=None):
    """Any trip off a festival map ends it. Staying is allowed. None when she is not on a festival map."""
    from arti_stardew_event_control import festival_free_roam
    festival = location == "Temp" or festival_free_roam(movement)
    if not festival:
        return None
    going = any(getattr(c, "name", None) == "pergi_ke" for c in calls or ())
    return LEAVE if going else ("Ini peta festival. " + LEAVE.split(". ", 1)[-1])


def inside_line(location, movement=None, actions=None, calendar=None, today=None, names=None):
    """On a festival map: its name, who starts the main event if native exposes them."""
    from arti_stardew_event_control import festival_free_roam
    if location != "Temp" and not festival_free_roam(movement):
        return None
    name = None
    if calendar is not None:
        rows = today_rows(calendar, today)
        if rows:
            name = rows[0].get("name")
    move = movement if isinstance(movement, Mapping) else {}
    actions = actions if isinstance(actions, Mapping) else {}
    fid = actions.get("festival_id") or move.get("festival_id")
    host = actions.get("festival_host")
    bits = []
    if name:
        bits.append("festival " + str(name))
    elif fid:
        bits.append("festival " + str(fid))
    else:
        bits.append("peta festival")
    if host:
        bits.append("yang memulai acara utama: " + str(host))
    if actions.get("main_event_running") is True:
        bits.append("acara utama sedang berjalan")
    return "; ".join(bits)


def _targets(actions):
    raw = (actions or {}).get("targets") if isinstance(actions, Mapping) else None
    if isinstance(raw, Mapping):
        return list(raw.items())
    if isinstance(raw, (list, tuple)):
        return [(t.get("source_fact_id") or t.get("handle") or "e%d" % i, t)
                for i, t in enumerate(raw) if isinstance(t, Mapping)]
    return []


def _arg(identity):
    from hashlib import sha256
    return "event-" + sha256(identity.encode()).hexdigest()[:16]


def event_choices(snapshot):
    """Native controls keyed by target identity, independent of offer order.

    ADR 240: a person is one choice by actor identity and a press cell one choice by its route source, whether the
    native plan has routed it yet (``handle``) or the event map only shows it so far (``handle`` None)."""
    import arti_stardew_skills as S
    actions = S._g(snapshot, "event_actions") or {}
    people = actions.get("people")
    if not isinstance(people, Mapping):
        return {}
    choices = {}
    for handle, target in _targets(actions):
        source = target.get("source_fact_id", "")
        parts = source.split(":")
        actor = people.get(parts[2]) if len(parts) >= 4 and target.get("kind") in (
            "festival_npc", "festival_npc_route") else None
        tile = (target.get("tile_x"), target.get("tile_y"))
        choices.setdefault(_arg(f"event:person:{parts[2]}" if actor else source), dict(
            handle=handle, name="ngobrol_hadiah" if actor else "aksi", label=actor, kind=target.get("kind"),
            tile=tile if all(type(v) is int for v in tile) else None))
    seen = actions.get("seen") if isinstance(actions.get("seen"), Mapping) else {}
    for row in seen.get("people") or ():
        if row.get("talk") is True and isinstance(people.get(row.get("id")), str):
            arg = _arg(f"event:person:{row['id']}")
            if arg in choices:
                choices[arg]["tile"] = tuple(row["tile"])  # where the person stands, not the route's capture cell
            else:
                choices[arg] = dict(handle=None, name="ngobrol_hadiah", label=people[row["id"]],
                                    kind="festival_npc_route", tile=tuple(row["tile"]))
    for row in seen.get("things") or ():
        choices.setdefault(_arg(row["source"]), dict(handle=None, name="aksi", label=None, kind=row["kind"],
                                                     tile=tuple(row["tile"])))
    return choices


PAGE_LIMIT = 8  # map_affordances.LIMIT: eight people and eight things on one page, the rest counted
# ADR 240: attempt 89 published its first event routes 10.7-18.2 s after entering (d1 J1110-J1156). A choice made
# before then waits for its route this long (about 43 game minutes at 7 s per 10), then says none came.
ROUTE_WAIT_S = 30.0


def event_page(snapshot):
    """ADR 240: the people and press cells her page offers, nearer first, with how many are left for a closer look.

    Before the route plan is ready every choice ranks by distance from her tile; once it is ready, routed choices
    (reachable now) rank first. Things are described by ADR 236 B's rule (where they are, from her tile)."""
    import arti_stardew_skills as S
    from arti_runtime.map_affordances import WORDS, _relative
    actions = S._g(snapshot, "event_actions") or {}
    seen = actions.get("seen") if isinstance(actions.get("seen"), Mapping) else {}
    ready = seen.get("routes_ready") is True
    here = S._wave1_xy(S._g(S._g(snapshot, "motor"), "tile"))
    def rank(item):
        arg, row = item
        far = abs(row["tile"][0] - here[0]) + abs(row["tile"][1] - here[1]) if here and row["tile"] else 9999
        return (ready and row["handle"] is None, far, row["tile"] or (9999, 9999), arg)
    rows = sorted(event_choices(snapshot).items(), key=rank)
    people = [(arg, row) for arg, row in rows if row["name"] == "ngobrol_hadiah"][:PAGE_LIMIT]
    things = [(arg, row) for arg, row in rows if row["name"] == "aksi"][:PAGE_LIMIT]
    labels = {}
    for arg, row in things:
        where = _relative(row["tile"], here, "tempatmu") if here and row["tile"] else "di peta acara ini"
        labels[arg] = "Bisa " + WORDS["press"] + " " + where
    people_total = max(seen.get("people_total", 0) if type(seen.get("people_total")) is int else 0,
                       sum(row["name"] == "ngobrol_hadiah" for _, row in rows))
    things_total = max(seen.get("things_total", 0) if type(seen.get("things_total")) is int else 0,
                       sum(row["name"] == "aksi" for _, row in rows))
    counts = dict(ditampilkan=len(things), belum_ditampilkan=max(0, things_total - len(things)),
                  orang_ditampilkan=len(people), orang_belum_ditampilkan=max(0, people_total - len(people)))
    return dict(people={arg: row["label"] for arg, row in people}, things=labels, counts=counts,
                calls=[(row["name"], arg) for arg, row in people + things])


def action_calls(snapshot):
    """Working festival choices the native event_actions list already exposes (talk, shop, host)."""
    import arti_stardew_skills as S
    actions = S._g(snapshot, "event_actions") or {}
    if not isinstance(actions, Mapping) or not actions:
        return []
    if isinstance(actions.get("people"), Mapping):
        if "seen" in actions:  # ADR 240: the bounded, nearest-first selection the page shows
            return [S.SkillCall(name, arg) for name, arg in event_page(snapshot)["calls"]]
        return [S.SkillCall(row["name"], arg) for arg, row in event_choices(snapshot).items()]
    out, seen = [], set()
    labels = actions.get("route_labels") if isinstance(actions.get("route_labels"), Mapping) else {}
    kinds = {"festival_npc", "festival_npc_route", "festival_shop", "festival_shop_route", "shop",
             "festival_seat", "festival_seat_route", "festival_host", "festival_host_route"}
    for handle, target in _targets(actions):
        if not isinstance(target, Mapping):
            continue
        kind = str(target.get("kind") or "")
        source = target.get("source_fact_id") or handle
        if kind not in kinds and not str(labels.get(source) or "").strip():
            continue
        arg = handle or source
        if not arg or arg in seen:
            continue
        seen.add(arg)
        tile = target.get("tile") or ((target.get("tile_x"), target.get("tile_y"))
                                      if type(target.get("tile_x")) is int else None)
        if isinstance(tile, (list, tuple)) and len(tile) == 2 and all(type(v) is int for v in tile):
            out.append(S.SkillCall("aksi", arg, tile=tuple(tile)))
        else:
            out.append(S.SkillCall("aksi", arg))
    return out


def labels(snapshot):
    import arti_stardew_skills as S
    actions = S._g(snapshot, "event_actions") or {}
    if not isinstance(actions, Mapping):
        return {}
    route = actions.get("route_labels") if isinstance(actions.get("route_labels"), Mapping) else {}
    host = actions.get("festival_host")
    out = {}
    for handle, target in _targets(actions):
        source = (target or {}).get("source_fact_id") if isinstance(target, Mapping) else None
        name = route.get(source) or route.get(handle)
        kind = (target or {}).get("kind") if isinstance(target, Mapping) else None
        if not name and kind in ("festival_shop", "festival_shop_route", "shop"):
            name = "toko festival"
        if not name and host and kind in ("festival_npc", "festival_npc_route", "festival_host", "festival_host_route"):
            name = "bicara dengan " + str(host)
        if name:
            out[handle] = name
            if source:
                out[source] = name
    return out


def press(call, snap, state):
    """One event_action for a festival aksi, or None so ordinary aksi continues."""
    import arti_stardew_skills as S
    if getattr(call, "name", None) not in ("aksi", "ngobrol_hadiah") or not call.arg:
        return None
    actions = S._g(snap, "event_actions") or {}
    if not isinstance(actions, Mapping):
        return None
    last = S._g(S._g(snap, "working_memory"), "last_action")
    last_pin = tuple(S._g(last, key) for key in ("request_id", "lifecycle", "reason"))
    if state.get("festival_act_sent") == call.arg:
        if (last and S._g(last, "cleanup_confirmed") is True
                and last_pin != state.get("festival_act_prev")):
            lifecycle = S._g(last, "lifecycle")
            effect = S._g(last, "world_effect_status")
            if lifecycle in ("rejected", "not_taken") and effect == "zero_effect":
                return S.Step("fail", reason=S._g(last, "reason") or "event_action_refused")
            if lifecycle == "completed" and effect in ("observed_event_effect", "observed_world_delta", "unchanged"):
                return S.Step("done", reason="observed_event_effect")
        return S._wait(state, "festival_act", "event_action_unverified", "Amati hasil aksi festival", S.WAIT_S)
    if call.arg.startswith("event-"):
        row = event_choices(snap).get(call.arg)
        if row is None and not actions and "wait:event_route" in state:
            # A read without the event block (stale or in flight) while waiting for a route: keep the same bound.
            return S._wait(state, "event_route", "event_target_changed", "Amati pilihan di acara", ROUTE_WAIT_S)
        if row is None or row["name"] != call.name:
            return S.Step("fail", reason="event_target_changed")
        if row["handle"] is None:
            # ADR 240: chosen from what the map shows before its route was planned. The native plan publishes
            # the route (then the same event_action runs), or says it is complete without one.
            if ((actions.get("seen") or {}).get("routes_ready") is True
                    and state.get("wait:event_route") is not None):
                return S.Step("fail", reason="event_route_not_offered")
            return S._wait(state, "event_route", "event_route_not_offered", "Amati jalan ke pilihan di acara",
                           ROUTE_WAIT_S)
        handle = row["handle"]
    else:
        if call.name != "aksi":
            return None
        handle = next((h for h, t in _targets(actions) if call.arg in (h, t.get("source_fact_id"))), None)
        if handle is None:
            return None
    state["festival_act_sent"] = call.arg
    state["festival_act_prev"] = last_pin
    return S._decision(S.act("event_action", {"handle": handle, "btn": "A"}))
