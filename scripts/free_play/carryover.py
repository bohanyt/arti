"""Explicit campaign save handoff; never mutate a prior run or rewrite XML (ADR 142)."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

from scripts.free_play import storage
from scripts.free_play.memory_checkpoint import stamp


def checkpoint_date(value):
    """CLI native date, independent of directory/day-runner numbering (ADR 150)."""
    import re
    match = re.fullmatch(r'Y(\d+)-(spring|summer|fall|winter)-(\d+)', value or '', re.I)
    if not match:
        raise ValueError('checkpoint must be a native date such as Y1-summer-19')
    year, season, day = match.groups()
    date = [int(year), season.lower(), int(day)]
    if not stamp(date):
        raise ValueError('invalid native checkpoint date')
    return date


def select_folder(run, name, checkpoint=None):
    end = storage.safe_child(run / 'preserved-saves', name)
    if checkpoint is None:
        return end  # ADR 142 default remains the end save
    date = checkpoint_date(checkpoint)
    folders = [end, run / 'continuation-start' / name, run / 'template' / name]
    folders += sorted(run.glob('runtime/live7/*/checkpoints/*/save'))
    folders += sorted(run.glob('checkpoints/*/save'))
    matches = []
    for folder in folders:
        main = folder / name
        if not storage.exists(main):
            continue
        root = ET.fromstring(storage.read_bytes(main))
        native = [int(root.findtext('year', '0')), root.findtext('currentSeason'), int(root.findtext('dayOfMonth', '0'))]
        if native == date:
            matches.append(folder)
    if not matches:
        raise ValueError(f'checkpoint {checkpoint} is not in run {run.name}; no fallback to end save')
    if len({storage.digest_file(p / name) for p in matches}) != 1:
        raise ValueError(f'checkpoint {checkpoint} has conflicting native save versions; select a different run')
    return matches[0]


def read(path):
    return json.loads(storage.read_bytes(path).decode('utf-8-sig'))


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def inspect_candidate(run, farm, checkpoint=None):
    """Verify the saved HOST world and established farmhand, including packed saves.

    Multiplayer farmhands live in the HOST SaveGame XML, not a second save file.
    The end save is the last SAVED morning; unsaved terminal-day actions are excluded.
    """
    run = Path(run).resolve()
    staging = read(run / 'STAGING.json')
    restore = read(run / 'restore.json')
    if restore.get('saves_identical') is not True or restore.get('files_identical') is not True:
        raise ValueError('continuation requires a closed run with verified restoration')
    name = staging.get('save_folder')
    if not isinstance(name, str) or not name or name in ('.', '..') or any(c in name for c in '/\\:'):
        raise ValueError('invalid continuation save folder')
    folder = select_folder(run, name, checkpoint)
    main, info = folder / name, folder / 'SaveGameInfo'
    if not storage.exists(main) or not storage.exists(info):
        raise ValueError('continuation requires a complete main save and SaveGameInfo')
    # Validate all selected files, including _old siblings, before exposing any copy.
    hashes = selected_hashes(folder)
    try:
        root = ET.fromstring(storage.read_bytes(main))
        host_info = ET.fromstring(storage.read_bytes(info))
        if root.tag != 'SaveGame' or host_info.tag != 'Farmer':
            raise ValueError('invalid save document roots')
        host = root.find('player')
        host_id = int(host.findtext('UniqueMultiplayerID', '0'))
        host_info_id = int(host_info.findtext('UniqueMultiplayerID', '0'))
        save_id = int(root.findtext('uniqueIDForThisGame', '0'))
        which = int(root.findtext('whichFarm', '-1'))
        year = int(root.findtext('year', '0'))
        season = root.findtext('currentSeason')
        day = int(root.findtext('dayOfMonth', '0'))
        time = int(root.findtext('timeOfDay', '600'))
        peers = root.findall('./farmhands/Farmer')
        peers += [e for e in root.findall('.//farmhand') if e.find('UniqueMultiplayerID') is not None]
        peers = {int(p.findtext('UniqueMultiplayerID', '0')): p for p in peers}
    except (ET.ParseError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError('incomplete continuation XML / native identities') from exc
    if (not host_id or save_id <= 0 or host_info_id != host_id or host_id != staging.get('host_id')
            or save_id != staging.get('save_id')):
        raise ValueError('continuation HOST / SaveGameInfo / save identity mismatch')
    peers.pop(host_id, None); peers.pop(0, None)
    peer_id = staging.get('farmhand_id')
    if (len(peers) != 1 or peer_id not in peers
            or peers[peer_id].findtext('name') != staging.get('farmhand_name')):
        raise ValueError('continuation must retain the same established farmhand')
    if which != {'Standard': 0, 'Wilderness': 4}[farm]:
        raise ValueError('continuation farm mismatch')
    if year < 1 or season not in ('spring', 'summer', 'fall', 'winter') or not 1 <= day <= 28 or time != 600:
        raise ValueError('continuation must contain a complete saved morning')
    # SaveGameInfo and both farmers must describe this same saved morning when
    # their native save-date fields are present (old versions may omit them).
    saved_date = (str(day), str(('spring', 'summer', 'fall', 'winter').index(season)), str(year))
    for farmer in (host_info, host, peers[peer_id]):
        stamp = tuple(farmer.findtext(k) for k in ('dayOfMonthForSaveGame', 'seasonForSaveGame', 'yearForSaveGame'))
        if any(v is not None for v in stamp) and stamp != saved_date:
            raise ValueError('continuation HOST/farmhand save-date mismatch')
    return dict(source_run=str(run), source=str(folder), save_folder=name, save_id=save_id,
        host_id=host_id, farmhand_id=peer_id, farmhand_name=peers[peer_id].findtext('name'),
        farm=farm, saved_date=[year, season, day], hashes=hashes, continuation_checkpoint=checkpoint,
        scope='last native saved morning; HOST world and farmhand in the same XML; unsaved terminal-day progress excluded')


def selected_hashes(folder):
    """Hash a selected subtree even when its index belongs to its parent."""
    folder = Path(folder)
    if not (folder / storage.INDEX).is_file() and (folder.parent / storage.INDEX).is_file():
        names = storage.index(folder.parent)[0]['files']
        paths = [storage.safe_child(folder.parent, n) for n in names if n.split('/')[0] == folder.name]
    else:
        paths = storage.files(folder)
    return {p.relative_to(folder).as_posix(): storage.digest_file(p) for p in paths}


def archive_start(candidate, output):
    source = Path(candidate['source'])
    output = Path(output)
    archive = output / 'continuation-start'
    # capture() accepts packed logical members through the parent index.
    members = [storage.safe_child(source, name) for name in candidate['hashes']]
    storage.capture(source, archive / candidate['save_folder'], output, members=members)
    if selected_hashes(archive / candidate['save_folder']) != candidate['hashes']:
        raise RuntimeError('continuation archive byte verification failed')
    return archive / candidate['save_folder']


def stage_continuation(prior_run, saves, farm, output, baseline, checkpoint=None):
    candidate = inspect_candidate(prior_run, farm, checkpoint)
    saves, output = Path(saves).resolve(), Path(output).resolve()
    if baseline.saves.resolve() != saves or not (output / 'baseline.json').is_file():
        raise ValueError('capture current baseline before continuation staging')
    if candidate['save_folder'] not in baseline.owned_saves:
        raise ValueError('continuation save not owned by baseline')
    if baseline.archive_manifest() != baseline.record['saves']:
        raise RuntimeError('baseline backup changed before continuation staging')
    if baseline.saves_manifest() != baseline.record['saves']:
        raise RuntimeError('current saves changed after baseline capture')
    archive = archive_start(candidate, output)
    record = dict(schema='arti.free_play.continuation/1', **candidate,
        baseline_manifest='baseline.json', start_archive=str(archive), staged_identical=False)
    write(output / 'continuation.json', record)
    destination = storage.safe_child(saves, candidate['save_folder'])
    if destination.exists():
        displaced = output / 'continuation-displaced' / destination.name
        displaced.parent.mkdir()
        # Both resolved paths are bounded by the authorized saves/output roots.
        if not destination.resolve().is_relative_to(saves) or not displaced.resolve().is_relative_to(output):
            raise ValueError('continuation collision path escapes backup roots')
        shutil.move(str(destination), str(displaced))
    storage.materialize(archive, destination)
    actual = selected_hashes(destination)
    if actual != candidate['hashes']:
        raise RuntimeError('continuation start byte verification failed')
    record.update(staged_identical=True, staged_hashes=actual)
    write(output / 'continuation.json', record)
    fixture = dict(tag='STAGING', template=str(archive), template_hashes=candidate['hashes'],
        staged_hashes=actual, farm=farm, save_folder=candidate['save_folder'], save_id=candidate['save_id'],
        host_id=candidate['host_id'], farmhand_id=candidate['farmhand_id'], farmhand_name=candidate['farmhand_name'],
        changes=[], continuation_from=candidate['source_run'], saved_date=candidate['saved_date'],
        continuation_checkpoint=checkpoint)
    write(output / 'STAGING.json', fixture)
    return fixture


def retain_candidate(run):
    """Record eligibility after normal cleanup/restoration, never auto-select a run."""
    run = Path(run)
    staging = read(run / 'STAGING.json') if (run / 'STAGING.json').is_file() else {}
    try:
        candidate = inspect_candidate(run, staging.get('farm', 'Standard'))
        from scripts.free_play.farm_memory import next_memory
        result = dict(schema='arti.free_play.next_candidate/1', available=True, **candidate,
                      memory=next_memory(run, candidate))
    except (OSError, ValueError, KeyError) as exc:
        result = dict(schema='arti.free_play.next_candidate/1', available=False, reason=str(exc))
    write(run / 'next-candidate.json', result)
    return result
