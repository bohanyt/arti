"""Save-relative memory, filesystem only: death and budget-stop continuations."""
import json
from pathlib import Path

import pytest

from scripts.free_play import carryover, farm_memory as F, memory_checkpoint as C, storage
from tests.test_free_play_fix79d import save_fixture


def journal(prior, candidate, days):
    cycle = prior / 'runtime/live7/c1'
    state = dict(save_id=candidate['save_id'], player_id=candidate['farmhand_id'], hints={}, days=[])
    for index, (date, rows) in enumerate(days, 1):
        attempt = f'day-{index:04d}-attempt-000'
        state['days'].append(dict(date=dict(zip(('year', 'season', 'day'), date)), attempts=[dict(id=attempt)]))
        path = cycle / attempt / 'journal/g1_journal.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    F.write(cycle / 'cycle_state.json', state)


def event(day, floor, time=1100):
    return dict(id=f'location_entered:UndergroundMine{floor}', kind='location_entered',
                subject=f'UndergroundMine{floor}', date=f'1:summer:{day}',
                event_time=f'1:summer:{day} {time}', seq=floor)


def test_legacy_death_after_save_cuts_native_persona_and_replays_older_chest(tmp_path):
    prior, save = save_fixture(tmp_path)  # chosen save: Summer 4, 06:00
    candidate = carryover.inspect_candidate(prior, 'Standard')
    before, rolled_back = event(3, 1), event(4, 3)
    F.write(prior / 'preserved-discoveries' / F.discovery_name(candidate),
            dict(Seen=[before['id'], rolled_back['id']], Events=[before, rolled_back],
                 Flags=['event:discarded'], Accessible={'Mine': True}, Connections={'later': True}))
    for row in (before, rolled_back):
        path = prior / 'bridge/bridge-transcript.jsonl'
        path.parent.mkdir(exist_ok=True)
        with path.open('a') as out:
            out.write(json.dumps(dict(ev='discovery_context', event=dict(row, importance='high', count=1,
                ts=1, situation=row['subject'], facts=dict(kind=row['kind'], subject=row['subject'])))) + '\n')
    chest = lambda n: dict(chests={'obj:Farm:1,2:(BC)130': dict(items={'(O)388': n}, date=[1, 'summer', 3 if n == 10 else 4], time=900)})
    journal(prior, candidate, [([1, 'summer', 3], [dict(ev='live7_chest_memory', memory=chest(10)),
        dict(ev='live7_catch_totals', totals={'Farm': {'junk': 2}}, kinds={'(O)167': 2})]),
        ([1, 'summer', 4], [dict(ev='skill_session_started', game=dict(date=[1, 'summer', 4], time=630)),
        dict(ev='live7_chest_memory', memory=chest(99)),
        dict(ev='live7_catch_totals', totals={'Farm': {'junk': 9}}, kinds={'(O)167': 9}),
        dict(ev='reconnected', game=dict(date=[1, 'summer', 4], time=600)),
        dict(ev='live7_catch_totals', totals={'Farm': {'junk': 9}}, kinds={'(O)167': 9})])])
    original = storage.digest_file(prior / 'preserved-discoveries' / F.discovery_name(candidate))
    runtime = F.prepare(tmp_path / 'next', candidate, tmp_path / 'mod', prior=prior)
    native = F.read(runtime['discovery'])
    assert native['Events'] == [before]
    assert native['Seen'] == [before['id']] and not native['Flags'] and not native['Accessible']
    active = Path(runtime['active'])
    hints = F.read(active / 'cycle.json')
    assert hints['chest_memory'] == chest(10)
    assert hints['catch_kinds'] == {'(O)167': 2}
    persona = F.read(active / 'persona/discoveries.json')
    assert persona['seen'] == [before['id']] and persona['counts'] == {'high': 1}
    assert rolled_back['subject'] not in json.dumps(persona)
    assert storage.digest_file(prior / 'preserved-discoveries' / F.discovery_name(candidate)) == original


@pytest.mark.parametrize('terminal', ['death', 'midday_stop', 'clean_budget_stop'])
def test_checkpoint_restores_all_stores_including_overwritten_goals_and_future_files(tmp_path, terminal):
    prior, save = save_fixture(tmp_path)
    candidate = carryover.inspect_candidate(prior, 'Standard')
    runtime = F.prepare(prior, candidate, tmp_path / 'mod', inbox=tmp_path / 'inbox')
    active = Path(runtime['active'])
    stores = ['mind/' + n for n in F.MIND_FILES] + ['state/' + n for n in F.STATE_FILES]
    stores += ['mind/stardew/future-store.json', 'state/future-store.bin', 'mind/custom/MANIFEST.json']
    for name in stores:
        path = active / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + ': saved history').encode())
    hints = {name: {'saved': name} for name in F.HINTS}
    F.write(active / 'cycle.json', hints)
    native = dict(Events=[event(3, 1)], Seen=[event(3, 1)['id']])
    F.write(runtime['discovery'], native)
    for name in F.native_names(candidate):
        F.write(Path(runtime['inbox']) / name, {'saved': name})
    F.capture_checkpoint(prior, candidate, save)
    expected = F.hashes(F.checkpoint_path(prior, candidate, candidate['hashes'][candidate['save_folder']]))
    if terminal != 'clean_budget_stop':
        for name in stores:
            (active / name).write_bytes(b'rolled back goal/friend/story/place/depth')
        F.write(active / 'cycle.json', {name: {'rolled_back': True} for name in F.HINTS})
        F.write(runtime['discovery'], dict(Events=[event(4, 3)], Seen=[event(4, 3)['id']]))
        for name in F.native_names(candidate):
            F.write(Path(runtime['inbox']) / name, {'rolled_back': True})
    F.preserve(prior, candidate)
    frozen = F.hashes(F.bundle_path(prior, candidate))
    next_run = tmp_path / 'next'
    next_runtime = F.prepare(next_run, candidate, tmp_path / 'next-mod', prior=prior, inbox=tmp_path / 'next-inbox')
    next_active = Path(next_runtime['active'])
    for name in stores:
        if name == 'mind/now.json':
            assert not (next_active / name).exists()
        else:
            assert (next_active / name).read_bytes() == (name + ': saved history').encode()
    assert F.read(next_active / 'cycle.json') == hints
    assert F.read(next_runtime['discovery']) == native
    for name in F.native_names(candidate):
        assert F.read(Path(next_runtime['inbox']) / name) == {'saved': name}
    assert F.hashes(F.bundle_path(prior, candidate)) == frozen
    assert F.read(next_run / 'memory-cut.json')['checkpoint']['date'] == [1, 'summer', 4]
    # A second continuation with no saved night keeps the same checkpoint-valid history.
    F.preserve(next_run, candidate)
    storage.materialize(prior / 'preserved-saves', next_run / 'preserved-saves')
    F.write(next_run / 'STAGING.json', F.read(prior / 'STAGING.json'))
    F.write(next_run / 'restore.json', dict(saves_identical=True, files_identical=True))
    third = F.prepare(tmp_path / 'third', candidate, tmp_path / 'third-mod', prior=next_run)
    assert F.read(Path(third['active']) / 'cycle.json') == hints
    assert expected


def test_checkpoint_corruption_fails_before_native_install(tmp_path):
    prior, save = save_fixture(tmp_path)
    candidate = carryover.inspect_candidate(prior, 'Standard')
    F.prepare(prior, candidate, tmp_path / 'mod')
    F.capture_checkpoint(prior, candidate, save)
    F.preserve(prior, candidate)
    folder = F.checkpoint_path(prior, candidate, candidate['hashes'][candidate['save_folder']])
    F.write(folder / 'cycle.json', {'corrupt': True})
    with pytest.raises(ValueError, match='hashes'):
        F.prepare(tmp_path / 'next', candidate, tmp_path / 'never-installed', prior=prior)
    assert not (tmp_path / 'never-installed').exists()


@pytest.mark.parametrize(('date', 'clock', 'allowed'), [
    ('1:summer:3 2600', None, True), ('1:summer:4 600', None, True),
    ('1:summer:4 610', None, False), ('Y1 summer 4 06:00', None, True),
    ('Y1 summer 4 06:10', None, False), ('1:summer:4', None, False),
    ('1:summer:4 UNKNOWN', None, False), ([1, 'summer', 4], 600, True),
    (dict(year=1, season='summer', day=4), 630, False),
    ('2:spring:1 600', None, False), ('garbage', None, False),
])
def test_native_date_time_boundary_is_inclusive_and_unknown_is_not_fact(date, clock, allowed):
    assert C.admits(C.stamp(date, clock), dict(date=[1, 'summer', 4], time=600)) is allowed


def test_end_only_legacy_mind_all_kinds_and_pending_ko_are_never_imported_as_facts(tmp_path):
    prior, _ = save_fixture(tmp_path)
    candidate = carryover.inspect_candidate(prior, 'Standard')
    runtime = F.prepare(prior, candidate, tmp_path / 'mod', inbox=tmp_path / 'inbox')
    active = Path(runtime['active'])
    for name in ('discoveries.jsonl', 'journey.jsonl', 'likes.jsonl', 'progress.jsonl'):
        path = active / 'mind/stardew' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(id='saved', date='Y1 summer 3', facts=dict(depth=1, friends=['Linus']))) + '\n' +
                        json.dumps(dict(id='discarded', date='Y1 summer 4', event_time='1:summer:4 1200',
                                        facts=dict(depth=3, place='Mine', friends=['Abigail']))) + '\n')
    (active / 'mind/stardew/goals.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        dict(id='saved', set_on_day=31, status='open', evidence=[[31, 'saved', 'kept']]),
        dict(id='new_discarded', set_on_day=32, status='open'),
        dict(id='revised_discarded', set_on_day=1, revised_on_day=32, status='done'),
        dict(id='evidence_discarded', set_on_day=1, status='open', evidence=[[32, 'discarded', 'floor 3']]),
        dict(id='carry_discarded', set_on_day=1, status='open', for_day=33, carried_days=20),
        dict(id='tomorrow_goal_saved', set_on_day=32, why_set='reflection', status='open'),
    ]) + '\n')
    # Undated latest facts cannot be inverted, even if first/status day is old.
    for name in ('mind/stardew/ledger.jsonl', 'state/self.jsonl', 'state/wants.jsonl'):
        path = active / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(id='discarded', first_day=1, status_day=1,
                                        facts=dict(native_story='discarded KO', friends=['Abigail']))) + '\n')
    F.write(active / 'mind/stardew/future.json', dict(discarded='unknown future store'))
    for name in F.native_names(candidate):
        if name.startswith('nights.'):
            F.write(Path(runtime['inbox']) / name, [
                dict(C.night_start([], candidate, dict(date=[1, 'summer', 3]))[0], state='committed'),
                dict(state='pending', date=dict(year=1, season='summer', day=4), collapse='ko')])
        else:
            F.write(Path(runtime['inbox']) / name, dict(records=[dict(kind='ko_items_lost', wall_ms=1)]))
    F.preserve(prior, candidate)  # old end-only bundle, deliberately no checkpoint
    new = F.prepare(tmp_path / 'next', dict(candidate, saved_date=[9, 'winter', 28]), tmp_path / 'new-mod',
                    prior=prior, inbox=tmp_path / 'new-inbox')
    for name in ('discoveries.jsonl', 'journey.jsonl', 'likes.jsonl', 'progress.jsonl'):
        rows = list(C.rows(Path(new['mind']) / 'stardew' / name))
        assert [r['id'] for r in rows] == ['saved']
    goals = list(C.rows(Path(new['mind']) / 'stardew/goals.jsonl'))
    assert [r['id'] for r in goals] == ['saved', 'tomorrow_goal_saved']
    for name in ('mind/stardew/ledger.jsonl', 'state/self.jsonl', 'state/wants.jsonl'):
        assert list(C.rows(Path(new['active']) / name)) == []
    assert not Path(new['mind'], 'stardew/future.json').exists()
    for name in F.native_names(candidate):
        path = Path(new['inbox']) / name
        if name.startswith('nights.'):
            nights = F.read(path)
            assert [r['state'] for r in nights] == ['committed', 'pending']
            assert [r['date']['day'] for r in nights] == [3, 4]
            assert nights[-1]['collapse'] is None and nights[-1]['seq'] == 1
        else:
            assert not path.exists()
    cut = F.read(tmp_path / 'next/memory-cut.json')
    assert cut['checkpoint']['date'] == [1, 'summer', 4]  # XML, not supplied metadata
    assert any('ledger.jsonl' in n for n in new['missing'])


def test_snapshot_for_another_save_hash_is_not_selected(tmp_path):
    prior, save = save_fixture(tmp_path)
    candidate = carryover.inspect_candidate(prior, 'Standard')
    runtime = F.prepare(prior, candidate, tmp_path / 'mod')
    F.write(Path(runtime['mind']) / 'unsafe.json', dict(should_not_carry=True))
    F.capture_checkpoint(prior, candidate, save)
    F.preserve(prior, candidate)
    main = save / candidate['save_folder']
    main.write_text(main.read_text().replace('<dayOfMonth>4</dayOfMonth>', '<dayOfMonth>3</dayOfMonth>'))
    runtime = F.prepare(tmp_path / 'next', candidate, tmp_path / 'new-mod', prior=prior)
    assert not Path(runtime['mind'], 'unsafe.json').exists()
    assert F.read(tmp_path / 'next/memory-cut.json')['mode'] == 'dated_legacy_recovery'


def test_save_seam_excludes_later_automatic_observations_and_full_persona_counts(tmp_path):
    prior, save = save_fixture(tmp_path)
    candidate = carryover.inspect_candidate(prior, 'Standard')
    runtime = F.prepare(prior, candidate, tmp_path / 'mod')
    active = Path(runtime['active'])
    saved, late = event(3, 1), event(4, 3, 610)
    F.write(runtime['discovery'], dict(Events=[saved, late], Seen=[saved['id'], late['id']]))
    for folder in ('mind/stardew', 'state'):
        path = active / folder / 'future.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved) + '\n' + json.dumps(late) + '\n')
    F.write(active / 'mind/extra.json', late)
    plans = dict(id='kept-plan', authored_on_day=31, set_on_day=32, for_day=32,
                 referenced_days=[60], event_days=[60], text='future plan')
    calendar = dict(id='calendar', first_day=31, status_day=31, facts=dict(dates=['Y1 summer 10']))
    (active / 'mind/stardew/goals.jsonl').write_text(json.dumps(plans) + '\n')
    (active / 'mind/stardew/ledger.jsonl').write_text(json.dumps(calendar) + '\n')
    F.write(active / 'cycle.json', dict(chest_memory=dict(chests={
        'saved': dict(date=[1, 'summer', 3], time=2200, items={'wood': 10}),
        'late': dict(date=[1, 'summer', 4], time=610, items={'wood': 99})})))
    transcript = prior / 'bridge/bridge-transcript.jsonl'
    transcript.parent.mkdir()
    # More than the compact context's 8 entries: filtering its tail alone would
    # leave invalid IDs/counts that suppress new real discoveries after reload.
    events = [dict(saved, importance='low', count=1, situation='saved')]
    events += [dict(late, id=f'late-{n}', importance='high', count=1, situation='late') for n in range(20)]
    transcript.write_text(''.join(json.dumps(dict(ev='discovery_context', event=e)) + '\n' for e in events))
    F.capture_checkpoint(prior, candidate, save)
    F.preserve(prior, candidate)
    new = F.prepare(tmp_path / 'next', candidate, tmp_path / 'new-mod', prior=prior)
    assert F.read(new['discovery'])['Events'] == [saved]
    assert F.read(Path(new['active']) / 'persona/discoveries.json') == dict(
        counts={'low': 1}, recent=[events[0]], seen=[saved['id']])
    for folder in ('mind/stardew', 'state'):
        assert list(C.rows(Path(new['active']) / folder / 'future.jsonl')) == [saved]
    assert F.read(Path(new['mind']) / 'extra.json') == {}
    assert list(C.rows(Path(new['mind']) / 'stardew/goals.jsonl')) == [plans]
    assert list(C.rows(Path(new['mind']) / 'stardew/ledger.jsonl')) == [calendar]
    assert list(F.read(Path(new['active']) / 'cycle.json')['chest_memory']['chests']) == ['saved']


def test_harness_freezes_only_after_verified_save_and_before_next_day():
    # Source order is the invariant: the real harness cannot be executed here
    # without a game. Pure file/consumer behavior is covered separately above.
    from scripts.free_play import soak
    import inspect
    source = inspect.getsource(soak)
    verified = source.index('saved_day_outcome(saved)')
    compacted = source.index('storage.compact_day(run, cycle, saved)', verified)
    frozen = source.index("farm_memory.capture_checkpoint(run, fixture, Path(saved['checkpoint']) / 'save')", compacted)
    assert verified < compacted < frozen < source.index('days += 1', frozen)
