"""Evaluator 1.3.6 gameplay coverage boundary (ADR 40); no native-sample trimming."""
from datetime import datetime
import math

VERSION = 'arti.free_play.gameplay_window/1'
CLEANUP = {'skill_terminal_cleanup'}
STOPS = {'live7_no_progress', 'live7_stop_file', 'SAFETY_STOP'}


def stop_row(r):
    """A stop row. ADR 241 B: a `live7_no_progress` row is a stop only from runtimes that still ended on it
    (outcome SAFETY_STOP); the ladder's recoverable 600 s row is not one."""
    return r.get('ev') in STOPS and (r.get('ev') != 'live7_no_progress' or r.get('outcome_class') == 'SAFETY_STOP')


def timestamp(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None


def timeline_index(evidence, journals):
    """Only timestamps bound to an existing journal line and its event."""
    native = {(p.relative_to(evidence.run).as_posix(), n): r for p, rs in journals for n, r in rs}
    times = {}
    for _, row in evidence.rows(evidence.run / 'timeline.jsonl'):
        if type(row.get('line')) is not int:
            continue
        key = (str(row.get('file', '')).replace('\\', '/'), row.get('line'))
        if key in native and row.get('ev') == native[key].get('ev'):
            at = timestamp(row.get('at'))
            if at is not None:
                times.setdefault(key, []).append(at)
    return {key: vals[0] for key, vals in times.items() if len(set(vals)) == 1}


def _stop_clock(evidence, end, entries):
    """ADR 126 (a54 d23 J661-J679 share one timeline stamp, 23:18:22Z: the recorder's poll after the disk stall). A timeline stamp
    is when a row was read, so an upper bound. The cycle's own clock on its stop, later than the journal's last own clock, is when
    it happened (23:13:26Z). An earlier cycle stop, from before the run's last rows, moves nothing."""
    own = [t for t in (timestamp(r.get('at')) for _, r, _ in entries) if t is not None]
    if end is None or not own:
        return end
    stops = [t for path in evidence.run.glob('runtime/live7/*/cycle_events.jsonl') for _, r in evidence.rows(path) or ()
             if stop_row(r) and (t := timestamp(r.get('at'))) is not None and max(own) <= t < end]
    return min(stops, default=end)


def gameplay_window(evidence, journals):
    bounds = evidence.json('window.json', {}) or {}
    start, shutdown = timestamp(bounds.get('start_at')), timestamp(bounds.get('end_at'))
    result = dict(version=VERSION, start_at=start, end_at=shutdown, harness_end_at=shutdown,
                  non_gameplay_tail_s=0, evidence=['window.json'],
                  basis='harness window fallback; no timestamped terminal gameplay boundary')
    times = timeline_index(evidence, journals)
    candidates = []
    for p, rs in journals:
        path = p.relative_to(evidence.run).as_posix()
        entries = [(n, r, times.get((path, n), timestamp(r.get('at')))) for n, r in rs]
        latest = max((at for _, _, at in entries if at is not None), default=float('-inf'))
        candidates.append((latest, path, entries))
    if start is None or shutdown is None or shutdown <= start or not candidates:
        return result
    _, path, entries = max(candidates, key=lambda x: (x[0], x[1]))
    # A terminal stop ends gameplay only if subsequent rows are finish/cleanup.
    # Later gameplay means it was not the final run stop: retain the full bound.
    stops = [i for i, (_, r, _) in enumerate(entries)
             if stop_row(r) or r.get('outcome_class') == 'SAFETY_STOP']
    if stops:
        i = stops[-1]
        if any(r.get('ev') not in CLEANUP | {'skill_session_finished'} for _, r, _ in entries[i+1:]):
            return result
        n, _, end = entries[i]
    else:
        gameplay = [(n, r, at) for n, r, at in entries if r.get('ev') not in CLEANUP]
        if not gameplay:
            return result
        n, _, end = gameplay[-1]
    end = _stop_clock(evidence, end, entries)
    if end is not None and start <= end <= shutdown:
        result.update(end_at=end, non_gameplay_tail_s=round(shutdown-end, 6),
                      evidence=['window.json', f'{path}:J{n}'] + (['timeline.jsonl'] if (path, n) in times else []),
                      basis='ARMED to terminal gameplay stop/state; subsequent harness shutdown is non-gameplay')
    return result
