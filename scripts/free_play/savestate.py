"""Copy-only checkpoint library and journal checks (ADR 150). No game imports.

python -m scripts.free_play.savestate add RUN --id ID --checkpoint Y1-summer-19
  --expect conditions.json --broken DESCRIPTION --journal-ref relative/journal:LINE
python -m scripts.free_play.savestate list|verify [ID]
python -m scripts.free_play.savestate check RUN --state ID
python -m scripts.free_play.savestate from-checkpoint .../live7/c1/checkpoints/day-NNNN-* --id ID
  --needs DESCRIPTION --journal-ref REF [--mind rig-run/farm-state/SAVE-FARMHAND]   (ADR 242)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import tempfile

from scripts.free_play import carryover as C, farm_memory as F, storage
from scripts.free_play.expectations import Engine, JournalCheck

LIBRARY = Path(__file__).resolve().parents[3] / "lab-rig" / "savestates"
SCHEMA = 'arti.free_play.savestate/1'
FLOOR = 15 * 1024**3


def entry_path(state_id, library=LIBRARY):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', str(state_id)):
        raise ValueError('invalid savestate id')
    return storage.safe_child(library, str(state_id)).resolve()


def resolve_source(value, library=LIBRARY):
    path = Path(value)
    if path.is_dir():
        path = path.resolve()
    elif path.name == str(value):
        path = entry_path(str(value), library)
    else:
        raise ValueError('continuation run folder does not exist: ' + str(value))
    if (path / 'manifest.json').is_file():
        verify(path)
    elif not (path / 'STAGING.json').is_file():
        raise ValueError('unknown savestate/run: ' + str(value))
    return path


def payload_hashes(entry):
    return {p.relative_to(entry).as_posix(): storage.digest_file(p)
            for p in storage.files(entry) if p != entry / 'manifest.json'}


def verify(entry):
    entry = Path(entry).resolve()
    doc = F.read(entry / 'manifest.json')
    if doc.get('schema') != SCHEMA or doc.get('id') != entry.name:
        raise ValueError('savestate schema/id mismatch')
    Engine(doc.get('expect'))
    if doc.get('hashes') != payload_hashes(entry):
        raise ValueError('savestate payload hashes mismatch')
    for name in doc['hashes']:
        storage.safe_child(entry, name)
    candidate = C.inspect_candidate(entry, doc['farm'])
    if (F.identity(candidate) != doc['identity'] or candidate['saved_date'] != C.checkpoint_date(doc['native_date'])
            or candidate['hashes'] != doc['save_hashes']):
        raise ValueError('savestate native identity/date/hash mismatch')
    snapshot = F.checkpoint_path(entry, candidate, candidate['hashes'][candidate['save_folder']])
    found = F.inspect_folder(snapshot, candidate)
    if not found or found[1].get('checkpoint') != F.cutoff.native(candidate['source'], candidate):
        raise ValueError('savestate memory checkpoint mismatch')
    return doc


def add(run, state_id, *, expect, broken, journal_refs, checkpoint=None, library=LIBRARY):
    Engine(expect)
    if not broken or not journal_refs:
        raise ValueError('describe what broke and provide journal references')
    run = resolve_source(run, library)
    staging = F.read(run / 'STAGING.json')
    candidate = C.inspect_candidate(run, staging.get('farm', 'Standard'), checkpoint)
    library = Path(library).resolve()
    entry = entry_path(state_id, library)
    if entry.exists():
        raise ValueError('savestate already exists; never overwrite')
    library.mkdir(parents=True, exist_ok=True)
    # Tiny, bounded copies only. Check both before recovery and before publishing.
    if shutil.disk_usage(library).free - 3 * 64 * 1024**2 < FLOOR:
        raise ValueError('savestate copy would cross 15 GiB free-space floor')
    with tempfile.TemporaryDirectory(prefix='savestate-', dir=library) as tmp:
        temp = Path(tmp)
        snapshot = F.checkpoint_path(run, candidate, candidate['hashes'][candidate['save_folder']])
        found = F.inspect_folder(snapshot, candidate)
        if found:
            bounded, memory = found
        else:
            source = temp / 'legacy-evidence'
            found = F.inspect_bundle(run, candidate)
            if found:
                source, source_doc = found
            else:
                source_doc = F.legacy_bundle(run, candidate, source)
            bounded, memory = F.checkpoint_start(temp, run, candidate, source, source_doc)
        size = sum(storage.file_size(Path(candidate['source']) / n) for n in candidate['hashes'])
        size += sum(storage.file_size(bounded / n) for n in memory['hashes']) + storage.file_size(bounded / 'MANIFEST.json')
        if size > 64 * 1024**2:
            raise ValueError('savestate exceeds 64 MiB; inspect checkpoint/memory selection')
        if shutil.disk_usage(library).free - size - 1024**2 < FLOOR:
            raise ValueError('savestate copy would cross 15 GiB free-space floor')
        staged = temp / str(state_id)
        staged.mkdir()
        for name in candidate['hashes']:
            F.copy_file(Path(candidate['source']) / name, staged / 'preserved-saves' / candidate['save_folder'] / name)
        destination = F.checkpoint_path(staged, candidate, candidate['hashes'][candidate['save_folder']])
        for name in (*memory['hashes'], 'MANIFEST.json'):
            F.copy_file(bounded / name, destination / name)
        F.write(staged / 'STAGING.json', dict(staging, saved_date=candidate['saved_date']))
        F.copy_file(run / 'restore.json', staged / 'restore.json')
        size = sum(storage.file_size(p) for p in storage.files(staged))
        year, season, day = candidate['saved_date']
        doc = dict(schema=SCHEMA, id=str(state_id), source_run=str(run), source_checkpoint=candidate['source'],
            native_date=f'Y{year}-{season}-{day}', farm=candidate['farm'], game_id=candidate['save_id'],
            identity=F.identity(candidate), save_folder=candidate['save_folder'], save_hashes=candidate['hashes'],
            broken=dict(description=broken, journal_refs=journal_refs), expect=expect,
            memory=dict(checkpoint=memory['checkpoint'], missing=memory['missing'], scope=memory['scope']),
            hashes=payload_hashes(staged), payload_bytes=size,
            witness=dict(status='DESIGN_ONLY', sequence=[], readbacks='existing ADR 132 pinned native witness facts'))
        F.write(staged / 'manifest.json', doc)
        verify(staged)
        # Publish by exclusive copy; even the temporary selected bytes are not moved.
        shutil.copytree(staged, entry)
    verify(entry)
    return entry


IMPORT_MIND = tuple(n for n in F.MIND_FILES if n != 'prefs.jsonl')  # personal prefs never enter a test world


def from_checkpoint(checkpoint, state_id, *, expect, broken, journal_refs, farm='Standard', mind=None,
                    library=LIBRARY, cheats=True):
    """ADR 242: turn one live checkpoint (`.../live7/c*/checkpoints/day-*/` = `save/` + `manifest.json`) of a real
    co-op session into a bench savestate. Bytes are copied and hash-checked; the source folder is only read.

    The copied save must be a confirmed night save of a test world with exactly one farmhand (ARTI). Its memory is a
    test-world memory: no hints, plus the dated mind/state files of an explicit `mind` folder (a rig run's
    `farm-state/<save>-<farmhand>`), never personal prefs. Its restore.json is the import receipt: the copied save
    bytes equal the checkpoint manifest; no rig session ran.
    """
    import xml.etree.ElementTree as ET
    from scripts.free_play.bench import protected, reviewed, safe_save
    from scripts.free_play import memory_checkpoint as cutoff
    Engine(expect)
    if not broken or not journal_refs:
        raise ValueError('describe what the check needs and provide journal references')
    source = protected(Path(checkpoint))
    doc = F.read(reviewed(source / 'manifest.json'))
    if (doc.get('tag') != 'LIVE_CHECKPOINT' or doc.get('status') != 'copied'
            or (doc.get('night_save') or {}).get('confirmed') is not True):
        raise ValueError('not a confirmed LIVE_CHECKPOINT night-save copy')
    name, files = doc.get('save_name'), doc.get('files')
    if not isinstance(name, str) or not isinstance(files, dict) or name not in files or 'SaveGameInfo' not in files:
        raise ValueError('checkpoint manifest names no complete save')
    save = protected(source / 'save')
    present = {p.name for p in save.iterdir() if p.is_file()}
    if present != set(files) or any(storage.digest_file(save / n) != h for n, h in files.items()):
        raise ValueError('checkpoint save bytes differ from its manifest')
    root = ET.fromstring(storage.read_bytes(save / name))
    save_id = safe_save(root.findtext('uniqueIDForThisGame'), name)  # refuses 100000001 by id and folder
    host_id = int(root.findtext('player/UniqueMultiplayerID', '0'))
    peers = root.findall('./farmhands/Farmer') + [e for e in root.findall('.//farmhand')
                                                  if e.find('UniqueMultiplayerID') is not None]
    peers = {int(p.findtext('UniqueMultiplayerID', '0')): p for p in peers}
    peers.pop(host_id, None); peers.pop(0, None)
    if len(peers) != 1:
        raise ValueError('a bench savestate needs exactly one farmhand in the copied save')
    farmhand_id, peer = next(iter(peers.items()))
    library = Path(library).resolve()
    entry = entry_path(state_id, library)
    if entry.exists():
        raise ValueError('savestate already exists; never overwrite')
    library.mkdir(parents=True, exist_ok=True)
    size = sum(storage.file_size(save / n) for n in files)
    if shutil.disk_usage(library).free - 2 * size - 1024**2 < FLOOR:
        raise ValueError('savestate copy would cross 15 GiB free-space floor')
    with tempfile.TemporaryDirectory(prefix='savestate-', dir=library) as tmp:
        staged = Path(tmp) / str(state_id)
        for n in files:
            F.copy_file(save / n, staged / 'preserved-saves' / name / n)
        edits = []
        main = staged / 'preserved-saves' / name / name
        if cheats and b'<allowChatCheats>true</allowChatCheats>' not in main.read_bytes():
            # benchrun4 03-quest-log: a co-op save without chat cheats refuses every /debug staging line. A bench
            # savestate is a disposable test save: stage_save's own edit, recorded (the source stays as it was).
            from scripts.m1_operator.stage_save import apply_edits
            new, edits = apply_edits(main.read_bytes(), {'allowChatCheats': 'true'})
            main.write_bytes(new)
        F.write(staged / 'STAGING.json', dict(
            tag='STAGING', save_folder=name, save_id=save_id, host_id=host_id, farmhand_id=farmhand_id,
            farmhand_name=peer.findtext('name'), farm=farm, changes=[], template_hashes=files, staged_hashes=files,
            imported_from=str(source)))
        F.write(staged / 'restore.json', dict(
            kind='live_checkpoint_import', saves_identical=True, files_identical=True, file_sets_identical=True,
            basis='copied save bytes equal the checkpoint manifest hashes, then the recorded edits; no rig session ran',
            edits=edits,
            source_manifest=str(source / 'manifest.json'),
            source_manifest_sha256=storage.digest_file(source / 'manifest.json')))
        candidate = C.inspect_candidate(staged, farm)
        native = cutoff.native(candidate['source'], candidate)
        memory = F.checkpoint_path(staged, candidate, candidate['hashes'][name])
        F.write(memory / 'cycle.json', {})
        copied = []
        if mind is not None:
            mind = reviewed(mind)
            for folder, names in (('mind', IMPORT_MIND), ('state', F.STATE_FILES)):
                for n in names:
                    if (mind / folder / n).is_file():
                        F.copy_file(mind / folder / n, memory / folder / n)
                        copied.append(f'{folder}/{n}')
        missing = (['mind/' + n for n in F.MIND_FILES if f'mind/{n}' not in copied]
                   + ['state/' + n for n in F.STATE_FILES if f'state/{n}' not in copied]
                   + ['cycle/' + n for n in F.HINTS] + ['discoveries/' + F.discovery_name(candidate)]
                   + ['native/' + n for n in F.native_names(candidate)])
        mdoc = F._manifest(memory, candidate, source=source, missing=missing)
        mdoc.update(checkpoint=native, scope='imported live checkpoint; test-world memory (ADR 242)')
        F.write(memory / 'MANIFEST.json', mdoc)
        year, season, day = candidate['saved_date']
        doc = dict(schema=SCHEMA, id=str(state_id), source_run=str(source), source_checkpoint=str(save),
                   native_date=f'Y{year}-{season}-{day}', farm=farm, game_id=save_id,
                   identity=F.identity(candidate), save_folder=name, save_hashes=candidate['hashes'],
                   broken=dict(description=broken, journal_refs=journal_refs), expect=expect,
                   memory=dict(checkpoint=native, missing=missing, scope=mdoc['scope'], copied=copied),
                   imported=dict(kind='live_checkpoint', manifest=str(source / 'manifest.json'), edits=edits,
                                 copy_state=doc.get('copy_state'), night_save=doc.get('night_save', {}).get('basis')),
                   hashes=payload_hashes(staged), payload_bytes=sum(storage.file_size(p) for p in storage.files(staged)),
                   witness=dict(status='DESIGN_ONLY', sequence=[], readbacks='existing ADR 132 pinned native witness facts'))
        F.write(staged / 'manifest.json', doc)
        verify(staged)
        shutil.copytree(staged, entry)
    verify(entry)
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, default=LIBRARY)
    sub = parser.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('from-checkpoint')
    imp.add_argument('checkpoint', type=Path)
    imp.add_argument('--id', required=True)
    imp.add_argument('--farm', default='Standard', choices=('Standard', 'Wilderness'))
    imp.add_argument('--expect', type=Path)
    imp.add_argument('--needs', required=True, help='what the check needs from this state')
    imp.add_argument('--journal-ref', action='append', required=True)
    imp.add_argument('--mind', type=Path, help="a rig run's farm-state/<save>-<farmhand> folder (test world only)")
    add_parser = sub.add_parser('add')
    add_parser.add_argument('run', type=Path)
    add_parser.add_argument('--id', required=True)
    add_parser.add_argument('--checkpoint')
    add_parser.add_argument('--expect', required=True, type=Path)
    add_parser.add_argument('--broken', required=True)
    add_parser.add_argument('--journal-ref', action='append', required=True)
    sub.add_parser('list')
    verify_parser = sub.add_parser('verify'); verify_parser.add_argument('id')
    check_parser = sub.add_parser('check'); check_parser.add_argument('run', type=Path)
    check_parser.add_argument('--state', required=True)
    check_parser.add_argument('--final', action='store_true', help='fail missing evidence even for a running run')
    args = parser.parse_args(argv)
    try:
        if args.command == 'from-checkpoint':
            expect = F.read(args.expect) if args.expect else [dict(
                id='next_native_morning', kind='event', description='the copied morning starts',
                match=dict(ev='skill_session_started'))]
            path = from_checkpoint(args.checkpoint, args.id, expect=expect, broken=args.needs,
                                   journal_refs=args.journal_ref, farm=args.farm, mind=args.mind,
                                   library=args.library)
            print(json.dumps(dict(path=str(path), **verify(path)), indent=2))
        elif args.command == 'add':
            path = add(args.run, args.id, expect=F.read(args.expect), broken=args.broken,
                       journal_refs=args.journal_ref, checkpoint=args.checkpoint, library=args.library)
            print(json.dumps(dict(path=str(path), **verify(path)), indent=2))
        elif args.command == 'list':
            for path in sorted(args.library.glob('*/manifest.json')):
                doc = F.read(path)
                print(f"{doc['id']} {doc['native_date']} {doc['payload_bytes']} bytes {doc['broken']['description']}")
        elif args.command == 'verify':
            path = resolve_source(args.id, args.library)
            doc = verify(path)
            print(f"PASS {doc['id']} {doc['native_date']} {len(doc['hashes'])} payload hashes")
        else:
            doc = verify(resolve_source(args.state, args.library))
            result = JournalCheck(args.run, doc['expect']).poll(final=args.final or (args.run / 'restore.json').is_file(),
                stop_reason='run failure' if (args.run / 'failure.json').is_file() else None)
            for condition in result['conditions']:
                print(f"{condition['status']} {condition['id']} {' '.join(condition['refs'])}")
            print(result['status'])
            return {'PASS': 0, 'FAIL': 1, 'PENDING': 3}[result['status']]
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print('FAIL ' + str(exc))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
