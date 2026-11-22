"""Native progress receipts (ADR 38). No inference from UI success or absent logs."""
import json
from scripts.free_play.gameplay_window import gameplay_window
from scripts.free_play.endpoint_coverage import MAX_INTERVAL_MS, terminal_tail

SCHEMA = 'arti.native_progress/1'
FIELDS = ('cc_bundle_deposits', 'museum_donations', 'letter_tasks_completed')


def fresh_coverage(row):
    if row['schema'] == SCHEMA:
        return True  # v1 rows are synchronous native reads
    observed = row.get('observed_wall_ms')
    return (row.get('sample_kind') == 'heartbeat' and type(observed) is int
            and 0 <= row['wall_ms']-observed <= MAX_INTERVAL_MS)


def observed_at(row):
    value = row.get('observed_wall_ms') if row['schema'] == 'arti.native_progress/2' else row['wall_ms']
    return value if type(value) is int else None


def progress_receipts(evidence, rows, native_text, journals=()):
    records = [(ref, row) for ref, row, _, _ in rows
               if row.get('ev') in ('native_progress', 'native_progress_coverage')]
    malformed = False
    for n, line in enumerate(native_text.splitlines(), 1):
        if 'NATIVE_PROGRESS ' not in line:
            continue
        try:
            row = json.loads(line.split('NATIVE_PROGRESS ', 1)[1])
            if not isinstance(row, dict):
                raise ValueError('invalid native row')
            records.append((f'farmhand-SMAPI.txt:L{n}', row))
        except ValueError:
            malformed = True
    window = gameplay_window(evidence, journals)
    start, end = window.get('start_at'), window.get('end_at')
    bounded = all(type(t) in (int, float) for t in (start, end)) and end > start
    result = {k: {} for k in FIELDS}
    requests = set()
    coverage = {}
    for ref, r in records:
        if r.get('schema') not in (SCHEMA, 'arti.native_progress/2') or not isinstance(r.get('save_id'), str) or not r['save_id'] or type(r.get('player_id')) is not int:
            continue
        at = r.get('wall_ms')
        if type(at) is not int:
            continue
        if r.get('ev') == 'native_progress_coverage':
            key = (r.get('monitor_id'), r['save_id'], r['player_id'], r.get('seq'))
            if not isinstance(key[0], str) or not key[0] or type(key[3]) is not int or key[3] < 1:
                continue
            if key in coverage and coverage[key][1] != r:
                malformed = True
            coverage[key] = (ref, r)
            continue
        if bounded and not start * 1000 <= at <= end * 1000:
            continue  # copied history predating this evaluation earns no credit
        if r.get('actor_verified') is not True or not isinstance(r.get('event_id'), str):
            continue
        kind, qty = r.get('kind'), r.get('quantity')
        if kind in ('cc_bundle_deposit', 'museum_donation'):
            if (not isinstance(r.get('request_id'), str) or not r['request_id']
                    or not isinstance(r.get('item'), str) or not r['item'].startswith('(')
                    or type(qty) is not int or qty <= 0 or type(r.get('inventory_delta')) is not int
                    or r['inventory_delta'] != -qty):
                continue
            if kind == 'cc_bundle_deposit':
                if (r.get('flag_before') is not False or r.get('flag_after') is not True
                        or not isinstance(r.get('bundle_id'), str) or not r['bundle_id']
                        or type(r.get('slot')) is not int or r['slot'] < 0):
                    continue
                field, identity = FIELDS[0], f"{r['bundle_id']}:{r['slot']}"
            else:
                if qty != 1 or r.get('museum_before') is not False or r.get('museum_after') is not True:
                    continue
                field, identity = FIELDS[1], r['item']
            event_id = f"{r['save_id']}:{kind}:{identity}"
        elif kind == 'letter_task_completed':
            if (r.get('completed_before') is not False or r.get('completed') is not True
                    or r.get('task_kind') not in ('quest', 'special_order')
                    or r.get('link_source') != {'quest': 'mail_quest_attachment', 'special_order': 'special_order_mail_reward'}[r['task_kind']]
                    or any(not isinstance(r.get(k), str) or not r[k] for k in ('task_id', 'letter_id'))):
                continue
            field = FIELDS[2]
            identity = f"{r['task_kind']}:{r['task_id']}:{r['letter_id']}"
            event_id = f"{r['save_id']}:{r['player_id']}:{kind}:{identity}"
        else:
            continue
        if r['event_id'] == event_id:
            if kind in ('cc_bundle_deposit', 'museum_donation'):
                requests.add(r['request_id'])
            # Multiple mail rewards/links for one task are still one completion.
            key = (r['save_id'], r['player_id'], r['task_kind'], r['task_id']) if field == FIELDS[2] else event_id
            result[field].setdefault(key, (ref, r))
    # Zero needs continuous instrumentation covering gameplay, not
    # merely an event-free file. Restarts, missing ticks, ambiguity or absent
    # instrumentation preserve UNKNOWN. Counts are never backfilled in old runs.
    samples = sorted(coverage.values(), key=lambda pair: pair[1]['wall_ms'])
    complete = bounded and not malformed and len(samples) >= 2
    if complete:
        before = [s for s in samples if s[1]['wall_ms'] <= start * 1000
                  and observed_at(s[1]) is not None and observed_at(s[1]) <= start * 1000]
        after = [s for s in samples if observed_at(s[1]) is not None and observed_at(s[1]) >= end * 1000]
        # The nearest bracketing samples plus EVERY intervening sample. Never
        # shorten gameplay to the coverage log, or discard an internal gap.
        lo = before[-1][1]['wall_ms'] if before else start * 1000
        hi = after[0][1]['wall_ms'] if after else end * 1000
        samples = [(ref, r) for ref, r in samples if lo <= r['wall_ms'] <= hi]
        complete = len(samples) >= 2
    if complete:
        a, b = samples[0][1], samples[-1][1]
        first_read, last_read = observed_at(a), observed_at(b)
        tail = (last_read is not None and last_read < end * 1000
                and terminal_tail(evidence, journals, native_text, window, b))
        # ADR 126 (a54: 11 gaps over 15 s in the disk stall, 0 ambiguous, one monitor, read again at 23:14:16Z after the
        # 23:13:26Z stop). NativeProgressObserver compares each read with the one before it across any wait: a flipped flag
        # in a gap raises a counter or `ambiguous` at the next read, and a failed read raises `ambiguous`. So a gap is closed
        # by a later read of the same monitor; what stays open is time after the last read (ADR 55: a heartbeat is not a read).
        complete = (first_read is not None and last_read is not None
                    and start * 1000 - MAX_INTERVAL_MS <= first_read <= start * 1000
                    # A v1 row has no read stamp of its own, so only its interval can stand for the read.
                    and (end * 1000 <= last_read <= b['wall_ms']
                         and (b['schema'] != SCHEMA or b['wall_ms'] <= end * 1000 + MAX_INTERVAL_MS) or tail)
                    and len({(r['monitor_id'], r['save_id'], r['player_id']) for _, r in samples}) == 1
                    and all(r.get('ambiguous') is False for _, r in samples)
                    and all(type(r.get('observed_wall_ms')) is int and r.get('sample_kind') == 'heartbeat'
                            and r['observed_wall_ms'] <= r['wall_ms'] for _, r in samples if r['schema'] != SCHEMA)
                    and all(0 <= observed_at(y)-observed_at(x) for (_, x), (_, y) in zip(samples, samples[1:]))
                    and all(0 < y['wall_ms'] - x['wall_ms'] and y['seq'] == x['seq'] + 1
                            and (y['schema'] != SCHEMA or (y['wall_ms'] - x['wall_ms'] <= MAX_INTERVAL_MS
                                                           and observed_at(y) - observed_at(x) <= MAX_INTERVAL_MS))
                            for (_, x), (_, y) in zip(samples, samples[1:])))
        if complete and tail:
            window.update(terminal_sample_tail_s=round(end-last_read/1000, 6),
                          terminal_sample_basis='one allowed interval; bound stop and nonmutating journal/native tail (ADR 42)')
    zero = {field: (complete and all(type(r.get(field)) is int and r[field] >= 0 for _, r in samples)
                    and len({r[field] for _, r in samples}) == 1) for field in FIELDS}
    ages = [r['wall_ms']-r['observed_wall_ms'] for _, r in samples
            if r['schema'] == 'arti.native_progress/2' and type(r.get('observed_wall_ms')) is int]
    window['native_coverage_diagnostics'] = dict(
        sample_count=len(samples), complete=bool(complete), interval_bound_ms=MAX_INTERVAL_MS,
        max_emission_gap_ms=max((y['wall_ms']-x['wall_ms'] for (_, x), (_, y) in zip(samples, samples[1:])), default=None),
        max_native_read_age_ms=max(ages, default=None),
        max_native_observation_gap_ms=max((observed_at(y)-observed_at(x)
            for (_, x), (_, y) in zip(samples, samples[1:])
            if observed_at(x) is not None and observed_at(y) is not None), default=None),
        stale_or_invalid_heartbeat_samples=sum(not fresh_coverage(r) for _, r in samples),
        gaps_over_bound_closed_by_later_read=sum(y['wall_ms']-x['wall_ms'] > MAX_INTERVAL_MS or (
            observed_at(x) is not None and observed_at(y) is not None and observed_at(y)-observed_at(x) > MAX_INTERVAL_MS)
            for (_, x), (_, y) in zip(samples, samples[1:])) if complete else 0)
    return result, zero, [ref for ref, _ in samples] if complete else [], requests, window
