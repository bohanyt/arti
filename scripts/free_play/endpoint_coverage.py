"""Bounded, nonmutating terminal sampling interval (ADR 42)."""
import json

from scripts.free_play.gameplay_window import timeline_index, timestamp

MAX_INTERVAL_MS = 15000
# An unknown event is deliberately not treated as idle time.
QUIET = {'ui_inspect', 'skill_offer', 'skill_boundary_render_bytes', 'skill_budget_rebuild',
         'live7_no_progress', 'live7_stop_file', 'SAFETY_STOP', 'skill_session_finished',
         'skill_terminal_cleanup'}


def terminal_tail(evidence, journals, native_text, window, sample):
    """Require a bound terminal state and only known nonmutating tail events.

    Untimed journal rows inherit the interval between their neighboring anchors,
    not a fabricated timestamp. If that interval overlaps the tail they are checked.
    Native-log suffixes must be empty: an unclassified native line cannot prove idle.
    """
    observed = sample.get('observed_wall_ms') if sample.get('schema') == 'arti.native_progress/2' else sample['wall_ms']
    if type(observed) is not int:
        return False
    end, last = window['end_at'], observed/1000
    if (window['basis'].startswith('harness window') or not 0 < (end-last)*1000 <= MAX_INTERVAL_MS):
        return False
    times = timeline_index(evidence, journals)
    for path, rows in journals:
        name = path.relative_to(evidence.run).as_posix()
        ats = [times.get((name, n), timestamp(r.get('at'))) for n, r in rows]
        lower = float('-inf')
        upper = [float('inf')] * len(rows)
        next_at = float('inf')
        for i in range(len(rows)-1, -1, -1):
            if ats[i] is not None:
                next_at = ats[i]
            upper[i] = next_at
        for i, (_, r) in enumerate(rows):
            if ats[i] is not None:
                lower = ats[i]
            if upper[i] <= last or lower > end:
                continue
            if r.get('ev') in QUIET or (r.get('ev') == 'FAULT' and r.get('type') == 'render_failed'
                                       and r.get('code') == 'skill_render_budget_exceeded'):
                continue
            return False
    lines = native_text.splitlines()
    anchors = []
    for i, line in enumerate(lines):
        if 'NATIVE_PROGRESS ' in line:
            try:
                if json.loads(line.split('NATIVE_PROGRESS ', 1)[1]) == sample:
                    anchors.append(i)
            except ValueError:
                return False
    # No native log is also valid for journal-only fixtures. A nonempty native
    # log without this anchor is not evidence of a quiet suffix.
    if lines and sample.get('schema') == 'arti.native_progress/2':
        # A delayed heartbeat cannot hide native lines between its read and
        # its emission. Start from the preceding logged coverage anchor and
        # permit only same-monitor, unchanged, unambiguous coverage thereafter.
        decoded = {}
        for i, line in enumerate(lines):
            if 'NATIVE_PROGRESS ' in line:
                try:
                    decoded[i] = json.loads(line.split('NATIVE_PROGRESS ', 1)[1])
                except ValueError:
                    return False
        prior = [i for i, row in decoded.items() if isinstance(row, dict)
                 and row.get('ev') == 'native_progress_coverage'
                 and type(row.get('wall_ms')) is int and row['wall_ms'] <= observed
                 and all(row.get(k) == sample.get(k) for k in ('monitor_id', 'save_id', 'player_id'))
                 and anchors and i <= anchors[-1]]
        if not anchors or not prior:
            return False
        keys = ('monitor_id', 'save_id', 'player_id', 'cc_bundle_deposits',
                'museum_donations', 'letter_tasks_completed', 'ambiguous')
        return all(not line.strip() or (isinstance(row := decoded.get(i), dict)
                   and row.get('ev') == 'native_progress_coverage'
                   and row.get('ambiguous') is False
                   and all(row.get(k) == sample.get(k) for k in keys))
                   for i, line in enumerate(lines) if i > prior[-1])
    return not lines or bool(anchors) and not any(line.strip() for line in lines[anchors[-1]+1:])
