"""Advisory arrival facts from native map doors and the existing route estimates (ADR 55)."""
import re


def supersede_failures(policy, location):
    """Fresh presence corrects a failed destination, including arrival on a different trip."""
    from arti_stardew_interact import _LOCATION_ALIASES
    removed=[]
    for old_key in list(policy.last_trips):
        old_dest=_LOCATION_ALIASES.get(old_key[1],str(old_key[1]).removeprefix('@'))
        if old_dest==location and policy.last_trips[old_key].get('result')!='done':
            removed.append(dict(origin=old_key[0],destination=old_key[1],**policy.last_trips.pop(old_key)))
    return removed


def remember_trip(policy, run, outcome, date, now, *, game_time=None, observed_location=None, arrival_verified=False):
    """One measured active-wall duration per departure map/arg, never a prediction.

    Includes failed attempts, so a long failure cannot masquerade as an arrival.
    Session-local and bounded; no route or future admission is cached here.
    """
    origin = (run.state.get('live7_baseline') or {}).get('location')
    if run.call.name != 'pergi_ke' or not origin or outcome[0] not in ('done', 'fail'):
        return None
    from arti_runtime.route_pause import pause_reason
    from arti_stardew_interact import _LOCATION_ALIASES
    destination = _LOCATION_ALIASES.get(run.call.arg, str(run.call.arg or '').removeprefix('@'))
    # A generic objective may finish as NOT_READY although its independently read
    # location proves arrival. This corrects the travel fact, not effect credit.
    if arrival_verified and observed_location == destination:
        outcome = ('done', 'observed_arrival')
    if pause_reason(outcome[1]):
        return None  # ADR 155/195: a pause on the way is neither an arrival nor a failed trip; the trip continues on her pick
    fact = dict(seconds=max(0, round(now-run.started_at)), result=outcome[0], date=list(date),
                **({'time':game_time} if type(game_time) is int else {}))
    path = list(run.state.get('travel_observed_route') or [origin])
    if outcome[0] == 'done' and len(path) > 1:
        fact['route'] = path
    start = (run.state.get('live7_baseline') or {}).get('time_of_day')
    if outcome[0] == 'done' and type(start) is int and type(game_time) is int:
        from arti_stardew_live7 import minutes
        if 600 <= start <= game_time <= 2600:
            fact['game_minutes'] = minutes(game_time)-minutes(start)
    key = (origin, run.call.arg)
    history = policy.last_trips
    if outcome[0] == 'done':
        # A later arrival supersedes failures to the same place from other origins.
        supersede_failures(policy,destination)
    samples = list((history.get(key) or {}).get('measurements', ()))
    if outcome[0] == 'done' and fact.get('game_minutes', 0) > 0 and 'route' in fact:
        samples.append({k: fact[k] for k in ('result', 'game_minutes', 'route', 'date')})
    if samples:
        fact['measurements'] = samples[-5:]  # a later failure cannot erase a measured arrival
    if outcome[0] != 'done':
        # ADR 125 (a recorded run: one place chosen many times, no arrival, the token said only that it failed): how, and how many in a row.
        fact.update(reason=str(outcome[1]), fails=(history.get(key) or {}).get('fails', 0)+1)
    history.pop(key, None)
    history[key] = fact
    while len(history) > 128:
        history.pop(next(iter(history)))
    return fact


# ADR 105 (a47 d14 J774-J1317: `0900-2100 tiba 1829 buka`, counter closed on arrival). The map publishes door hours, and the
# counter's only for an OpenShop action; other counters depend on where the owner stands. Where a counter keeps shorter
# hours than its door, this is the labelled general fact, like ADR 99's PLACE_USES. Closed weekdays stay hers to discover.
COUNTER_HOURS = {"SeedShop": (900, 1700), "AnimalShop": (900, 1600), "ScienceHouse": (900, 1700)}
COUNTER_BASIS_STATIC = "jam umum game, hafalan bukan bacaan game; penjaga dicek saat tiba"
COUNTER_BASIS_NATIVE = "native counter action"


def counter_hours(place):
    """(opens, closes, basis) of a place's counter: the native action's hours, else the labelled static fact, else None."""
    for fact in place.get("facts", ()):
        match = re.match(r"shop counter \S+; native hours (\d+)-(\d+);", str(fact))
        if match:
            return int(match[1]), int(match[2]), COUNTER_BASIS_NATIVE
    if "experience_known" in place:
        return None  # ADR 168: a remembered general schedule is not an observed counter
    hours = COUNTER_HOURS.get(place.get("id"))
    return (*hours, COUNTER_BASIS_STATIC) if hours else None


def reserve_fact(location, now, policy=None):
    from arti_runtime.travel_time import reserve, deadline
    info = reserve(location, now, policy)
    cutoff = info['cutoff_min']
    tick = (cutoff + 9) // 10 * 10
    return dict(basis=info['basis'], from_location=location, home_eta_estimate_min=info['minutes'],
                margin_min=info['margin_min'], home_target=deadline()//60*100+deadline()%60,
                triggers_at=cutoff//60*100+cutoff%60,
                first_ten_minute_clock_tick=tick//60*100+tick%60, active=info['active'])


def route_estimate(here, route, policy=None):
    from arti_runtime.travel_time import estimate
    return estimate(here, route, policy)['minutes']


def arrival_facts(place, here, now, route, policy=None, base=None):
    from arti_stardew_live7 import minutes
    from arti_runtime.travel_time import estimate
    info = estimate(here, route, policy)
    eta = info["minutes"]
    at = minutes(now) + eta if type(now) is int else None
    doors = []
    for fact in place.get("facts", ()):
        match = re.match(r"entry hours (\d+)-(\d+); door (.*?);", str(fact))
        if not match:
            continue
        opens, closes = map(int, match.group(1, 2))
        if not (0 <= opens < closes <= 2800 and opens % 100 < 60 and closes % 100 < 60):
            continue
        doors.append(dict(opens_at=opens, closes_at=closes, source_door=match[3],
                          arrives_before_closing_estimate=None if at is None else at < minutes(closes),
                          arrives_during_hours_estimate=None if at is None else minutes(opens) <= at < minutes(closes)))
    untimed = any('native WarpCommunityCenter has no opening-hour arguments' in str(f) for f in place.get('facts', ()))
    counter = counter_hours(place)
    is_shop = any(str(f).startswith('shop counter ') for f in place.get('facts', ()))
    result = dict(current_game_time=now, from_location=here,
                **({'source': place['fact_source']} if place.get('fact_source') else {}),
                **({"counter": dict(opens_at=counter[0], closes_at=counter[1], basis=counter[2],
                                    open_on_arrival=None if at is None else minutes(counter[0]) <= at < minutes(counter[1]))}
                   if counter else {'counter': dict(opens_at=None, closes_at=None, open_on_arrival=None,
                                                   basis='unknown in native counter action')} if is_shop else {}),
                travel_estimate_min=eta,
                estimate_basis=info["basis"],
                arrival_game_time_estimate=None if at is None else at // 60 * 100 + at % 60,
                native_entry_hours=doors or ("no time restriction: native WarpCommunityCenter; unlock status is separate" if untimed
                                            else "unknown: no timed native map door published"),
                entry_status=[s for s in place.get("facts", ()) if str(s).startswith("entry status now:")],
                return_home_reserve=reserve_fact(here, now, policy))
    if base:
        from copy import deepcopy
        for key in ("source", "counter", "native_entry_hours", "entry_status"):
            if key in base:
                result[key] = deepcopy(base[key])
        if isinstance(result["native_entry_hours"], list):
            for door in result["native_entry_hours"]:
                door["arrives_before_closing_estimate"] = None if at is None else at < minutes(door["closes_at"])
                door["arrives_during_hours_estimate"] = None if at is None else minutes(door["opens_at"]) <= at < minutes(door["closes_at"])
        if result.get("counter"):
            counter = result["counter"]
            counter["open_on_arrival"] = (None if at is None or type(counter.get('opens_at')) is not int
                                         or type(counter.get('closes_at')) is not int else
                                         minutes(counter["opens_at"]) <= at < minutes(counter["closes_at"]))
    return result


def door_names(after):
    """FIX81B (ADR 145): {"@Trailer": "[recorded remark omitted]"}: the native place label (world_graph) of each door arg whose id
    does not already read as that label; {} when none. Labels only, no ownership guessed."""
    from arti_runtime.fact_names import display_name
    out = {}
    for arg, row in (after or {}).items():
        label = display_name(row.get("label")) if isinstance(row, dict) else None
        if (isinstance(arg, str) and arg.startswith("@") and label
                and re.sub(r"[^a-z0-9]", "", label.casefold()) != re.sub(r"[^a-z0-9]", "", arg[1:].casefold())):
            out[arg] = label
    return out


def remembered_counter(arrival, schedule):
    from arti_stardew_live7 import minutes, _clock, mind_label
    at = arrival.get('arrival_game_time_estimate')
    start, end = schedule['opens_at'], schedule['closes_at']
    basis = '%s, dibaca %s %s' % (schedule.get('basis') or 'native counter',
                                  mind_label(schedule.get('date')), _clock(schedule.get('time')))
    return dict(arrival, counter=dict(opens_at=start, closes_at=end, basis=basis,
                open_on_arrival=None if at is None else minutes(start) <= minutes(at) < minutes(end)),
                source='; '.join(s for s in (arrival.get('source'), basis) if s))


def compact_arrivals(after):
    """Keep hours/arrival facts beside every travel arg even when route details are paged."""
    after = {k: v for k, v in after.items() if 'arrival' in v}
    if not after:
        return None
    first = next(iter(after.values()))['arrival']
    return dict(current_game_time=first['current_game_time'], from_location=first['from_location'],
                estimate_basis=first['estimate_basis'], return_home_reserve=first['return_home_reserve'],
                legend='rows=[arg,travel_estimate_min,arrival_game_time_estimate,native_doors,status,counter,last_trip]; doors=[opens_at,closes_at,before_closing_estimate,during_hours_estimate]; counter=[opens_at,closes_at,open_on_arrival], general hours, not read from the game; last_trip=active wall seconds/result/date from this map, not a prediction; null=unknown; native access/owner readiness rechecked',
                sources={arg: row['arrival']['source'] for arg, row in after.items() if row['arrival'].get('source')},
                rows=[[arg, row['arrival']['travel_estimate_min'], row['arrival']['arrival_game_time_estimate'],
                       [[d[k] for k in ('opens_at','closes_at','arrives_before_closing_estimate','arrives_during_hours_estimate')]
                        for d in row['arrival']['native_entry_hours']]
                       if isinstance(row['arrival']['native_entry_hours'], list) else None,
                       [str(s)[len('entry status now: '):].split(';')[0][:STATUS_CHARS] for s in row['arrival']['entry_status']],
                       [row['arrival']['counter'][k] for k in ('opens_at', 'closes_at', 'open_on_arrival')]
                       if 'counter' in row['arrival'] else None,
                       row.get('last_trip')]
                      for arg, row in after.items()])


# fix62 (ADR 89, witness 20261103T193853Z: 24 destinations per offer, 13000 B guard): one short token per destination on
# the page she decides from. The exact estimates, reserve and basis stay in the on-demand read (`after_travel`).
STATUS_CHARS = 24
# ADR 105: this legend rides every decision page, so its counter clause was paid for by shortening the rest; it did not grow.
DESTINATION_LEGEND = ("pintu/jam,tiba~;konter=native/umum(bukan bacaan game);konter tutup=di luar jam tiba;"
                      "tanpa baris=?;rute=read_boundary;akses/penjaga dicek tiba")


# How a trip ended, as she can judge it; the reason code itself stays in the journal and the detail read.
TRIP_FAILURES = (('no_effect_progress', 'jalan tak maju'), ('exit_not_offered', 'jalan keluar tak ketemu'),
                 ('curfew', 'keburu jam pulang'), ('route_clear_', 'jalan tertutup'), ('route_blocked_', 'jalan tertutup'), ('route_no_', 'jalan tertutup'),
                 ('UNCERTAIN', 'terputus'), ('gather_full_bag', 'tas penuh'), ('player_cannot_move', 'tak bisa gerak'),
                 ('game_dialogue_mid_walk', 'dihentikan pesan game'))  # ADR 231 part B1


def trip_failure(trip):
    """``gagal 3x jalan tak maju``: a measured record of consecutive failures from this map, not advice."""
    reason = str(trip.get('reason') or '')
    how = next((text for prefix, text in TRIP_FAILURES if reason.startswith(prefix)), None)
    fails = trip.get('fails') or 0
    return 'gagal' + (' %dx' % fails if fails > 1 else '') + (' ' + how if how else '')


def trip_stamp(trip):
    from arti_runtime.plan_dates import ordinal, label
    date=trip.get('date')
    date=('Y%s %s %s' % tuple(date)) if isinstance(date,(list,tuple)) and len(date)==3 else date
    day=ordinal('stardew',date)
    text=label('stardew',day) if day else 'tanggal tidak diketahui'
    clock=trip.get('time')
    return text+(' %02d:%02d' % (clock//100,clock%100) if type(clock) is int else ' jam tidak diketahui')


def destination_token(row, *, dated=True):
    """One destination as a short fact, e.g. ``0900-1700 tiba 1010 buka``; '' when nothing is known about it."""
    arrives, doors = row[2], row[3]
    status = row[4] if len(row) > 4 else ()
    parts = []
    if row[0] in ("mine", "@Mine") and type(row[1]) is int:
        parts.append("perkiraan %d menit game" % row[1])
    if doors:
        parts += ["%04d-%04d" % (doors[0][0], doors[0][1])]
        parts += ["%04d-%04d" % (d[0], d[1]) for d in doors[1:] if d[:2] != doors[0][:2]]
    if arrives is not None and (doors or status or len(row) > 5 and row[5]):
        parts.append("tiba %04d" % arrives)
    if doors and doors[0][3] is not None:
        parts.append("buka" if doors[0][3] else "belum buka" if doors[0][2] else "tutup")
    if status and str(status[0]) != "open":
        parts.append(str(status[0]))
    counter = row[5] if len(row) > 5 else None
    if counter:
        if all(type(v) is int for v in counter[:2]):
            parts.append("konter %04d-%04d" % (counter[0], counter[1]) +
                         (" tutup" if counter[2] is False else " tiba?" if counter[2] is None else ""))
        else:
            parts.append('konter jam layanan/tiba belum diketahui')
    trip = row[6] if len(row) > 6 else None
    if trip:
        parts.append((trip_stamp(trip)+': ' if dated else '') + 'lalu %ss ' % trip['seconds']
                     + (trip_failure(trip) if trip['result'] != 'done' else 'tiba'))
        if row[0] in ('mine', '@Mine') and type(trip.get('game_minutes')) is int and trip['result'] == 'done':
            parts.append('terukur %d menit game dari map ini' % trip['game_minutes'])
    return " ".join(parts)


def compact_destinations(facts, *, share_dates=True):
    """The render form of compact_arrivals: {arg: token} plus the one reserve fact that bounds a trip."""
    if not isinstance(facts, dict) or not isinstance(facts.get('rows'), list):
        return facts
    trips=[row[6] for row in facts['rows'] if len(row)>6 and row[6]]
    stamps={trip_stamp(trip) for trip in trips}
    shared=share_dates and len(trips)>1 and len(stamps)==1
    tokens = {row[0]: token + (" (sumber: %s)" % facts['sources'][row[0]]
                              if (facts.get('sources') or {}).get(row[0]) else "")
              for row in facts['rows'] if (token := destination_token(row,dated=not shared))}
    history = any(len(row) > 6 and row[6] for row in facts['rows'])
    now = facts.get('current_game_time')
    return dict(legend=DESTINATION_LEGEND + ('; lalu=detik nyata aktif dari map ini, bukan perkiraan' if history else ''), rows=tokens,
                **({'riwayat_pada':next(iter(stamps))} if shared else {}),
                **({'clock': '%02d:%02d' % (now//100, now%100)} if type(now) is int else {}))
