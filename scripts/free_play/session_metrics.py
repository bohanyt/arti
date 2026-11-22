"""Report-only tempo, social and variety measurements. Offline, ADR 142.

Shares are fractions (0..1), never percentages. No targets are inferred here.
"""
from __future__ import annotations

import json
import math

from scripts.free_play.goal_gate import measure

METRICS = {
    'tempo': ('seconds_per_decision', 'game_minutes_per_decision', 'decisions_per_game_day', 'model_wait_share'),
    'social': ('talks_started_per_game_day', 'talks_finished_per_game_day', 'gifts_started_per_game_day',
               'gifts_finished_per_game_day', 'walks_completed_across_maps_per_game_day',
               'approach_replans_per_game_day', 'route_pauses_per_game_day',
               'route_pauses_then_talk_per_game_day', 'route_pauses_then_same_dest_per_game_day',
               'route_pauses_then_other_per_game_day', 'friendship_points_delta', 'hearts_gained'),
    'variety': ('off_farm_cabin_share',),
    'mines': ('floors_gained_per_visit', 'ways_down_offered', 'ways_down_chosen', 'ways_down_completed',
              'fight_outcomes_by_class', 'minutes_in_mines_per_floor_gained'),
}


def number(v):
    return type(v) in (int, float) and math.isfinite(v)


def overlap(a, b, intervals):
    return sum(max(0, min(b, hi) - max(a, lo)) for lo, hi in intervals)


def clock(v):
    if type(v) is int and 0 <= v <= 2800 and v % 100 < 60:
        return v // 100 * 60 + v % 100
    return None


def model_wait(evidence, journals, active_s):
    """Union logical waits per process. Timeout ends waiting even if worker returns later.

    Provider clocks are monotonic, so they cannot be aligned to wall-clock stall
    intervals. Report the raw decision-lane wait / active proxy explicitly.
    """
    from scripts.free_play.evaluate import merge_intervals, interval_seconds
    total, refs, incomplete, calls = 0, [], 0, 0
    for path, _ in journals:
        log = path.parent.parent / 'stdout.txt'
        if not log.is_file():
            incomplete += 1
            continue
        evidence.track(log)
        groups = {}
        for n, line in enumerate(log.read_text(encoding='utf-8-sig', errors='replace').splitlines(), 1):
            if not line.startswith('[StardewProvider] '):
                continue
            try:
                r = json.loads(line[len('[StardewProvider] '):])
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get('lane') != 'background' or not r.get('generation_id'):
                continue
            t = r.get('monotonic_ns')
            if not number(t):
                continue
            phase = r.get('phase') or r.get('ev')
            if phase in ('logical_wait_start', 'physical_worker_complete', 'logical_timeout', 'logical_wait_cancelled'):
                groups.setdefault(r['generation_id'], {}).setdefault(phase, (t / 1e9, n))
        intervals = []
        for phases in groups.values():
            start = phases.get('logical_wait_start')
            stops = [v for k, v in phases.items() if k != 'logical_wait_start']
            if start is None:
                continue
            calls += 1
            stop = min(stops, default=None)
            if stop is None or stop[0] < start[0]:
                incomplete += 1
                continue
            intervals.append((start[0], stop[0]))
        total += interval_seconds(merge_intervals(intervals))
        if intervals:
            refs.append(evidence.ref(log))
        elif not groups:
            incomplete += 1
    value = total / active_s if refs and active_s and not incomplete else None
    return measure(round(value, 4) if value is not None else None, refs,
        missing='complete decision-lane provider logical start/end phases and active time',
        wait_seconds=round(total, 3) if refs else None, calls=calls, incomplete=incomplete,
        definition='union of background logical waits in each day stdout, including failures/superseded replies; ends at first timeout/cancel/worker completion; raw wait divided by active proxy, monotonic waits cannot be wall-stall clipped')


def session_metrics(evidence, journals, endurance):
    timeline = evidence.rows(evidence.run / 'timeline.jsonl')
    stamps = {(str(r.get('file', '')).replace('\\', '/'), r.get('line')): r.get('at') for _, r in timeline}
    intervals = endurance.get('active_intervals') or []
    active_s = sum(b - a for a, b in intervals) if intervals else None
    if active_s is None and number(endurance.get('active_minutes')):
        active_s = endurance['active_minutes'] * 60
    decisions, decision_refs, dates, date_refs = 0, [], {}, []
    off_s, covered_s, location_refs = 0, 0, []
    social = {k: {} for k in ('talks_started', 'talks_finished', 'gifts_started', 'gifts_finished',
                              'walks_completed_across_maps', 'approach_replans',
                              'route_pauses', 'route_pauses_then_talk', 'route_pauses_then_same_dest',
                              'route_pauses_then_other')}
    mines = dict(visits=[], floors=[], offered={}, chosen={}, completed={}, fights={}, mine_s=0.0)
    snapshots = []
    instrumented = False
    for path, rows in journals:
        relative = evidence.ref(path)
        at, positions = None, []
        for n, r in rows:
            ref = f'{relative}:J{n}'
            explicit = r.get('at')
            receipt = stamps.get((relative, n))
            at = explicit if number(explicit) else receipt if number(receipt) else at
            game = r.get('game') or {}
            if not isinstance(game, dict):
                game = {}
            date, minutes = game.get('date'), clock(game.get('time'))
            if isinstance(date, list) and len(date) == 3 and minutes is not None:
                key = tuple(date)
                dates.setdefault(key, []).append(minutes)
                date_refs.append(ref)
            if number(at) and isinstance(game.get('location'), str):
                positions.append((at, game['location'], ref))
            if r.get('ev') == 'skill_model_decision' and r.get('choice') != 'invalid':
                decisions += 1
                decision_refs.append(ref)
            instrumented |= r.get('ev') == 'skill_session_started'
            if r.get('ev') == 'progress_snapshot':
                snapshots.append((ref, r.get('progress') or {}))
            pinned = r.get('pinned') or {}
            if not isinstance(pinned, dict):
                pinned = {}
            kind = pinned.get('kind') if r.get('ev') == 'objective_progress' else None
            goal = r.get('goal_id')
            if goal and kind in ('talk_npc', 'gift'):
                key = (relative, goal, pinned.get('npc_name') or pinned.get('name'), pinned.get('target_ref'))
                social['talks_started' if kind == 'talk_npc' else 'gifts_started'].setdefault(key, ref)
            if (goal and r.get('ev') == 'objective_progress' and r.get('kind') == 'talk_npc'
                    and r.get('dialogue_closed') is True):
                social['talks_finished'].setdefault((relative, goal), ref)
            if (r.get('ev') == 'extras_effect' and r.get('kind') == 'npc_gift'
                    and r.get('outcome') == 'GiftGiven' and r.get('world_effect_status') == 'observed_extras_effect'):
                social['gifts_finished'].setdefault((relative, r.get('request_id') or goal or n), ref)
            if r.get('ev') == 'live7_route_continue':
                social['walks_completed_across_maps'].setdefault(
                    (relative, r.get('session_id'), r.get('destination'), r.get('map')), ref)
            if (r.get('ev') == 'part_live_row' and r.get('skill') == 'pergi_ke'
                    and r.get('outcome_class') == 'VERIFIED'):
                row_evidence = r.get('evidence') if isinstance(r.get('evidence'), dict) else {}
                start = r.get('from_location') or row_evidence.get('from_location')
                loc = r.get('location') or row_evidence.get('location')
                route = row_evidence.get('observed_route') or r.get('route')
                if (start and loc and start != loc) or (isinstance(route, list) and len({x for x in route if x}) > 1):
                    social['walks_completed_across_maps'].setdefault((relative, goal or n, 'verified'), ref)
            if r.get('ev') in ('approach_replan_decision', 'talk_follow_replan'):
                social['approach_replans'].setdefault((relative, n), ref)
            if r.get('ev') == 'skill_offer':
                names = r.get('interact_targets') or r.get('nama') or {}
                skills = r.get('skills') or ()
                aksi = next((e for e in skills if isinstance(e, dict) and e.get('name') == 'aksi'), None)
                labels = (aksi or {}).get('nama') or {}
                for row in names if isinstance(names, list) else ():
                    if isinstance(row, dict) and row.get('kind') in ('ladder_down', 'shaft'):
                        mines['offered'].setdefault((relative, row.get('ref')), ref)
                for arg, label in labels.items() if isinstance(labels, dict) else ():
                    if 'turun' in str(label).casefold() or 'lantai' in str(label).casefold():
                        mines['offered'].setdefault((relative, arg), ref)
            if r.get('ev') in ('skill_started', 'skill_model_decision') and r.get('skill') == 'aksi':
                arg = r.get('arg') or (r.get('choice') or {}).get('arg')
                if isinstance(arg, str) and 'xt:mine:' in arg and arg in {k[1] for k in mines['offered']}:
                    mines['chosen'].setdefault((relative, arg, n), ref)
                elif isinstance(arg, str) and 'xt:mine:' in arg and r.get('ev') == 'skill_started':
                    mines['chosen'].setdefault((relative, arg, n), ref)
            if ((r.get('ev') == 'extras_effect' and r.get('kind') in ('ladder_down', 'shaft'))
                    or (r.get('ev') == 'skill_leg' and r.get('reason') == 'verified:ladder')
                    or (r.get('ev') == 'skill_finished' and r.get('skill') == 'aksi'
                        and r.get('verdict') == 'observed_generic_readback'
                        and isinstance(r.get('arg'), str) and 'xt:mine:' in r['arg'])):
                mines['completed'].setdefault((relative, n), ref)
            if r.get('ev') == 'part_live_row' and r.get('skill') == 'lawan_mine':
                mines['fights'].setdefault((relative, n), (r.get('outcome_class') or r.get('reason') or 'unknown', ref))
            if r.get('ev') == 'live7_route_pause':
                key = (relative, n)
                social['route_pauses'].setdefault(key, ref)
                nxt = next((x for j, x in rows if j > n and x.get('ev') == 'skill_started'), None)
                dest = r.get('destination')
                if nxt is None:
                    social['route_pauses_then_other'].setdefault(key, ref)
                elif nxt.get('skill') == 'ngobrol_hadiah':
                    social['route_pauses_then_talk'].setdefault(key, ref)
                elif nxt.get('skill') == 'pergi_ke' and nxt.get('arg') == dest:
                    social['route_pauses_then_same_dest'].setdefault(key, ref)
                else:
                    social['route_pauses_then_other'].setdefault(key, ref)
        # Last-location-held interpolation only between this journal's observations;
        # never stretch a location across unobserved night/day gaps or the tail.
        for (a, location, ref), (b, _, _) in zip(positions, positions[1:]):
            if b <= a:
                continue
            seconds = overlap(a, b, intervals)
            covered_s += seconds
            if location not in ('Farm', 'Cabin') and not location.startswith('FarmHouse') and not location.startswith('Cabin'):
                off_s += seconds
            if str(location).startswith('UndergroundMine'):
                mines['mine_s'] += seconds
            if seconds:
                location_refs.append(ref)
        prev_loc = None
        for _at, location, ref in positions:
            if str(location).startswith('UndergroundMine') and not str(prev_loc or '').startswith('UndergroundMine'):
                mines['visits'].append(ref)
            a, b = _mine_floor(prev_loc), _mine_floor(location)
            if a is not None and b is not None and b > a:
                mines['floors'].append(ref)
            prev_loc = location
    days = len(dates)
    game_minutes = sum(max(v) - min(v) for v in dates.values()) if days else None
    def ratio(v, d, refs, **extra):
        return measure(round(v / d, 4) if v is not None and d else None, refs,
                       missing='recorded numerator and positive denominator', **extra)
    tempo = dict(
        seconds_per_decision=ratio(active_s, decisions, decision_refs, active_seconds=active_s, decisions=decisions),
        game_minutes_per_decision=ratio(game_minutes, decisions, date_refs, game_minutes=game_minutes,
            definition='sum of last minus first observed HH:MM clock per native game date, excluding overnight resets; observed-window proxy'),
        decisions_per_game_day=ratio(decisions, days, decision_refs, game_days=days,
            definition='valid (non-invalid) decision rows / distinct observed native dates, including partial final day'),
        model_wait_share=model_wait(evidence, journals, active_s))
    talk_keys = ('talks_started', 'talks_finished', 'gifts_started', 'gifts_finished')
    result_social = {key + '_per_game_day': ratio(len(events) if instrumented else None, days, events.values(),
        count=len(events) if instrumented else None, game_days=days,
        definition='recorded objective starts or native dialogue-closed/GiftGiven finishes; deduplicated per journal/goal/request; received HOST gifts excluded')
        for key, events in social.items() if key in talk_keys}
    result_social['walks_completed_across_maps_per_game_day'] = ratio(
        len(social['walks_completed_across_maps']) if instrumented else None, days,
        social['walks_completed_across_maps'].values(),
        count=len(social['walks_completed_across_maps']) if instrumented else None, game_days=days,
        definition='verified pergi_ke that changed map, plus live7_route_continue hops; report-only, no gate')
    result_social['approach_replans_per_game_day'] = ratio(
        len(social['approach_replans']) if instrumented else None, days, social['approach_replans'].values(),
        count=len(social['approach_replans']) if instrumented else None, game_days=days,
        definition='approach_replan_decision and talk_follow_replan events; report-only, no gate')
    pause_def = 'live7_route_pause rows (ADR 155 people page); follow-up is the next skill_started; report-only, no gate'
    for name, bucket in (('route_pauses_per_game_day', 'route_pauses'),
                         ('route_pauses_then_talk_per_game_day', 'route_pauses_then_talk'),
                         ('route_pauses_then_same_dest_per_game_day', 'route_pauses_then_same_dest'),
                         ('route_pauses_then_other_per_game_day', 'route_pauses_then_other')):
        result_social[name] = ratio(
            len(social[bucket]) if instrumented else None, days, social[bucket].values(),
            count=len(social[bucket]) if instrumented else None, game_days=days, definition=pause_def)
    result_social['talk_offers'] = talk_offer_counts(journals, evidence.ref)
    friends = None
    if (len(snapshots) >= 2 and all(isinstance(p.get('friends'), dict)
            and all(type(v) is int for v in p['friends'].values())
            for _, p in (snapshots[0], snapshots[-1]))):
        from scripts.free_play.journey import delta
        friends = delta(snapshots[0][1], snapshots[-1][1]).get('friendship')
    for key, field in (('friendship_points_delta', 'points'), ('hearts_gained', 'hearts')):
        result_social[key] = measure(friends[field] if friends is not None else None,
            [snapshots[0][0], snapshots[-1][0]] if friends is not None else [],
            missing='first and last native friendship snapshots',
            definition='native friend maps; points are signed net delta; hearts sum positive per-NPC floor(points/250) gains')
    classes = {}
    for cls, ref in mines['fights'].values():
        classes.setdefault(cls, []).append(ref)
    visits = len(mines['visits']) if instrumented else None
    floors = len(mines['floors']) if instrumented else None
    mine_minutes = mines['mine_s'] / 60 if instrumented else None
    result_mines = dict(
        floors_gained_per_visit=ratio(floors, visits, mines['floors'],
            count=floors, visits=visits,
            definition='native location UndergroundMineN rising within a visit; report-only, no gate'),
        ways_down_offered=measure(len(mines['offered']) if instrumented else None, mines['offered'].values(),
            count=len(mines['offered']) if instrumented else None,
            definition='skill_offer native ladder_down/shaft or aksi turun label; report-only'),
        ways_down_chosen=measure(len(mines['chosen']) if instrumented else None, mines['chosen'].values(),
            count=len(mines['chosen']) if instrumented else None,
            definition='skill_started/skill_model_decision aksi on a listed mine down; report-only'),
        ways_down_completed=measure(len(mines['completed']) if instrumented else None, mines['completed'].values(),
            count=len(mines['completed']) if instrumented else None,
            definition='extras_effect ladder_down/shaft, verified:ladder, or aksi generic readback; report-only'),
        fight_outcomes_by_class=measure({k: len(v) for k, v in classes.items()} if instrumented else None,
            [ref for refs in classes.values() for ref in refs],
            counts={k: len(v) for k, v in classes.items()} if instrumented else None,
            definition='part_live_row lawan_mine by outcome_class; report-only, no gate'),
        minutes_in_mines_per_floor_gained=ratio(mine_minutes, floors, mines['floors'],
            minutes=round(mine_minutes, 3) if mine_minutes is not None else None, floors=floors,
            definition='interpolated UndergroundMine* active seconds / floors gained; report-only'),
    )
    return dict(version='1.7.12', tempo=tempo, social=result_social, mines=result_mines,
        variety=dict(off_farm_cabin_share=ratio(off_s if covered_s else None, active_s, location_refs,
            covered_seconds=round(covered_s, 3), off_farm_cabin_seconds=round(off_s, 3),
            coverage_share=round(covered_s / active_s, 4) if active_s else None,
            lower_bound=bool(active_s and covered_s < active_s),
            definition='last observed location held to next observation within each day journal, intersected with active intervals; Farm and Cabin/FarmHouse interiors excluded; uncovered time earns no off-farm credit')))


def _mine_floor(location):
    text = str(location or '')
    if not text.startswith('UndergroundMine'):
        return None
    suffix = text[len('UndergroundMine'):]
    return int(suffix) if suffix.isdigit() else None


def talk_offer_counts(journals, ref_path=str):
    """ADR 156: only an offered talk with a target the routine accepts earns offer credit.

    Old journals without the target projection cannot prove this and remain unverified.
    """
    from arti_runtime.social_facts import talk_open
    refs, refused, unverified = [], 0, 0
    for path, rows in journals:
        for n, row in rows:
            if row.get('ev') != 'skill_offer' or 'ngobrol_hadiah' not in (row.get('skills') or ()):
                continue
            targets = row.get('talk_targets')
            if not isinstance(targets, dict):
                unverified += 1
            elif talk_open(targets):
                refs.append('%s:J%d' % (ref_path(path), n))
            else:
                refused += 1
    return measure(len(refs), refs, count=len(refs), refused=refused, unverified=unverified,
                   definition='offered talk with a scene target accepted by the map-wide talk routine; missing legacy target evidence is unverified')


def configured_criteria(metrics, config=None):
    config = {} if config is None else config
    if not isinstance(config, dict) or type(config.get('enabled', False)) is not bool:
        raise ValueError('measured criteria config needs boolean enabled')
    enabled = config.get('enabled', False)
    thresholds = config.get('thresholds', {})
    if not isinstance(thresholds, dict) or enabled and not thresholds:
        raise ValueError('enabled measured criteria require explicit thresholds')
    allowed = {f'{group}.{key}' for group, keys in METRICS.items() for key in keys}
    criteria = {}
    for name, bounds in thresholds.items():
        if (name not in allowed or not isinstance(bounds, dict) or not bounds or set(bounds) - {'min', 'max'}
                or any(not number(v) for v in bounds.values())
                or bounds.get('min', -math.inf) > bounds.get('max', math.inf)):
            raise ValueError(f'invalid measured criterion threshold: {name}')
        group, key = name.split('.')
        field = metrics.get(group, {}).get(key) or measure(missing='metric absent')
        value = field['value']
        status = 'UNKNOWN' if value is None else 'MET' if bounds.get('min', -math.inf) <= value <= bounds.get('max', math.inf) else 'NOT_MET'
        # An incompletely observed lower bound cannot fail a minimum or pass a maximum.
        if value is not None and field.get('lower_bound') and (status == 'MET' and 'max' in bounds or status == 'NOT_MET' and value < bounds.get('min', -math.inf)):
            status = 'UNKNOWN'
        criteria[name] = dict(field, status=status if enabled else 'DISABLED', thresholds=bounds)
    return dict(enabled=enabled, available=sorted(allowed), criteria=criteria)


def metrics_markdown(metrics):
    lines = ['', '## Tempo, social and variety (measured; targets disabled by default)', '',
             '| Metric | Value |', '|---|---:|']
    for group, keys in METRICS.items():
        for key in keys:
            field = (metrics or {}).get(group, {}).get(key) or {}
            value = field.get('value')
            suffix = ' (lower bound)' if field.get('lower_bound') else ''
            lines.append(f"| {group}.{key} | {value if value is not None else 'UNKNOWN'}{suffix} |")
    offers = (metrics or {}).get('social', {}).get('talk_offers') or {}
    if offers:
        lines += ['', 'Talk offers with an accepted target: **%s**; refused targets: **%s**; '
                  'legacy offers without target evidence: **%s unverified**.' %
                  (offers.get('count', 0), offers.get('refused', 0), offers.get('unverified', 0))]
    lines += ['', 'Shares are fractions. Definitions, coverage and source references are in EVALUATION.json.']
    return lines
