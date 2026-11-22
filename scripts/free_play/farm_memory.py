"""Farm-bound, copy-only memory handoff beside the native save (ADR 144).

This packages the existing stores, not a second memory implementation. Missing
legacy stores are explicit unknowns; no bundle attests to whole-farm coverage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from scripts.free_play import storage, memory_checkpoint as cutoff

SCHEMA = 'arti.free_play.farm_memory/1'
# Persisted by live_cycle and consumed by the existing Live7 resume path.
HINTS = ('crop_memory', 'shop_memory', 'ground_items', 'catch_totals', 'catch_kinds',
         'chest_memory', 'forage_memory', 'seen_objects', 'cc_seen', 'item_names',
         'quest_refused_items')
MIND_FILES = ('prefs.jsonl', 'now.json', 'stardew/goals.jsonl', 'stardew/likes.jsonl',
              'stardew/journey.jsonl', 'stardew/discoveries.jsonl',
              'stardew/ledger.jsonl', 'stardew/progress.jsonl')
STATE_FILES = ('self.jsonl', 'wants.jsonl')
EVENTS = {'live7_crop_memory': ('crop_memory', 'crops'),
          'live7_ground_items': ('ground_items', 'items'),
          'live7_catch_totals': ('catch_totals', 'totals'),
          'live7_chest_memory': ('chest_memory', 'memory'),
          'live7_forage_memory': ('forage_memory', 'memory'),
          'live7_seen_objects': ('seen_objects', 'memory'),
          'live7_cc_seen': ('cc_seen', 'signature'),
          'live7_item_names': ('item_names', 'names'),
          'live7_shop_memory': ('shop_memory', 'memory'),
          'live7_quest_refusals': ('quest_refused_items', 'items')}


def read(path):
    return json.loads(storage.read_bytes(path).decode('utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def identity(candidate):
    return {k: str(candidate[k]) for k in ('save_id', 'host_id', 'farmhand_id')}


def key(candidate):
    ids = identity(candidate)
    if (not all(v.lstrip('-').isdigit() and int(v) != 0 for v in ids.values())
            or int(ids['save_id']) <= 0):
        raise ValueError('memory requires native save/actor identity')
    return ids['save_id'] + '-' + ids['farmhand_id']


def discovery_name(candidate):
    ids = identity(candidate)
    digest = hashlib.sha256((ids['save_id'] + ':' + ids['farmhand_id']).encode()).hexdigest()
    return 'discoveries-' + digest + '.json'


def native_names(candidate):
    ids = identity(candidate)
    return tuple(f"{kind}.{ids['save_id']}.{ids['farmhand_id']}.json" for kind in ('occurrences', 'nights'))


def copy_file(source, target):
    """Packed or raw evidence -> independent bytes. Never consult a source's live path."""
    data = storage.read_bytes(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    if storage.digest_file(source) != storage.digest_file(target):
        raise RuntimeError('memory copy byte verification failed')


def hashes(folder):
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): storage.digest_file(p)
            for p in storage.files(folder) if p != folder / 'MANIFEST.json'}


def bundle_path(run, candidate):
    return Path(run) / 'farm-memory' / key(candidate)


def inspect_bundle(run, candidate):
    folder = bundle_path(run, candidate)
    return inspect_folder(folder, candidate)


def inspect_folder(folder, candidate):
    folder = Path(folder)
    if not storage.exists(folder / 'MANIFEST.json'):
        return None  # legacy run, never silently read its mutable external mind_dir
    doc = read(folder / 'MANIFEST.json')
    if doc.get('schema') != SCHEMA or doc.get('identity') != identity(candidate):
        raise ValueError('farm memory identity/schema mismatch')
    if doc.get('hashes') != hashes(folder):
        raise ValueError('farm memory hashes mismatch')
    # Validate names even for raw manifests, before using a path from evidence.
    for name in doc['hashes']:
        storage.safe_child(folder, name)
    return folder, doc


def cycle_memory(run, candidate, *, checkpoint=None):
    """Legacy and current cycles: copy history only; never carry stops/grants/session IDs.

    Replay closed journals as well: the final tail may have landed after the last
    supervisor scan. Their full native evidence stays in the old run unchanged.
    """
    result, sources = {}, {}
    past_checkpoint = False
    for path in sorted((Path(run) / 'runtime/live7').glob('*/cycle_state.json')):
        state = read(path)
        if (str(state.get('save_id')) != str(candidate['save_id'])
                or str(state.get('player_id')) != str(candidate['farmhand_id'])):
            raise ValueError('cycle memory save/actor identity mismatch')
        if checkpoint is None:
            for name in HINTS:
                if name in state.get('hints', {}):
                    result[name] = state['hints'][name]
        sources[path.relative_to(run).as_posix()] = storage.digest_file(path)
        for journal in sorted(path.parent.glob('day-*/journal/g1_journal.jsonl')):
            date = next((day['date'] for day in state.get('days', [])
                         if any(a.get('id') == journal.parent.parent.name for a in day.get('attempts', []))), None)
            clock = cutoff.stamp(date)
            sources[journal.relative_to(run).as_posix()] = storage.digest_file(journal)
            for line in storage.read_bytes(journal).decode('utf-8-sig').splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a partial tail cannot erase the last complete memory
                if checkpoint is not None:
                    game = row.get('game')
                    if isinstance(game, dict) and (observed := cutoff.stamp(game)):
                        # Some memory rows omit time; retain the last clock on
                        # that date, never a time from a different game day.
                        clock = (observed[0], observed[1] if observed[1] is not None else
                                 clock[1] if clock and clock[0] == observed[0] else None)
                    if clock:
                        end = cutoff.stamp(checkpoint['date'], checkpoint['time'])
                        past_checkpoint |= (clock[0] > end[0] or clock[0] == end[0]
                                            and clock[1] is not None and clock[1] > end[1])
                    # A death/reconnect clock rewind does not make later
                    # aggregate snapshots evidence of the saved timeline.
                    if past_checkpoint or not cutoff.admits(clock, checkpoint):
                        continue
                if row.get('ev') in EVENTS:
                    name, field = EVENTS[row['ev']]
                    if field in row:
                        if name == 'item_names':
                            result.setdefault(name, {}).update(row[field])
                        elif name == 'quest_refused_items':
                            result[name] = sorted(set(result.get(name, ())) | set(row[field]))
                        else:
                            result[name] = row[field]
                if row.get('ev') == 'live7_catch_totals' and 'kinds' in row:
                    result['catch_kinds'] = row['kinds']
                if row.get('ev') == 'part_live_row' and date:
                    from scripts.live7.live7_common import CropMemory
                    crops = CropMemory(result.get('crop_memory'))
                    crops.record(row, date)
                    result['crop_memory'] = crops.crops
    return result, sources


def _manifest(folder, candidate, *, source, missing, sources=None):
    doc = dict(schema=SCHEMA, identity=identity(candidate), save_folder=candidate['save_folder'],
               source_run=str(source), coverage='recorded_history_only',
               missing_scope='predecessor coverage gaps plus absent end stores',
               missing=sorted(set(missing)), sources=sources or {}, hashes=hashes(folder),
               scope='unfiltered end evidence; continuation installs only chosen-native-save checkpoint memory')
    write(folder / 'MANIFEST.json', doc)
    return doc


def persona_memory(run, target, history=None, *, checkpoint=None):
    if checkpoint is not None:
        write(target / 'persona/discoveries.json', cutoff.snapshot_persona(run, history, checkpoint))
        return
    from arti_discovery_inbox import DiscoveryInbox
    transcript = Path(run) / 'bridge/bridge-transcript.jsonl'
    inbox = DiscoveryInbox(SimpleNamespace(path=transcript), history_path=history)
    write(target / 'persona/discoveries.json', inbox.snapshot())


def legacy_bundle(prior, candidate, target):
    hints, sources = cycle_memory(prior, candidate)
    write(target / 'cycle.json', hints)
    missing = ['mind/' + n for n in MIND_FILES] + ['state/' + n for n in STATE_FILES]
    missing += ['cycle/' + n for n in HINTS if n not in hints]
    name = discovery_name(candidate)
    source = Path(prior) / 'preserved-discoveries' / name
    if storage.exists(source):
        copy_file(source, target / 'discoveries' / name)
    else:
        missing.append('discoveries/' + name)
    missing += ['native/' + n for n in native_names(candidate)]
    persona_memory(prior, target)
    transcript = Path(prior) / 'bridge/bridge-transcript.jsonl'
    if transcript.is_file():
        sources['bridge/bridge-transcript.jsonl'] = storage.digest_file(transcript)
    return _manifest(target, candidate, source=prior, missing=missing, sources=sources)


def checkpoint_path(run, candidate, save_sha256):
    if len(save_sha256) != 64 or any(c not in '0123456789abcdef' for c in save_sha256):
        raise ValueError('invalid memory checkpoint save hash')
    return Path(run) / 'memory-checkpoints' / key(candidate) / save_sha256


def capture_checkpoint(run, candidate, save):
    """At the existing saved-day seam, before another day runner starts (ADR 144).

    The full isolated stores preserve overwritten goals, ledger facts and future
    files without guessing their schema. Exact native save hash selects them.
    """
    checkpoint = cutoff.native(save, candidate)
    target = checkpoint_path(run, candidate, checkpoint['save_sha256'])
    found = inspect_folder(target, candidate)
    if found:
        if found[1].get('checkpoint') != checkpoint:
            raise ValueError('memory checkpoint save binding mismatch')
        return found[1]
    doc = _freeze(run, candidate, target, checkpoint=checkpoint)
    doc['checkpoint'] = checkpoint
    doc['scope'] = 'complete memory at verified native saved-morning boundary'
    write(target / 'MANIFEST.json', doc)
    return doc


def checkpoint_start(run, prior, candidate, source, source_doc):
    checkpoint = cutoff.native(candidate['source'], candidate)
    snapshot = checkpoint_path(prior, candidate, checkpoint['save_sha256'])
    found = inspect_folder(snapshot, candidate)
    target = Path(run) / 'continuation-memory-checkpoint' / key(candidate)
    if found:
        folder, doc = found
        if doc.get('checkpoint') != checkpoint:
            raise ValueError('memory checkpoint save binding mismatch')
        for name in doc['hashes']:
            copy_file(storage.safe_child(folder, name), storage.safe_child(target, name))
        copy_file(folder / 'MANIFEST.json', target / 'MANIFEST.json')
        # ADR 152: older checkpoint snapshots can themselves contain fix83A's
        # committed-only legacy cut. Repair only the copy, bound to this save.
        for name in native_names(candidate):
            path = target / 'native' / name
            if name.startswith('nights.') and path.is_file():
                nights = read(path)
                if isinstance(nights, list) and nights and nights[-1].get('state') == 'committed':
                    write(path, cutoff.night_start(nights, candidate, checkpoint))
                    doc = dict(doc, hashes=hashes(target), native_repairs=['committed_only_night_tail:' + name])
                    write(target / 'MANIFEST.json', doc)
        mode = 'verified_save_snapshot'
    else:
        missing, sources = cutoff.cut_legacy(prior, source, target, candidate, checkpoint)
        doc = _manifest(target, candidate, source=prior, missing=source_doc['missing'] + missing, sources=sources)
        doc['checkpoint'] = checkpoint
        doc['scope'] = 'checkpoint-bounded legacy recovery; unavailable overwritten history remains unknown'
        write(target / 'MANIFEST.json', doc)
        mode = 'dated_legacy_recovery'
    write(Path(run) / 'memory-cut.json', dict(checkpoint=checkpoint, mode=mode,
        source_run=str(prior), archive=str(source), selected=str(target), missing=doc['missing'],
        native_repairs=doc.get('native_repairs', []),
        scope='post-save experiences are rolled back, not facts in this continued game'))
    return target, doc


def seed_later_record(active, candidate, entry):
    """Recreate the reload situation from two verified checkpoints of the same farm."""
    from scripts.free_play.carryover import inspect_candidate
    from arti_runtime.save_reload import present_day
    from arti_runtime.plan_dates import ordinal

    recorded = inspect_candidate(entry, candidate["farm"])
    if identity(recorded) != identity(candidate):
        raise ValueError("later record belongs to another native save or player")
    folder, doc = inspect_folder(checkpoint_path(entry, recorded, recorded["hashes"][recorded["save_folder"]]),
                                 recorded)
    year, season, day = candidate["saved_date"]
    today = ordinal("stardew", f"Y{year} {season} {day}")
    names = ("mind/stardew/progress.jsonl", "mind/stardew/ledger.jsonl")
    if names[0] not in doc["hashes"]:
        raise ValueError("later checkpoint has no scoped progress record")
    # Only she may write the reload row. The bench supplies the earlier native
    # save and the later existing record, including its prior reload timeline.
    for name in names:
        target = Path(active) / name
        if name in doc["hashes"]:
            copy_file(folder / name, target)
        elif target.is_file():
            target.unlink()
    ids = identity(candidate)
    present = present_day(Path(active) / "mind", ids["save_id"] + ":" + ids["farmhand_id"])
    if present is None or today is None or present <= today:
        raise ValueError("later checkpoint does not prove an ahead-of-save record")


def prepare(run, candidate, mod_data, *, prior=None, personal_mind=None, personal_state=None, inbox=None,
            later_record=None):
    """Before either consumer starts: isolate writable state and seed verified copies.

    Only an explicit prior run supplies farm history. A fresh run retains personal
    prefs/self/wants; it never imports another farm's unscoped goals/journey.
    """
    run = Path(run)
    active = run / 'farm-state' / key(candidate)
    active.mkdir(parents=True, exist_ok=False)
    if prior:
        # Re-read the chosen preserved XML, including packed saves. STAGING's
        # start date or the terminal live clock must never choose the cutoff.
        from scripts.free_play.carryover import inspect_candidate
        selected = inspect_candidate(prior, candidate.get('farm', 'Standard'), candidate.get('continuation_checkpoint'))
        if identity(selected) != identity(candidate) or selected['save_folder'] != candidate['save_folder']:
            raise ValueError('memory checkpoint selected save identity mismatch')
        if candidate.get('staged_hashes') and selected['hashes'] != candidate['staged_hashes']:
            raise ValueError('memory checkpoint source differs from staged native save bytes')
        start = run / 'continuation-memory-start' / key(candidate)
        found = inspect_bundle(prior, candidate)
        if not found and (Path(prior) / 'manifest.json').is_file():
            # ADR 150 library contains only one bounded bundle, no end archive.
            found = inspect_folder(checkpoint_path(prior, selected,
                selected['hashes'][selected['save_folder']]), candidate)
        if found:
            source, doc = found
            for name in doc['hashes']:
                copy_file(storage.safe_child(source, name), storage.safe_child(start, name))
            copy_file(source / 'MANIFEST.json', start / 'MANIFEST.json')
        else:
            doc = legacy_bundle(Path(prior), candidate, start)
        bounded, doc = checkpoint_start(run, prior, selected, start, doc)
        for name in doc['hashes']:
            copy_file(storage.safe_child(bounded, name), storage.safe_child(active, name))
        missing = doc['missing']
    else:
        for folder, names, source in (('mind', ('prefs.jsonl',), personal_mind),
                                       ('state', STATE_FILES, personal_state)):
            for name in names:
                if source and Path(source, name).is_file():
                    copy_file(Path(source, name), active / folder / name)
        write(active / 'cycle.json', {})
        missing = ['farm history before this run is unknown']
    if later_record:
        seed_later_record(active, candidate, later_record)
    # Only this native save+actor's discovery file is replaced; Baseline owns its
    # exact restoration. No unrelated save's discovery ledger enters this farm.
    name = discovery_name(candidate)
    source = active / 'discoveries' / name
    destination = Path(mod_data) / name
    if source.is_file():
        copy_file(source, destination)
    else:
        write(destination, {})
    if inbox:
        for name in native_names(candidate):
            source, dest = active / 'native' / name, storage.safe_child(inbox, name)
            if source.is_file():
                copy_file(source, dest)
            elif dest.is_file():
                dest.unlink()  # current mutable copy only; its baseline is already captured
    (active / 'mind').mkdir(exist_ok=True)
    (active / 'state').mkdir(exist_ok=True)
    # now.json is archived byte-for-byte but is a live cursor, not a historical
    # fact. Do not let an unsaved old pose/intent reach speech before a fresh read.
    now = active / 'mind/now.json'
    if now.exists():
        now.unlink()
    runtime = dict(schema=SCHEMA, identity=identity(candidate), active=str(active),
                   mind=str(active / 'mind'), state=str(active / 'state'),
                   discovery=str(destination), inbox=str(inbox) if inbox else None,
                   missing=missing, source_run=str(prior) if prior else None)
    write(run / 'memory-runtime.json', runtime)
    if prior:
        # If the next run dies before a new save, this exact start remains the
        # checkpoint for its successor, including every undated mutable store.
        capture_checkpoint(run, selected, selected['source'])
    return runtime


def seed_cycle(run, cycle):
    """Enrollment has exited; seed its normal hints before the first day starts."""
    runtime = read(Path(run) / 'memory-runtime.json')
    state_path = Path(cycle) / 'cycle_state.json'
    state = read(state_path)
    ids = runtime['identity']
    if str(state.get('save_id')) != ids['save_id'] or str(state.get('player_id')) != ids['farmhand_id']:
        raise ValueError('enrolled cycle memory identity mismatch')
    hints = read(Path(runtime['active']) / 'cycle.json')
    state.setdefault('hints', {}).update({k: v for k, v in hints.items() if k in HINTS})
    write(state_path, state)


def preserve(run, candidate):
    """After writers stop, before baseline restore: freeze the next link in the chain."""
    return _freeze(run, candidate, bundle_path(run, candidate))


def _freeze(run, candidate, target, *, checkpoint=None):
    run = Path(run)
    runtime = read(run / 'memory-runtime.json')
    if runtime['identity'] != identity(candidate):
        raise ValueError('end memory save/actor identity mismatch')
    active, target = Path(runtime['active']), Path(target)
    target.mkdir(parents=True, exist_ok=False)
    for folder in ('mind', 'state'):
        for path in sorted((active / folder).rglob('*')):
            if path.is_file():
                copy_file(path, target / folder / path.relative_to(active / folder))
    persona_memory(run, target, active / 'persona/discoveries.json', checkpoint=checkpoint)
    hints, sources = cycle_memory(run, candidate)
    # Enrollment/start failure must not erase memories that were seeded.
    hints = dict(read(active / 'cycle.json'), **hints)
    write(target / 'cycle.json', hints)
    name = discovery_name(candidate)
    if Path(runtime['discovery']).is_file():
        copy_file(runtime['discovery'], target / 'discoveries' / name)
    missing = list(runtime['missing'])
    for name in native_names(candidate):
        source = Path(runtime['inbox']) / name if runtime.get('inbox') else active / 'native' / name
        if source.is_file():
            copy_file(source, target / 'native' / name)
        else:
            missing.append('native/' + name)
    missing += ['cycle/' + n for n in HINTS if n not in hints]
    missing += ['mind/' + n for n in MIND_FILES if not (target / 'mind' / n).is_file()]
    if checkpoint is not None:
        cutoff.seal_snapshot(target, checkpoint)
    return _manifest(target, candidate, source=run, missing=missing, sources=sources)


def next_memory(run, candidate):
    found = inspect_bundle(run, candidate)
    if not found:
        return dict(available=False, coverage='unknown', reason='legacy run has no frozen mind/state bundle')
    folder, doc = found
    checkpoint = cutoff.native(candidate['source'], candidate)
    snapshot = checkpoint_path(run, candidate, checkpoint['save_sha256'])
    saved = inspect_folder(snapshot, candidate)
    if saved and saved[1].get('checkpoint') != checkpoint:
        raise ValueError('memory checkpoint save binding mismatch')
    selected, selected_doc = saved if saved else (folder, doc)
    return dict(available=True, path=str(selected), manifest=str(selected / 'MANIFEST.json'),
                manifest_sha256=storage.digest_file(selected / 'MANIFEST.json'),
                identity=selected_doc['identity'], hashes=selected_doc['hashes'],
                coverage=selected_doc['coverage'], missing=selected_doc['missing'], end_archive=str(folder),
                checkpoint=checkpoint, checkpoint_path=str(snapshot) if saved else None,
                checkpoint_manifest_sha256=storage.digest_file(snapshot / 'MANIFEST.json') if saved else None,
                requires_checkpoint_recovery=not bool(saved),
                scope='install checkpoint snapshot, or dated legacy recovery; never unfiltered end evidence')
