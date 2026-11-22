"""ADR 167: measured route estimates for admission, never movement permission."""
from arti_runtime.native_rows import native_rows
from collections import deque
from math import ceil

# Attempt 66 d1 J36-J223 / J365-J488: 60+50 native game minutes,
# four map crossings, 78.2 active seconds. Excludes the social pause.
# Cold-start calibration is a route-length estimate, not an exact-map promise.
CALIBRATION_MINUTES_PER_HOP = 110 / 4
CLOCK_QUANTUM = 10
SAFETY_MARGIN = 60  # keep the existing decision/repair reserve; never trade it for faster walking
BED_MINUTES = 15  # unmeasured local bed interaction; explicitly retained safety allowance
EXIT_MINUTES = 90  # ADR 160 ladder/Leave allowance; never charged again as floor work
MAX_TRIPS = 128


def clock(value):
    return "%02d:%02d" % (value // 60 % 24, value % 60)


def observe(policy, location, context):
    """Retain only native topology, not targets or executable offers."""
    graph = ((context or {}).get("world_knowledge") or {}).get("world_graph") or {}
    current = graph.get("current")
    if current != location and not (str(location).startswith("UndergroundMine") and current == "Mine"):
        return  # a catalogue from another map cannot define this route's origin
    if graph.get("home"):
        policy.travel_home = graph["home"]
    edges = {}
    for edge in native_rows(graph.get("connections", ())):
        # Native [from,to,kind,locked,travel,player,id], directed and open.
        if isinstance(edge, (list, tuple)) and len(edge) >= 6 and edge[3] is None and edge[4] is True and edge[5] is True:
            edges.setdefault(edge[0], set()).add(edge[1])
    for place in native_rows(graph.get("places", ())):
        route = place.get("motor_route", place.get("route"))
        if not isinstance(route, list):
            continue
        if place.get("reachable_now") is not True or place.get("go_to_available", True) is not True:
            continue
        previous = current
        for dest in route:
            if isinstance(previous, str) and isinstance(dest, str) and previous != dest:
                edges.setdefault(previous, set()).add(dest)
            previous = dest
    policy.travel_topology = edges


def route(policy, origin, destination):
    if origin == destination:
        return []
    edges = getattr(policy, "travel_topology", {})
    queue = deque([(origin, [])])
    seen = {origin}
    while queue:
        here, path = queue.popleft()
        for dest in sorted(edges.get(here, ())):
            if dest == destination:
                return path + [dest]
            if dest not in seen:
                seen.add(dest)
                queue.append((dest, path + [dest]))
    return None


def measured(policy):
    return _measured(getattr(policy, "last_trips", {}))


def _measured(trips):
    rows = [sample for row in trips.values()
            for sample in row.get("measurements", [row])]
    return [r for r in rows
            if r.get("result") == "done" and type(r.get("game_minutes")) is int
            and r["game_minutes"] > 0 and len(r.get("route", ())) > 1][-MAX_TRIPS:]


def estimate(origin, ahead, policy=None):
    """Exact observed route first, then reverse, then measured map-hop length.

    Use the slowest of the last five relevant arrivals, rounded up one native
    clock quantum. The separate 60m safety margin covers variability/decisions.
    Failed/paused trips and wall time never masquerade as successful arrivals.
    """
    path = [origin] + list(ahead)
    if len(path) == 1:
        return dict(minutes=0, basis="already at destination", route=path)
    samples = measured(policy)
    exact = [r for r in samples if r["route"] == path]
    reverse = [r for r in samples if r["route"] == path[::-1]]
    chosen = exact or reverse
    if chosen:
        raw = max(r["game_minutes"] for r in chosen[-5:])
        basis = "recent measured same route" if exact else "recent measured reverse route; direction estimated"
    else:
        # Floor escape is not evidence of outdoor walking speed.
        walking = [r for r in samples if not any(s.startswith("UndergroundMine") for s in r["route"])]
        rates = [r["game_minutes"] / (len(r["route"]) - 1) for r in walking[-5:]]
        raw = (max(rates) if rates else CALIBRATION_MINUTES_PER_HOP) * (len(path) - 1)
        basis = "recent measured map-hop length" if rates else "a66 measured 110m/4 map hops; cold-start length estimate"
    return dict(minutes=ceil(raw / CLOCK_QUANTUM) * CLOCK_QUANTUM + CLOCK_QUANTUM,
                basis=basis + "; clock uncertainty 10m", route=path)


def home(location, policy=None):
    """Route actually ahead, plus separately stated bed/ladder allowances."""
    import arti_stardew_live7 as L
    own = getattr(policy, "travel_home", None)
    inside = str(location or "").startswith("UndergroundMine")
    origin = "Mine" if inside else location
    exits = [r for r in measured(policy) if r["route"] == [location, "Mine"]] if inside else []
    exit_minutes = max(r["game_minutes"] for r in exits[-5:]) + CLOCK_QUANTUM if exits else EXIT_MINUTES
    exit_basis = ("; measured ladder/Leave %dm" % exit_minutes if exits else "; ladder/Leave allowance 90m") if inside else ""
    ahead = route(policy, origin, own) if own else None
    if ahead is not None:
        result = estimate(origin, ahead, policy)
        result["minutes"] += BED_MINUTES + (exit_minutes if inside else 0)
        result["basis"] += "; bed allowance 15m" + exit_basis
        return result
    # Missing topology is not evidence of a fast way home. Retain the old
    # conservative bound ONLY until the native route becomes observable.
    result = dict(minutes=L.legacy_home_eta(location), route=None,
                  basis="native route unavailable; conservative unmeasured legacy bound")
    # An actual successful home trip is still usable during a catalogue gap.
    rows = [r for (start, arg), last in getattr(policy, "last_trips", {}).items()
            for r in last.get("measurements", [last])
            if start == location and arg == "home" and r.get("result") == "done"
            and type(r.get("game_minutes")) is int and r["game_minutes"] > 0]
    if rows:
        result.update(minutes=max(r["game_minutes"] for r in rows) + CLOCK_QUANTUM + BED_MINUTES,
                      basis="measured home arrival; clock uncertainty 10m; bed allowance 15m")
    return result


def deadline():
    import arti_stardew_live7 as L
    return min(L.minutes(L.HOME_TARGET), L.minutes(L.PASS_OUT_CLOCK))


def reserve(location, now, policy=None):
    import arti_stardew_live7 as L
    info = home(location, policy)
    margin = max(SAFETY_MARGIN, L.HOME_MARGIN)
    cutoff = deadline() - info["minutes"] - margin
    return dict(**info, margin_min=margin, cutoff_min=cutoff, until=clock(cutoff),
                active=type(now) is int and L.minutes(now) >= cutoff,
                deadline=clock(deadline()))


def admission_budget(call, facts, policy=None):
    import arti_stardew_live7 as L
    from arti_stardew_interact import _LOCATION_ALIASES
    work = L.WORK_MINUTES.get(call.name, 30)
    origin = facts.location
    eta = home(origin, policy)
    outward = None
    if call.name == "lawan_mine":
        from arti_runtime.mines_offer import work_minutes
        work = work_minutes(origin, work)  # complete allowance counts ladder/Leave once
    elif call.name == "pergi_ke":
        dest = call.arg[1:] if str(call.arg).startswith("@") else _LOCATION_ALIASES.get(call.arg)
        if dest:
            ahead = route(policy, origin, dest)
            if ahead is not None:
                outward = estimate(origin, ahead, policy)
                work = outward["minutes"]
            else:
                work = L.outward_minutes(origin, dest)
                outward = dict(minutes=work, basis="native route unavailable; conservative unmeasured bound")
            work += L.DESTINATION_WORK_MINUTES
            eta = home(dest, policy)
    margin = max(SAFETY_MARGIN, L.HOME_MARGIN)
    cutoff = deadline() - work - eta["minutes"] - margin
    return dict(work_min=work, home_min=eta["minutes"], margin_min=margin,
                cutoff_min=cutoff, until=clock(cutoff), basis=eta["basis"], outward=outward,
                deadline=clock(deadline()))


def export(policy):
    return [dict(origin=a, destination=b, **r) for (a, b), r in policy.last_trips.items()][-MAX_TRIPS:]


def restore(policy, rows):
    for row in rows[-MAX_TRIPS:] if isinstance(rows, list) else ():
        if not isinstance(row, dict) or not isinstance(row.get("origin"), str) or not isinstance(row.get("destination"), str):
            continue
        value = {k: row[k] for k in ("seconds", "result", "date", "time", "game_minutes", "route", "reason", "fails", "measurements") if k in row}
        path = value.get("route")
        if path is not None and (not isinstance(path, list) or not all(isinstance(s, str) for s in path) or len(path) > 128):
            continue
        history = value.get("measurements")
        if history is not None:
            value["measurements"] = [r for r in history[-5:] if isinstance(r, dict)
                and r.get("result") == "done" and type(r.get("game_minutes")) is int
                and 0 < r["game_minutes"] <= 1200 and isinstance(r.get("route"), list)
                and 1 < len(r["route"]) <= 128 and all(isinstance(s, str) for s in r["route"])] if isinstance(history, list) else []
        policy.last_trips[(row["origin"], row["destination"])] = value
