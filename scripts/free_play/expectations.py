"""ADR 150: read-only, incremental journal assertions; never steer gameplay."""
from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from arti_runtime import ipc
from scripts.free_play.memory_checkpoint import stamp
from scripts.free_play.native_safety import NativeSafety, SavedCounters

MISSING = object()
OPS = {'eq', 'ne', 'prefix', 'not_prefix', 'contains', 'has', 'exists', 'one_of', 'gt', 'gte', 'lt', 'lte'}


def field(row, path):
    for key in path.split('.'):
        if isinstance(row, list) and key.isascii() and key.isdecimal() and len(key) < 9:
            index = int(key)
            row = row[index] if index < len(row) else MISSING
            continue
        if not isinstance(row, dict) or key not in row:
            return MISSING
        row = row[key]
    return row


def validate_match(match):
    if not isinstance(match, dict) or not match:
        raise ValueError('expect match must be a nonempty dotted-field object')
    for path, wanted in match.items():
        if not isinstance(path, str) or not path or any(not p for p in path.split('.')):
            raise ValueError('invalid expect field')
        if isinstance(wanted, dict) and (not wanted or set(wanted) - OPS):
            raise ValueError('unknown expect operator')
        if isinstance(wanted, dict):
            for op, value in wanted.items():
                if (op in ('prefix', 'not_prefix') and not isinstance(value, str)
                        or op == 'one_of' and not isinstance(value, list)
                        or op == 'exists' and type(value) is not bool
                        or op in ('gt', 'gte', 'lt', 'lte') and
                        (type(value) not in (int, float) or not math.isfinite(value))):
                    raise ValueError('invalid expect operator value')
                if op == 'has':
                    validate_match(value)


def matches(row, match):
    for path, wanted in match.items():
        actual = field(row, path)
        if isinstance(wanted, dict) and 'exists' in wanted:
            if (actual is not MISSING) != wanted['exists']:
                return False
            if actual is MISSING and len(wanted) == 1:
                continue
        if actual is MISSING:
            return False  # missing is never evidence, even for ne/not_prefix
        if not isinstance(wanted, dict):
            if actual != wanted:
                return False
            continue
        for op, value in wanted.items():
            if op == 'exists':
                continue
            if op in ('eq', 'ne'):
                ok = (actual == value) if op == 'eq' else (actual != value)
            elif op in ('prefix', 'not_prefix'):
                ok = isinstance(actual, str) and bool(actual) and isinstance(value, str)
                if ok:
                    ok = actual.startswith(value) if op == 'prefix' else not actual.startswith(value)
            elif op == 'contains':
                ok = (isinstance(actual, str) and isinstance(value, str) or isinstance(actual, list)) and value in actual
            elif op == 'has':
                ok = isinstance(actual, list) and any(matches(entry, value) for entry in actual)
            elif op == 'one_of':
                ok = isinstance(value, list) and actual in value
            else:
                ok = type(actual) in (int, float) and type(value) in (int, float) and math.isfinite(actual) and math.isfinite(value)
                if ok:
                    ok = {'gt': actual > value, 'gte': actual >= value, 'lt': actual < value, 'lte': actual <= value}[op]
            if not ok:
                return False
    return True


def wall_seconds(value):
    """A finite wall-clock second: a number, or an ISO-8601 UTC string (the bench's `utc` rows); else None."""
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str) and value:
        from datetime import datetime
        try:
            when = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return when.timestamp() if when.tzinfo is not None else None
    return None


def bracketed(press, focus):
    """W1b B5 (ADR 250): refs of the focus rows that bracket one press, or None. A press counts only when the last focus
    readback at or before its start is front, the first one at or after its end is front, and every readback in between
    is front. A press with no wall window, or with no readback on either side, is not bracketed."""
    t0, t1, ref = press
    if t0 is None or t1 is None or t1 < t0:
        return None
    before = [f for f in focus if f[0] <= t0]
    after = [f for f in focus if f[0] >= t1]
    if not before or not after:
        return None
    first = max(before, key=lambda f: f[0])
    last = min(after, key=lambda f: f[0])
    inside = [f for f in focus if t0 < f[0] < t1]
    if not (first[1] and last[1] and all(f[1] for f in inside)):
        return None
    return [first[2], ref, last[2]]


def game_minutes(row):
    game = row.get('game')
    if not isinstance(game, dict):
        return None
    when = stamp(game.get('date'), game.get('time'))
    if not when or when[1] is None or not 0 <= when[1] % 100 < 60:
        return None
    return when[0] * 1440 + when[1] // 100 * 60 + when[1] % 100


class Engine:
    def __init__(self, specs, *, allow_absence=False):
        if not isinstance(specs, list) or not specs:
            raise ValueError('expect requires a nonempty list')
        self.states = []
        self.native_safety = NativeSafety()
        ids = set()
        for spec in specs:
            if not isinstance(spec, dict) or not isinstance(spec.get('id'), str) or not spec['id'] or spec['id'] in ids:
                raise ValueError('expect requires unique nonempty ids')
            ids.add(spec['id'])
            kind = spec.get('kind')
            if kind not in ('event', 'once', 'absent', 'sequence', 'no_death', 'native_counter_not_risen', 'bracketed'):
                raise ValueError('unknown expect kind')
            if set(spec) - {'id', 'kind', 'match', 'steps', 'within_game_minutes', 'description', 'counter', 'next_event',
                            'focus', 'bracket'}:
                raise ValueError('unknown expect field')
            if ('focus' in spec or 'bracket' in spec) and kind != 'bracketed':
                raise ValueError('focus/bracket belong to a bracketed expect')
            if kind == 'bracketed':
                # W1b B5: `match` rows carry started_unix/ended_unix; `focus` names every readback row (with `utc`),
                # `bracket` the readbacks that count as front.
                for key in ('match', 'focus', 'bracket'):
                    validate_match(spec.get(key))
            elif kind == 'sequence':
                steps = spec.get('steps')
                if not isinstance(steps, list) or len(steps) < 2:
                    raise ValueError('expect sequence requires at least two steps')
                for step in steps:
                    validate_match(step)
                if 'next_event' in spec and (len(steps) != 2 or not isinstance(spec['next_event'], str)
                                             or steps[-1].get('ev') != spec['next_event']):
                    raise ValueError('expect next_event requires a two-step sequence ending with that event')
                bound = spec.get('within_game_minutes')
                if bound is not None and (type(bound) is not int or bound <= 0):
                    raise ValueError('expect game-minute bound must be a positive integer')
            elif kind == 'native_counter_not_risen':
                if spec.get('counter') != 'timesUnconscious':
                    raise ValueError('unsupported native counter')
            elif kind != 'no_death':
                validate_match(spec.get('match'))
            self.states.append(dict(spec=spec, status='PASS' if kind in ('absent', 'no_death') else 'PENDING', refs=[], pending=[],
                                    focus=[], presses=[]))
        if not allow_absence and all(s['spec']['kind'] in ('absent', 'no_death', 'native_counter_not_risen') for s in self.states):
            raise ValueError('expect requires positive evidence, not absence alone')

    def consume(self, row, ref):
        native = self.native_safety.consume(row, ref)
        knockout = row.get('ev') == 'native_knockout' or any(r['ev'] == 'native_knockout' for r in native)
        for state in self.states:
            spec = state['spec']; kind = spec['kind']
            if state['status'] == 'FAIL':
                continue
            # Existing library manifests keep their byte identity while gaining
            # the missing native-outcome/counter guard (ADR 170).
            if knockout and (kind == 'no_death' or spec['id'] == 'no_death'):
                state.update(status='FAIL', refs=next((r['refs'] for r in native if r['ev'] == 'native_knockout'), [ref]),
                             reason='native knockout outcome or timesUnconscious rise')
                continue
            if kind == 'no_death':
                continue
            if kind == 'native_counter_not_risen':
                if row.get('ev') == 'native_counter' and row.get('counter') == spec['counter'] and type(row.get('value')) is int:
                    if 'baseline' not in state:
                        state.update(baseline=row['value'], refs=[ref])
                    elif ref not in state['refs']:
                        state.update(status='FAIL' if row['value'] != state['baseline'] else 'PASS',
                                     refs=[state['refs'][0], ref], reason='native counter compared with baseline')
                continue
            if kind == 'bracketed':
                # Journals are read file by file, so both lists are kept and every press is judged against every
                # readback seen so far (a later file can still bracket an earlier press).
                if state['status'] == 'PASS':
                    continue
                if matches(row, spec['focus']):
                    t = wall_seconds(row.get('utc'))
                    if t is not None:
                        state['focus'].append((t, matches(row, spec['bracket']), ref))
                if matches(row, spec['match']):
                    state['presses'].append((wall_seconds(row.get('started_unix')), wall_seconds(row.get('ended_unix')), ref))
                for press in state['presses']:
                    refs = bracketed(press, state['focus'])
                    if refs is not None:
                        state.update(status='PASS', refs=refs)
                        break
                else:
                    if state['presses']:
                        state['reason'] = 'no matching press was bracketed by front readbacks (front before and after)'
                continue
            if kind != 'sequence':
                if matches(row, spec['match']):
                    if kind == 'once' and state['status'] == 'PASS':
                        state.update(status='FAIL', refs=state['refs'] + [ref], reason='duplicate evidence')
                    else:
                        state.update(status='FAIL' if kind == 'absent' else 'PASS', refs=[ref])
                continue
            clock = game_minutes(row)
            pending = []
            for item in state['pending']:
                index, start, refs = item
                delta = clock - start if clock is not None and start is not None else None
                bound = spec.get('within_game_minutes')
                if bound is not None and delta is not None and (delta < 0 or delta > bound):
                    state.update(status='FAIL', refs=refs + [ref], reason='native game-minute deadline/rewind')
                    break
                if matches(row, spec['steps'][index]) and (bound is None or delta is not None):
                    refs = refs + [ref]
                    index += 1
                    if index == len(spec['steps']):
                        state.update(status='PASS', refs=refs)
                        continue
                elif row.get('ev') == spec.get('next_event') and 'next_event' in spec:
                    state.update(status='FAIL', refs=refs + [ref], reason='next observed event did not match')
                    break
                pending.append((index, start, refs))
            if state['status'] == 'FAIL':
                continue
            if matches(row, spec['steps'][0]):
                # Every observed trigger owes a completion; repeated choices cannot
                # reset an older deadline or erase a previously failed attempt.
                pending.append((1, clock, [ref]))
            state['pending'] = pending
            if pending:
                state['status'] = 'PENDING'

    def result(self, *, final=False, stop_reason=None):
        conditions = []
        for state in self.states:
            status = state['status']
            if final and status == 'PENDING':
                status = 'FAIL'
            conditions.append(dict(id=state['spec']['id'], status=status, refs=state['refs'],
                reason=state.get('reason', 'required evidence not observed' if status != 'PASS' else 'observed; absence covers checked prefix')))
        status = 'FAIL' if stop_reason or any(c['status'] == 'FAIL' for c in conditions) else (
            'PASS' if all(c['status'] == 'PASS' for c in conditions) else 'PENDING')
        return dict(schema='arti.free_play.expect_result/1', status=status, conditions=conditions,
                    final=final, stop_reason=stop_reason, scope='observed journal prefix only; no future-behavior claim')


class JournalCheck:
    def __init__(self, run, specs):
        self.run = Path(run)
        self.engine = Engine(specs)
        self.offsets, self.lines, self.context = {}, {}, {}
        self.saved_counters = SavedCounters(run)

    def poll(self, *, final=False, stop_reason=None):
        if stop_reason:
            self.error = stop_reason  # a later poll can never erase a safety/budget failure
        try:
            counters = self.saved_counters.poll()
        except (OSError, ValueError, KeyError, ET.ParseError) as exc:
            self.error = 'native counter evidence unavailable: ' + str(exc)
            counters = []
        for row, ref in counters:
            if '/checkpoints/' not in ref:
                self.engine.consume(row, ref)
        paths = sorted(self.run.glob('runtime/live7/*/day-*/journal/g1_journal.jsonl'))
        paths += sorted(self.run.glob('runtime/live7/*/cycle_events.jsonl'))
        if (self.run / 'native-outcomes.jsonl').exists():
            paths.append(self.run / 'native-outcomes.jsonl')
        for path in paths:
            with ipc.open_shared(path, 'rb') as stream:
                stream.seek(0, 2)
                if stream.tell() < self.offsets.get(path, 0):
                    self.error = 'journal truncated: ' + str(path.relative_to(self.run))
                stream.seek(self.offsets.get(path, 0)); raw = stream.read()
            complete = raw.rfind(b'\n') + 1
            if final and raw[complete:].strip():
                self.error = 'incomplete final journal append: ' + str(path.relative_to(self.run))
            self.offsets[path] = self.offsets.get(path, 0) + complete
            for line in raw[:complete].splitlines():
                self.lines[path] = self.lines.get(path, 0) + 1
                ref = f'{path.relative_to(self.run).as_posix()}:{self.lines[path]}'
                try:
                    row = json.loads(line.decode('utf-8-sig'))
                    if not isinstance(row, dict):
                        raise ValueError('non-object journal row')
                except (ValueError, UnicodeError):
                    stop_reason = 'malformed journal row: ' + ref
                    self.engine.consume(dict(ev='harness_malformed_journal'), ref)
                    self.error = stop_reason
                    continue
                # ADR 241 B: the ladder's recoverable live7_no_progress row is no safety stop (its outcome says so).
                if row.get('outcome_class') == 'SAFETY_STOP' or row.get('ev') in ('SAFETY_STOP', 'HOST_SAFETY_STOP'):
                    self.error = 'journal safety stop: ' + ref
                if isinstance(row.get('game'), dict):
                    self.context[path] = dict(game=row['game'], ref=ref)
                row['_context'] = self.context.get(path, {})
                if row.get('ev') == 'skill_offer':
                    skills, offered = row.get('skills'), row.get('offered')
                    if isinstance(skills, list) and isinstance(offered, list) and len(skills) == len(offered):
                        row['_offers'] = [dict(skill=s, arg=a) for s, a in zip(skills, offered)]
                self.engine.consume(row, ref)
        for row, ref in counters:
            if '/checkpoints/' in ref:
                self.engine.consume(row, ref)
        return self.engine.result(final=final, stop_reason=stop_reason or getattr(self, 'error', None))
