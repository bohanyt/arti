"""Field-specific fallback for null terminal native snapshots (ADR 43)."""
import json
import re

from scripts.free_play.gameplay_window import timestamp, timeline_index

# These effects cannot award skill XP. Unknown effects fail closed, including
# harvest, forage, fishing, rock/tree breaking and combat.
NO_XP = {'cut_weed', 'till_tile', 'water_tile', 'refill_can', 'place_item',
         'shipping_bin', 'warp', 'door', 'tv'}
EFFECTS = {'tool_work_effect', 'extras_effect', 'interaction_effect'}


def changed(row, field):
    """Inspect native receipts, including nested UI and skill mirrors."""
    if isinstance(row, list):
        return any(changed(r, field) for r in row)
    if not isinstance(row, dict):
        return False
    for key, value in row.items():
        if field == 'skills' and (re.search(r'(^|_)xp($|_)|experience', key.casefold()) or key == 'skill_level_gain'):
            if value not in (0, None, False, {}, []):
                return True
        if field == 'money' and key in ('money_delta', 'currency_delta', 'individual_money_earned_delta'):
            if type(value) is not int or value != 0:
                return True
        if isinstance(value, (dict, list)) and changed(value, field):
            return True
    return False


def tail_safe(row, field):
    if changed(row, field):
        return False
    ev = row.get('ev', '')
    if not isinstance(ev, str):
        return False
    if ev in EFFECTS:
        # Earning/reward effects must not be inferred from menu success.
        return row.get('kind') in NO_XP
    if '_effect' in ev:
        return False
    if ev in ('native_progress', 'native_progress_coverage'):
        return ev == 'native_progress_coverage' and row.get('ambiguous') is False
    if ev == 'native_transaction':
        return (row.get('source') == 'generic_ui_native_readback' and row.get('state') == 'completed'
                and row.get('kind') == 'ship' and row.get('money_delta') == 0)
    if ev == 'progress_snapshot':
        return True  # Null observations carry no mutation evidence.
    if ev == 'ui_press' and row.get('phase') == 'result':
        # Text/answer presses have no economy readback. Their progress effects
        # are fenced by the native effect/transaction receipts above.
        if row.get('readback') is None:
            return True
        try:
            rb = json.loads(row['readback'])
            if changed(rb, field):
                return False
            a, b = rb['before'], rb['after']
            if field == 'money':
                return type(a.get('money')) is int and a['money'] == b.get('money')
            # Bag crafting/storage/ship/close cannot award XP. Other menus may.
            return row.get('result_menu_type') in ('GameMenu', 'ItemGrabMenu', None, '')
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
    # Plans do not award anything, but an unaccounted native skill/money event
    # must never become an idle row just because it has a new event name.
    return not any(word in ev.casefold() for word in ('xp', 'experience', 'money', 'skill_level', 'native_')) or ev in {
        'native_done_readback_lag', 'native_capability_offer', 'native_capability_choice'}


def fallback_endpoint(evidence, journals, window, keys, valid, native_text=''):
    """Return a positive observed lower bound, or a field with a checked tail.

    The original first snapshot remains the baseline. No saved-state fallback,
    harness-only stop, untimed endpoint or replacement of a valid endpoint.
    Untimed receipts are checked whenever their neighboring interval overlaps.
    """
    if window['basis'].startswith('harness window'):
        return None
    start, end = window['start_at'], window['end_at']
    times = timeline_index(evidence, journals)
    entries, snaps = [], []
    for path, rows in journals:
        name = path.relative_to(evidence.run).as_posix()
        ats = [times.get((name, n), timestamp(r.get('at'))) for n, r in rows]
        upper, next_at = [float('inf')] * len(rows), float('inf')
        for i in range(len(rows)-1, -1, -1):
            if ats[i] is not None:
                next_at = ats[i]
            upper[i] = next_at
        lower = float('-inf')
        for i, (n, row) in enumerate(rows):
            if ats[i] is not None:
                lower = ats[i]
            ref = f'{name}:J{n}'
            entries.append((ref, row, lower, upper[i]))
            if row.get('ev') == 'progress_snapshot':
                value = row.get('progress')
                for key in keys:
                    value = value.get(key) if isinstance(value, dict) else None
                snaps.append((ref, value, ats[i]))
    if len(snaps) < 3 or not valid(snaps[0][1]) or valid(snaps[-1][1]):
        return None
    if snaps[0][2] is None or not start <= snaps[0][2] <= end:
        return None
    candidates = [s for s in snaps[1:] if valid(s[1]) and s[2] is not None and start <= s[2] <= end]
    if not candidates:
        return None
    ref, value, at = max(candidates, key=lambda s: s[2])
    field = 'skills' if keys == ('skills',) else 'money'
    baseline = snaps[0][1]
    # A later failed observation does not erase a positive observed gain in
    # these cumulative counters. This is a dated lower bound, not a claim that
    # the tail was idle. Zero still needs the existing complete tail proof.
    positive = (all(value[k] >= baseline[k] for k in baseline) and any(value[k] > baseline[k] for k in baseline)
                if field == 'skills' else value > baseline)
    if positive:
        return dict(value=value, ref=ref, at=at, lower_bound=True,
                    terminal_evidence=window['evidence'], tail_end_at=end)
    refs = []
    for tail_ref, row, lower, upper in entries:
        if upper <= at or lower > end or tail_ref == ref:
            continue
        if not tail_safe(row, field):
            return None
        if row.get('ev') in EFFECTS | {'native_transaction', 'ui_press'}:
            refs.append(tail_ref)
    for n, line in enumerate(native_text.splitlines(), 1):
        if 'NATIVE_PROGRESS ' not in line:
            continue
        try:
            row = json.loads(line.split('NATIVE_PROGRESS ', 1)[1])
            if type(row.get('wall_ms')) is not int or row['wall_ms'] < 0:
                return None
            wall = row['wall_ms']/1000
            if at < wall <= end and not tail_safe(row, field):
                return None
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
    return dict(value=value, ref=ref, at=at, tail_evidence=list(dict.fromkeys(refs)),
                terminal_evidence=window['evidence'], tail_end_at=end)
