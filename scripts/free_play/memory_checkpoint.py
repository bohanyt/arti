"""ADR 144: a continued mind may know only the chosen native save's timeline.

New runs freeze complete stores between saved days. Legacy end-only stores have
less evidence: replay dated journals, retain provably dated rows and disclose
unrecoverable overwritten/undated state instead of promoting it to a fact.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.free_play import storage

SEASONS = ('spring', 'summer', 'fall', 'winter')


def stamp(value, clock=None):
    """Native list/object, Y1 summer 11, or 1:summer:11 [0810]. Unknown time stays unknown."""
    if isinstance(value, dict):
        if 'date' in value:
            return stamp(value['date'], value.get('time', value.get('time_of_day', clock)))
        value = [value.get(k) for k in ('year', 'season', 'day')]
    if isinstance(value, str):
        match = re.fullmatch(r'Y?(\d+)[: ](spring|summer|fall|winter)[: ](\d+)(?:\s+(\d{1,4}|\d{1,2}:\d{2}|UNKNOWN))?', value, re.I)
        if not match:
            return None
        year, season, day, time = match.groups()
        value = [int(year), season.lower(), int(day)]
        if time and time.upper() != 'UNKNOWN':
            clock = int(time.replace(':', ''))
    try:
        year, season, day = value
        if type(year) is not int or year < 1 or season not in SEASONS or type(day) is not int or not 1 <= day <= 28:
            return None
        return ((year - 1) * 112 + SEASONS.index(season) * 28 + day,
                clock if type(clock) is int else None)
    except (TypeError, ValueError):
        return None


def row_stamp(row):
    if not isinstance(row, dict):
        return None
    for field in ('event_time', 'EventTime', 'game_time', 'game', 'day_label', 'date', 'Date'):
        if value := stamp(row.get(field), row.get('time', row.get('time_of_day'))):
            return value
    return None


def admits(when, checkpoint):
    end = stamp(checkpoint['date'], checkpoint['time'])
    return bool(when and (when[0] < end[0] or when[0] == end[0] and when[1] is not None and when[1] <= end[1]))


def native(folder, candidate):
    """Read the selected XML, never a last live clock or run-end date."""
    main = Path(folder) / candidate['save_folder']
    root = ET.fromstring(storage.read_bytes(main))
    if (root.tag != 'SaveGame' or str(root.findtext('uniqueIDForThisGame')) != str(candidate['save_id'])
            or str(root.findtext('player/UniqueMultiplayerID')) != str(candidate['host_id'])):
        raise ValueError('memory checkpoint native identity mismatch')
    peers = root.findall('./farmhands/Farmer') + root.findall('.//farmhand')
    if not any(p.findtext('UniqueMultiplayerID') == str(candidate['farmhand_id']) for p in peers):
        raise ValueError('memory checkpoint farmhand identity mismatch')
    date = [int(root.findtext('year')), root.findtext('currentSeason'), int(root.findtext('dayOfMonth'))]
    clock = int(root.findtext('timeOfDay', '600'))
    if not stamp(date, clock) or clock != 600:
        raise ValueError('memory checkpoint requires native saved morning')
    return dict(date=date, time=clock, save_sha256=storage.digest_file(main))


def rows(path):
    if not storage.exists(path):
        return
    for line in storage.read_bytes(path).decode('utf-8-sig').splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                yield row
        except ValueError:
            continue  # closed journals can have an interrupted final append


def discoveries(doc, checkpoint):
    events = [r for r in doc.get('Events', []) if admits(row_stamp(r), checkpoint)]
    # Undated mutable access/flag caches are re-observed by the mod. Seen is not
    # independent evidence: otherwise a discarded floor never emits again.
    return dict(Events=events, Seen=[r['id'] for r in events], Accessible={}, Connections={},
                Flags=[], FlagsInitialized=False, ObservationTime=None, ObservationPlace=None)


def persona(run, source, checkpoint):
    # Rebuild counts and dedup as well as the visible tail. A compact count/Seen
    # without the dated event cannot establish which timeline it belongs to.
    events = {}
    history = source / 'persona/discoveries.json'
    if storage.exists(history):
        doc = json.loads(storage.read_bytes(history))
        for row in doc.get('recent', []):
            if admits(row_stamp(row), checkpoint):
                events[row['id']] = row
    for row in rows(Path(run) / 'bridge/bridge-transcript.jsonl'):
        event = row.get('event')
        if row.get('ev') == 'discovery_context' and admits(row_stamp(event), checkpoint):
            events[event['id']] = event
    counts = {}
    for row in events.values():
        tier = row['importance']
        counts[tier] = counts.get(tier, 0) + row['count']
    return dict(recent=list(events.values())[-8:], seen=sorted(events), counts=counts)


def snapshot_persona(run, history, checkpoint):
    """Keep the valid start's complete dedup/counts; admit new transcript events by native clock."""
    doc = json.loads(storage.read_bytes(history)) if history and storage.exists(history) else dict(counts={}, recent=[], seen=[])
    counts, recent, seen = dict(doc['counts']), list(doc['recent']), set(doc['seen'])
    for row in rows(Path(run) / 'bridge/bridge-transcript.jsonl'):
        event = row.get('event')
        if (row.get('ev') == 'discovery_context' and admits(row_stamp(event), checkpoint)
                and event['id'] not in seen):
            seen.add(event['id'])
            tier = event['importance']
            counts[tier] = counts.get(tier, 0) + event['count']
            recent.append(event)
    return dict(counts=counts, recent=recent[-8:], seen=sorted(seen))


def seal_snapshot(folder, checkpoint, *, strict=False):
    """The save seam permits no new actions, but automatic observations may arrive
    after 06:00 while the harness verifies files. Exclude explicitly later rows
    even from that snapshot. Future *plan/calendar references* are not lived dates.
    Undated files are supported because their full version is captured at the seam.
    """
    from scripts.free_play import farm_memory as F
    end = stamp(checkpoint['date'], checkpoint['time'])

    def later(when):
        if strict:
            return bool(when and not admits(when, checkpoint))
        return bool(when and (when[0] > end[0] or when[0] == end[0] and when[1] is not None and when[1] > end[1]))

    def invalid(row):
        if not isinstance(row, dict):
            return False
        if later(row_stamp(row)):
            return True
        # Native day ordinals describe authorship/mutation, unlike for_day,
        # referenced_days and calendar dates, which can intentionally be future.
        for key in ('observed_day', 'status_day', 'revised_on_day', 'capped_on_day', 'expired_on_day', 'authored_on_day'):
            if type(row.get(key)) is int and row[key] > end[0]:
                return True
        for key in ('planted_date', 'watered_date', 'last_seen_date', 'walk_date', 'ready_date', 'dead_date'):
            if later(stamp(row.get(key), row.get(key.replace('_date', '_time')))):
                return True
        evidence = row.get('evidence')
        if isinstance(evidence, list) and any(isinstance(e, list) and e and type(e[0]) is int and e[0] > end[0] for e in evidence):
            return True
        return False

    def tree(value, *, dated_leaves=False):
        def keep(v):
            return not invalid(v) and not (dated_leaves and later(stamp(v)))
        if isinstance(value, dict):
            return {k: tree(v, dated_leaves=dated_leaves) for k, v in value.items() if keep(v)}
        if isinstance(value, list):
            return [tree(v, dated_leaves=dated_leaves) for v in value if keep(v)]
        return value

    for path in storage.files(folder):
        if path.suffix == '.jsonl':
            original = list(rows(path))
            kept = [tree(r) for r in original if not invalid(r)]
            if kept != original:
                path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in kept), encoding='utf-8')
        elif path.name == 'cycle.json':
            original = F.read(path)
            kept = tree(original, dated_leaves=True)
            if kept != original:
                F.write(path, kept)
        elif path.name.startswith('discoveries-'):
            doc = F.read(path)
            events = doc.get('Events', [])
            discarded = [r for r in events if invalid(r)]
            if discarded:
                rejected = {r['id'] for r in discarded}
                doc = dict(doc, Events=[r for r in events if r['id'] not in rejected],
                           Seen=[i for i in doc.get('Seen', []) if i not in rejected],
                           Accessible={}, Connections={}, Flags=[], FlagsInitialized=False,
                           ObservationTime=None, ObservationPlace=None)
                F.write(path, doc)
        elif path.suffix == '.json' and path.relative_to(folder).parts[0] in ('mind', 'state'):
            try:
                original = F.read(path)
            except (ValueError, UnicodeError):
                continue  # a future opaque format is still covered by the full seam snapshot
            kept = {} if invalid(original) else tree(original)
            if kept != original:
                F.write(path, kept)


def jsonl_history(path, checkpoint):
    """Recover dated append history. Mutable ledger facts require a saved snapshot."""
    limit = stamp(checkpoint['date'])[0]
    for row in rows(path):
        if (path.name == 'prefs.jsonl' and row.get('source') in ('owner', 'soul')
                and (row_stamp(row) is None or admits(row_stamp(row), checkpoint))):
            yield row
            continue
        if path.name == 'goals.jsonl':
            authored = row.get('authored_on_day', row.get('set_on_day'))
            if 'authored_on_day' not in row and row.get('why_set') == 'reflection' and type(authored) is int:
                authored -= 1
            revisions = [row.get(k) for k in ('revised_on_day', 'capped_on_day', 'expired_on_day')]
            revisions += [e[0] for e in row.get('evidence', []) if isinstance(e, list) and e]
            # A compacted post-save revision cannot be undone without the old
            # row. Do not invent an earlier goal text/status or renew its TTL.
            carried_after = type(row.get('for_day')) is int and row['for_day'] > limit
            if (type(authored) is int and authored < limit and not carried_after
                    and not (row_stamp(row) and not admits(row_stamp(row), checkpoint))
                    and all(type(d) is int and d < limit for d in revisions if d is not None)):
                yield row
        elif path.name != 'ledger.jsonl' and admits(row_stamp(row), checkpoint):
            yield row


def cut_legacy(run, source, target, candidate, checkpoint):
    from scripts.free_play import farm_memory as F
    hints, sources = F.cycle_memory(run, candidate, checkpoint=checkpoint)
    F.write(target / 'cycle.json', hints)
    name = F.discovery_name(candidate)
    path = source / 'discoveries' / name
    if storage.exists(path):
        F.write(target / 'discoveries' / name, discoveries(F.read(path), checkpoint))
    F.write(target / 'persona/discoveries.json', persona(run, source, checkpoint))
    missing = ['cycle/' + name for name in F.HINTS if name not in hints]
    for folder in ('mind', 'state'):
        for path in storage.files(source / folder):
            relative = path.relative_to(source)
            if path.suffix == '.jsonl':
                kept = list(jsonl_history(path, checkpoint))
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in kept), encoding='utf-8')
            # Whole-file snapshots and undated/upserted state have no safe
            # historical inverse. Keep the full end file only in the archive.
            missing.append(str(relative.as_posix()) + ': legacy checkpoint coverage incomplete')
    for name in F.native_names(candidate):
        path = source / 'native' / name
        if name.startswith('nights.') and storage.exists(path):
            nights = F.read(path)
            if isinstance(nights, list):
                # Current-date pending night can already contain an unsaved KO,
                # sleep intent, plan, etc. Drop it; NightLedger opens a fresh one.
                kept = [r for r in nights if r.get('state') == 'committed' and admits(stamp(r.get('date')), checkpoint)]
                # ADR 152: NightLedger requires committed* + exactly one pending
                # tail. A committed-only cut prevents all occurrence polling.
                F.write(target / 'native' / name, night_start(kept, candidate, checkpoint))
        else:
            # Occurrences carry wall/frame/sequence, not a native game date.
            missing.append('native/' + name + ': no checkpoint snapshot')
    seal_snapshot(target, checkpoint, strict=True)
    return missing, sources


def night_start(committed, candidate, checkpoint):
    """Keep proved earlier nights and open an empty night at the chosen save.

    Sequence numbers are ledger-local; never renumber earlier evidence or carry
    an unsaved intent/collapse/plan from the rewound current day.
    """
    boundary = stamp(checkpoint['date'])[0]
    prefix = f"{candidate['save_id']}|{candidate['farmhand_id']}|"
    previous_seq, previous_day = -1, -1
    for row in committed:
        when = stamp(row.get('date'))
        seq = row.get('seq')
        if (type(seq) is not int or seq <= previous_seq or not when
                or not previous_day < when[0] < boundary or row.get('state') != 'committed'
                or row.get('night_id') != prefix + '-'.join(map(str, (row['date'][k] for k in ('year', 'season', 'day'))))):
            raise ValueError('invalid continuation night history; source preserved')
        previous_seq, previous_day = seq, when[0]
    year, season, day = checkpoint['date']
    pending = dict(night_id=f'{prefix}{year}-{season}-{day}', seq=previous_seq + 1,
                   date=dict(year=year, season=season, day=day), state='pending', opened_by='restart',
                   intent=None, intent_cleared_by=None, collapse=None, day_events=[], plan=None,
                   save='unknown', day_started_frame=None, cause_hint=None)
    return committed[-16:] + [pending]
