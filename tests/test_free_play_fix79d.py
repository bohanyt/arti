"""Evaluator/harness regressions only; no game or provider calls (ADR 142)."""
import json

import pytest

from scripts.free_play.evaluate import Evidence
from scripts.free_play.goal_gate import native_progress
from tests.test_free_play_evaluate import journal


def test_verified_actual_shipments_are_native_receipts_not_estimated_gold(tmp_path):
    receipt = dict(ev='finish_verified', goal_id='ship-goal', verdict=dict(
        status='achieved', reason='observed_ship', observed=dict(actual_shipped=[
            dict(item='(O)390', quantity=1, request_id='one'),
            dict(item='(O)390', quantity=8, request_id='stack')])))
    snapshots = [dict(ev='progress_snapshot', progress=dict(stats=dict(individual_money_earned=n)))
                 for n in (30, 819)]
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, [snapshots[0], receipt, receipt, snapshots[1]])], '')
    assert p['own_shipments']['value'] == 2  # transfers, not five skill finishes
    assert p['own_sales_gold']['value'] == 0
    assert p['own_money_earned']['value'] == 789
    from scripts.free_play.goal_gate import goal_gate
    assert goal_gate(dict(active_minutes=None, active_basis='test'), p,
                     dict(value=None))['criteria']['own_money_earned']['status'] == 'MET'
    receipt['verdict']['status'] = 'uncertain'
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, [snapshots[0], receipt, snapshots[1]])], '')
    assert p['own_shipments']['value'] == 0
    assert goal_gate(dict(active_minutes=None, active_basis='test'), p,
                     dict(value=None))['criteria']['own_money_earned']['status'] == 'UNKNOWN'


def test_ship_mirrors_partial_receipts_and_malformed_entries(tmp_path):
    r = dict(ev='finish_verified', verdict=dict(status='partial', reason='observed_ship_partial',
        observed=dict(actual_shipped=[dict(item='(O)24', quantity=3, request_id='r'),
            dict(item='(O)24', quantity=0, request_id='zero'),
            dict(item='(O)24', quantity=3)])))
    mirror = dict(ev='native_transaction', state='completed', source='generic_ui_native_readback',
        kind='ship', request_id='r', item='(O)24', quantity=3, inventory_delta=-3, shipping_delta=3, money_delta=0)
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, [r, mirror])], '')
    assert p['own_shipments']['value'] == 1


def test_session_metrics_use_clock_minutes_location_time_and_native_social(tmp_path):
    from scripts.free_play.session_metrics import session_metrics
    rows = [dict(ev='skill_session_started', at=100, game=dict(date=[1, 'spring', 5], time=600, location='Cabin')),
            dict(ev='progress_snapshot', progress=dict(friends={'Leah': 240})),
            dict(ev='skill_model_decision', at=110, choice=['ngobrol_hadiah', None]),
            dict(ev='objective_progress', at=120, goal_id='talk', pinned=dict(kind='talk_npc')),
            dict(ev='state', at=130, game=dict(date=[1, 'spring', 5], time=700, location='Town')),
            dict(ev='objective_progress', at=140, goal_id='talk', kind='talk_npc', dialogue_closed=True),
            dict(ev='skill_model_decision', at=150, choice='invalid'),
            dict(ev='skill_model_decision', at=160, choice=['jalan_ke', None]),
            dict(ev='progress_snapshot', progress=dict(friends={'Leah': 270})),
            dict(ev='state', at=190, game=dict(date=[1, 'spring', 5], time=800, location='Farm'))]
    js = [journal(tmp_path, rows)]
    log = js[0][0].parent.parent / 'stdout.txt'
    log.write_text('\n'.join('[StardewProvider] ' + json.dumps(r) for r in [
        dict(ev='provider_phase', phase='logical_wait_start', generation_id='g', lane='background', monotonic_ns=0),
        dict(ev='provider_phase', phase='physical_worker_complete', generation_id='g', lane='background', monotonic_ns=10_000_000_000),
        dict(ev='provider_phase', phase='logical_timeout', generation_id='g', lane='background', monotonic_ns=5_000_000_000)]))
    end = dict(active_minutes=2, active_intervals=[[100, 220]])
    m = session_metrics(Evidence(tmp_path), js, end)
    assert m['tempo']['seconds_per_decision']['value'] == 60
    assert m['tempo']['game_minutes_per_decision']['value'] == 60
    assert m['tempo']['decisions_per_game_day']['value'] == 2
    assert m['tempo']['model_wait_share']['value'] == pytest.approx(5 / 120, abs=0.0001)
    assert m['social']['talks_started_per_game_day']['value'] == 1
    assert m['social']['talks_finished_per_game_day']['value'] == 1
    assert m['social']['gifts_finished_per_game_day']['value'] == 0
    assert m['social']['friendship_points_delta']['value'] == 30
    assert m['social']['hearts_gained']['value'] == 1
    assert m['variety']['off_farm_cabin_share']['value'] == .5


def test_optional_metrics_gate_requires_explicit_enable_and_config(tmp_path):
    from scripts.free_play.goal_gate import goal_gate
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, [])], '')
    args = (dict(active_minutes=None, active_basis='test'), p, dict(value=None))
    metrics = dict(tempo=dict(seconds_per_decision=dict(value=20, evidence=[], missing_signal=None)))
    config = dict(enabled=False, thresholds={'tempo.seconds_per_decision': {'max': 10}})
    assert goal_gate(*args, metrics=metrics, metrics_config=config)['status'] == 'UNKNOWN'
    config['enabled'] = True
    gate = goal_gate(*args, metrics=metrics, metrics_config=config)
    assert gate['status'] == 'NOT_MET'
    assert gate['measured_criteria']['criteria']['tempo.seconds_per_decision']['status'] == 'NOT_MET'
    with pytest.raises(ValueError):
        goal_gate(*args, metrics=metrics, metrics_config=dict(enabled=True, thresholds={}))


def save_fixture(tmp_path):
    run = tmp_path / 'prior'; run.mkdir()
    folder = 'farm_42'; save = run / 'preserved-saves' / folder; save.mkdir(parents=True)
    (save / folder).write_text('''<SaveGame><uniqueIDForThisGame>42</uniqueIDForThisGame><whichFarm>0</whichFarm>
        <year>1</year><currentSeason>summer</currentSeason><dayOfMonth>4</dayOfMonth><timeOfDay>600</timeOfDay>
        <player><name>Host</name><UniqueMultiplayerID>1</UniqueMultiplayerID></player>
        <farmhands><Farmer><name>Arti</name><UniqueMultiplayerID>2</UniqueMultiplayerID><money>819</money></Farmer></farmhands></SaveGame>''')
    (save / 'SaveGameInfo').write_text('<Farmer><name>Host</name><UniqueMultiplayerID>1</UniqueMultiplayerID></Farmer>')
    (run / 'STAGING.json').write_text(json.dumps(dict(save_folder=folder, save_id=42, host_id=1, farmhand_id=2, farmhand_name='Arti')))
    (run / 'restore.json').write_text(json.dumps(dict(saves_identical=True, files_identical=True)))
    return run, save


def test_carryover_packed_start_is_identical_and_baseline_restores(tmp_path):
    from scripts.free_play import storage
    from scripts.free_play.carryover import stage_continuation, retain_candidate
    from scripts.free_play.soak import Baseline, manifest
    prior, source = save_fixture(tmp_path)
    before = storage.hashes(source)
    storage.compact(prior / 'preserved-saves', prior)
    output = tmp_path / 'new'; output.mkdir()
    saves = tmp_path / 'Saves'; saves.mkdir()
    (saves / 'original').write_bytes(b'owner baseline')
    baseline = Baseline(output, {}, saves, owned_saves=(source.name,)); baseline.capture()
    staged = stage_continuation(prior, saves, 'Standard', output, baseline)
    assert manifest(saves / staged['save_folder']) == before
    assert storage.hashes(output / 'continuation-start' / source.name) == before
    assert (saves / 'original').read_bytes() == b'owner baseline'
    (saves / source.name / 'SaveGameInfo_old').write_bytes(b'end old info')
    baseline.restore()
    assert baseline.saves_manifest() == baseline.record['saves']
    assert (saves / 'original').read_bytes() == b'owner baseline'
    result = retain_candidate(output)
    assert result['available'] is True
    assert storage.hashes(output / 'preserved-saves' / source.name)['SaveGameInfo_old']


def test_carryover_rejects_incomplete_or_wrong_identity_before_copy(tmp_path):
    from scripts.free_play.carryover import inspect_candidate
    prior, source = save_fixture(tmp_path)
    assert inspect_candidate(prior, 'Standard')['farmhand_id'] == 2
    (source / 'SaveGameInfo').unlink()
    with pytest.raises(ValueError, match='complete'):
        inspect_candidate(prior, 'Standard')
    (source / 'SaveGameInfo').write_text('<Farmer><UniqueMultiplayerID>999</UniqueMultiplayerID></Farmer>')
    with pytest.raises(ValueError, match='HOST'):
        inspect_candidate(prior, 'Standard')


def test_carryover_collision_preserves_both_baseline_and_chosen_start(tmp_path):
    from scripts.free_play import storage
    from scripts.free_play.carryover import stage_continuation
    from scripts.free_play.soak import Baseline, manifest
    prior, source = save_fixture(tmp_path)
    output = tmp_path / 'new'; output.mkdir()
    saves = tmp_path / 'Saves'; collision = saves / source.name; collision.mkdir(parents=True)
    (collision / 'owner-save').write_bytes(b'original baseline')
    baseline = Baseline(output, {}, saves, owned_saves=(source.name,)); baseline.capture()
    stage_continuation(prior, saves, 'Standard', output, baseline)
    assert (output / 'continuation-displaced' / source.name / 'owner-save').read_bytes() == b'original baseline'
    assert manifest(saves / source.name) == storage.hashes(source)
    baseline.restore()
    assert manifest(saves) == baseline.record['saves']


def test_native_gifts_and_absent_metrics_do_not_gain_credit(tmp_path):
    from scripts.free_play.session_metrics import session_metrics
    rows = [dict(ev='skill_session_started', game=dict(date=[1, 'spring', 5], time=600)),
            dict(ev='objective_progress', goal_id='g', pinned=dict(kind='gift', name='Leah')),
            dict(ev='extras_effect', kind='npc_gift', outcome='GiftGiven', world_effect_status='uncertain'),
            dict(ev='extras_effect', kind='npc_gift', outcome='GiftGiven', world_effect_status='observed_extras_effect'),
            dict(ev='skill_finished', skill='terima_hadiah', verdict='observed_gift_answered')]
    m = session_metrics(Evidence(tmp_path), [journal(tmp_path, rows)], {})
    assert m['social']['gifts_started_per_game_day']['value'] == 1
    assert m['social']['gifts_finished_per_game_day']['value'] == 1
    assert m['social']['friendship_points_delta']['value'] is None
    assert m['tempo']['model_wait_share']['value'] is None
    assert m['variety']['off_farm_cabin_share']['value'] is None


def test_criteria_bounds_reject_unknown_fields_and_nonfinite_targets():
    from scripts.free_play.session_metrics import configured_criteria
    with pytest.raises(ValueError):
        configured_criteria({}, [])
    for thresholds in ({'tempo.typo': {'max': 20}}, {'tempo.model_wait_share': {'max': float('nan')}},
                       {'tempo.model_wait_share': {'min': 1, 'max': 0}}):
        with pytest.raises(ValueError):
            configured_criteria({}, dict(enabled=True, thresholds=thresholds))
    field = dict(value=.2, evidence=[], missing_signal=None, lower_bound=True)
    config = dict(enabled=True, thresholds={'variety.off_farm_cabin_share': {'min': .5}})
    assert configured_criteria(dict(variety=dict(off_farm_cabin_share=field)), config)['criteria']['variety.off_farm_cabin_share']['status'] == 'UNKNOWN'


def test_new_metrics_render_at_top_and_older_scoreboards_stay_absent(tmp_path):
    from scripts.free_play.evaluate import evaluate, markdown
    from scripts.free_play.scoreboard import write_scoreboard
    from tests.test_free_play_evaluate import fixture_run
    result = evaluate(fixture_run(tmp_path / 'fixture'))
    report = markdown(result)
    assert report.index('tempo.seconds_per_decision') < report.index('## Identity')
    assert result['goal_gate']['measured_criteria']['enabled'] is False
    result.pop('session_metrics')
    output = tmp_path / 'scoreboard.md'
    write_scoreboard([result], output)
    text = output.read_text(encoding='utf-8')
    assert text.index('seconds_per_decision') < text.index('## Journey')
    assert 'absent' in text
