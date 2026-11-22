"""ADR 170: native evidence shared by the harness, assertions and evaluator.

No game commands, prompts, memory files, or live save reads. Saved counters come
only from the staged baseline and completed, run-local night checkpoints.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
from scripts.free_play.gameplay_window import timestamp

from arti_runtime import ipc
from scripts.free_play import storage


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def counter_from_save(path, staging):
    root = ET.fromstring(storage.read_bytes(path))
    if str(root.findtext('uniqueIDForThisGame')) != str(staging['save_id']):
        raise ValueError('native safety save identity mismatch')
    peers = root.findall('./farmhands/Farmer') + root.findall('.//farmhand')
    peers = [p for p in peers if p.findtext('UniqueMultiplayerID') == str(staging['farmhand_id'])]
    if len(peers) != 1:
        raise ValueError('native safety requires the established farmhand')
    # Stardew 1.6 serializes Stats.Values as key/value items.
    values = [item.findtext('value/unsignedInt') for item in peers[0].findall('./stats/Values/item')
              if item.findtext('key/string') == 'timesUnconscious']
    if not values:
        direct = peers[0].findtext('./stats/timesUnconscious')
        values = [direct] if direct is not None else []
    if len(values) != 1 or not str(values[0]).isdigit():
        return None  # absence is unknown, never an invented zero
    return dict(ev='native_counter', counter='timesUnconscious', value=int(values[0]),
                game=dict(date=[int(root.findtext('year')), root.findtext('currentSeason'),
                                int(root.findtext('dayOfMonth'))], time=600))


class SavedCounters:
    def __init__(self, run):
        self.run = Path(run)
        self.seen = set()

    def poll(self):
        staging_path = self.run / 'STAGING.json'
        if not staging_path.exists():
            return []
        staging = json.loads(ipc.read_text(staging_path, encoding='utf-8-sig'))
        name = staging.get('save_folder')
        if not isinstance(name, str) or not name or any(c in name for c in '/\\:') or name in ('.', '..'):
            raise ValueError('invalid native safety save folder')
        result = []
        if 'baseline' not in self.seen and type(staging.get('timesUnconscious')) is int:
            result.append((dict(ev='native_counter', counter='timesUnconscious',
                                value=staging['timesUnconscious']), 'STAGING.json:timesUnconscious'))
            self.seen.add('baseline')
        paths = [] if 'baseline' in self.seen else [self.run / folder / name / name for folder in ('continuation-start', 'template')]
        # Do not follow paths from cycle_state (historical runs retain machine paths).
        for checkpoint in sorted(self.run.glob('runtime/live7/*/checkpoints/day-*')):
            manifest = checkpoint / 'manifest.json'
            if not manifest.exists():
                continue
            receipt = json.loads(ipc.read_text(manifest, encoding='utf-8-sig'))
            if receipt.get('status') == 'copied' and receipt.get('copy_state') == 'after_night_save':
                path = checkpoint / 'save' / name
                if path in self.seen:
                    continue
                if not storage.exists(path) or storage.digest_file(path) != receipt.get('files', {}).get(name):
                    raise ValueError('native safety checkpoint bytes differ from receipt')
                paths.append(path)
        for path in paths:
            if path in self.seen or not storage.exists(path):
                continue
            row = counter_from_save(path, staging)
            self.seen.add(path)
            if row:
                manifest = path.parent.parent / 'manifest.json'
                if manifest.exists():
                    row['at'] = timestamp(json.loads(ipc.read_text(manifest, encoding='utf-8-sig')).get('utc'))
                result.append((row, path.relative_to(self.run).as_posix() + ':timesUnconscious'))
        return result


def legacy_vitals(run, path, rows):
    """Read-only adapter for pre-170 reports whose HP never reached the journal.

    Bind to the first journal native game stamp matching that inspection. That
    row's wall receipt is an upper bound, not an invented HP-zero frame.
    """
    if any(r.get('ev') == 'native_vitals' for _, r in rows):
        return {}
    report = path.parent.parent / 'report.json'
    if not storage.exists(report):
        return {}
    result = {}
    doc = json.loads(storage.read_bytes(report).decode('utf-8-sig'))
    for index, step in enumerate(doc.get('steps', [])):
        inspect = step.get('inspect_evidence') or {}
        if inspect.get('status') != 'current':
            continue
        motor = (inspect.get('facts') or {}).get('motor') or {}
        hp = motor.get('health')
        if not number(hp):
            continue
        game = dict(date=[motor.get('year'), motor.get('season'), motor.get('day')],
                    time=motor.get('time_of_day'), location=motor.get('location'))
        match = next((n for n, r in rows if r.get('game') == game), None)
        if match is not None:
            observations = (inspect.get('facts') or {}).get('observations') or {}
            combat = ((observations.get('ui.context') or {}).get('facts') or {}).get('combat') or {}
            result.setdefault(match, []).append(dict(ev='native_vitals', hp=hp, max_hp=combat.get('max_hp'),
                game=game, source_ref=f'{report.relative_to(run).as_posix()}:steps[{index}].inspect_evidence.facts.motor'))
    return result


class NativeSafety:
    def __init__(self, *, hp_hard_stop=False):
        self.hp_hard_stop = hp_hard_stop
        self.max_hp = None
        self.counter = None
        self.first_stop = None
        self.game = {}
        self.relocation = None
        self.last_location = None

    def consume(self, row, ref, at=None):
        """Return factual outcome rows, never infer KO from an ordinary map change."""
        if isinstance(row.get('game'), dict):
            self.game = row['game']
        ev = row.get('ev')
        outcomes = []
        hp = maximum = None
        if ev == 'native_vitals':
            hp, maximum = row.get('hp'), row.get('max_hp')
        elif ev == 'reflex_combat_result':
            hp = min((v for v in (row.get('hp_min'), row.get('hp_end')) if number(v)), default=None)
            maximum = row.get('max_hp')
        elif ev in ('skill_mines_death', 'live7_knockout'):
            hp = row.get('hp')
        if number(maximum) and maximum > 0:
            self.max_hp = maximum
        if ev == 'native_vitals':
            location = self.game.get('location')
            if (self.last_location and location and location != self.last_location and number(hp)
                    and self.max_hp is not None and hp * 10 <= self.max_hp):
                self.relocation = ref  # candidate only; the saved counter proves a KO
            self.last_location = location
        if ev == 'native_vitals' and row.get('ko_relocation') is True:
            self.relocation = ref
        ko = ev in ('skill_mines_death', 'live7_knockout', 'native_knockout') or (
            ev == 'native_vitals' and row.get('ko_relocation') is True) or (
            ev == 'skill_session_finished' and str(row.get('reason', '')).startswith('death:'))
        refs = [ref] + ([row['source_ref']] if row.get('source_ref') else [])
        if ev == 'native_counter' and row.get('counter') == 'timesUnconscious' and type(row.get('value')) is int:
            current = row['value']
            if self.counter is not None and current > self.counter[0]:
                ko = True
                refs = [self.counter[1], ref] + ([self.relocation] if self.relocation else [])
            self.counter = (current, ref)
        if number(hp) and hp <= 0:
            ko = True
        if ko and ev != 'native_knockout':
            outcomes.append(dict(ev='native_knockout', reason='knockout', at=row.get('at') or at,
                                 game=self.game, refs=refs, basis='native outcome or timesUnconscious rise'))
        critical = number(hp) and self.max_hp is not None and hp * 10 <= self.max_hp
        if ko or critical:
            stop = dict(ev='harness_hard_stop', reason='knockout' if ko else 'hp_critical',
                        stop_class='owner-hard-stop', at=row.get('at') or at, game=self.game,
                        hp=hp, max_hp=self.max_hp, refs=refs)
            if (self.first_stop is None or number(stop['at']) and number(self.first_stop.get('at'))
                    and stop['at'] < self.first_stop['at']):
                if self.hp_hard_stop:
                    self.first_stop = stop
                    outcomes.append(stop)
                elif critical and not getattr(self, '_critical_seen', False):
                    self._critical_seen = True
                    outcomes.append(dict(stop, ev='hp_critical', stop_class='observe-only'))
        return outcomes
