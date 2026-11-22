"""Host-first setup ordering; no game, network or real child is started."""
from types import SimpleNamespace

import pytest

from scripts import stardew_trial_controller as controller
from scripts.stardew_trial_watchdog import OperationLease


@pytest.fixture
def staged(tmp_path, monkeypatch):
    lease = OperationLease(run_id='fresh', controller={'pid': 1}, watchdog={'pid': 2},
        containment_id='job', started=100, deadline=600)
    cfg = dict(game_launch_order='host_first', game_dir=str(tmp_path),
        host=['vanilla'], farmhand=['smapi'], operation_ids={
            'ui01-launch-title': 'title', 'ui02-host-load': 'load',
            'ui03-farmhand-join': 'join'})
    spawned = []
    def popen(command, **kwargs):
        spawned.append((command, kwargs))
        return SimpleNamespace(pid=10 + len(spawned))
    monkeypatch.setattr(controller.subprocess, 'Popen', popen)
    monkeypatch.setattr(controller.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)
    monkeypatch.setattr(controller, 'process_identity', lambda pid: {'pid': pid})
    processes = {}
    def activate(op, kind='ui_setup', now=110):
        value = dict(**lease.binding, operation_id=op, kind=kind,
            started_monotonic=now, deadline_monotonic=now + lease.CAPS[kind])
        lease.check(value, now=now, pulse=now)
        controller.write(tmp_path, 'watchdog-operation.json',
            dict(run_id='fresh', operation_id=op))
    def tick(now=111):
        controller.launch_games(tmp_path, cfg, processes, lease,
            pulse=lease.last_pulse, clock=lambda: now)
    return SimpleNamespace(folder=tmp_path, cfg=cfg, lease=lease, spawned=spawned,
        processes=processes, activate=activate, tick=tick)


def host(s):
    s.activate('title')
    (s.folder / 'LAUNCH_GAMES').touch()
    s.tick()


def join_request(s):
    s.lease.complete('title', 'success', now=112)
    s.activate('load', now=113)
    s.lease.complete('load', 'success', now=114)
    controller.write(s.folder, 'operation-complete.json',
        dict(**s.lease.binding, operation_id='load', outcome='success'))
    s.activate('join', now=115)
    controller.write(s.folder, 'START_FARMHAND',
        dict(**s.lease.binding, operation_id='join', requested_monotonic=115))


def test_host_is_registered_before_farmhand_can_exist(staged):
    host(staged)
    assert [v[0] for v in staged.spawned] == [['vanilla']]
    assert controller.read_json(staged.folder / 'owned-children.json') == {'host': 11}
    staged.tick()
    assert len(staged.spawned) == 1
    join_request(staged)
    staged.tick(now=116)
    assert [v[0] for v in staged.spawned] == [['vanilla'], ['smapi']]
    assert controller.read_json(staged.folder / 'owned-children.json') == {'host': 11, 'farmhand': 12}
    staged.tick(now=117)
    assert len(staged.spawned) == 2


@pytest.mark.parametrize('fault', ['early', 'wrong_operation', 'missing_completion',
    'failed_completion', 'wrong_completion_identity', 'wrong_request_identity',
    'wrong_ack', 'stale_request', 'expired', 'stop', 'watchdog_changed'])
def test_invalid_farmhand_admission_never_starts_a_second_child(staged, fault, monkeypatch):
    host(staged)
    join_request(staged)
    now = 116
    if fault == 'early': staged.processes.clear()
    elif fault == 'wrong_operation': staged.cfg['operation_ids']['ui03-farmhand-join'] = 'other'
    elif fault == 'missing_completion': (staged.folder / 'operation-complete.json').unlink()
    elif fault in ('failed_completion', 'wrong_completion_identity'):
        data = controller.read_json(staged.folder / 'operation-complete.json')
        data['outcome' if fault == 'failed_completion' else 'run_id'] = 'wrong'
        controller.write(staged.folder, 'operation-complete.json', data)
    elif fault == 'wrong_request_identity':
        data = controller.read_json(staged.folder / 'START_FARMHAND'); data['containment_id'] = 'other'
        controller.write(staged.folder, 'START_FARMHAND', data)
    elif fault == 'wrong_ack':
        controller.write(staged.folder, 'watchdog-operation.json', dict(run_id='fresh', operation_id='load'))
    elif fault == 'stale_request': now = 121
    elif fault == 'expired': now = 235
    elif fault == 'stop': (staged.folder / 'STOP').touch()
    else: monkeypatch.setattr(controller, 'process_identity', lambda pid: {'pid': 999})
    with pytest.raises((ValueError, FileNotFoundError)):
        staged.tick(now=now)
    assert len(staged.spawned) == 1


def test_host_launch_requires_the_exact_live_title_lease(staged):
    staged.activate('wrong')
    (staged.folder / 'LAUNCH_GAMES').touch()
    with pytest.raises(ValueError): staged.tick()
    assert not staged.spawned


def test_legacy_manifest_still_launches_both(staged):
    staged.cfg.pop('game_launch_order')
    (staged.folder / 'LAUNCH_GAMES').touch()
    staged.tick()
    assert [v[0] for v in staged.spawned] == [['vanilla'], ['smapi']]


def test_unknown_launch_mode_is_rejected_before_spawn(staged):
    staged.cfg['game_launch_order'] = 'typo'
    (staged.folder / 'LAUNCH_GAMES').touch()
    with pytest.raises(ValueError): staged.tick()
    assert not staged.spawned


def test_log_open_delay_cannot_launch_after_lease_expiry(staged):
    staged.activate('title')
    (staged.folder / 'LAUNCH_GAMES').touch()
    times = iter([111, 230])
    with pytest.raises(ValueError, match='operation_expired'):
        controller.launch_games(staged.folder, staged.cfg, staged.processes,
            staged.lease, pulse=110, clock=lambda: next(times))
    assert not staged.spawned


def test_operator_join_publishes_only_after_matching_admission(tmp_path, monkeypatch):
    from scripts import stardew_m1_operator as op
    op.configure(tmp_path)
    binding = dict(run_id='fresh', controller={'pid':1}, watchdog={'pid':2}, containment_id='job')
    op.once('namespace.json', dict(operation_ids={'ui02-host-load':'load','ui03-farmhand-join':'join'}))
    op.once('plan-manifest.json', dict(game_launch_order='host_first'))
    op.once('operation-complete.json', dict(**binding,operation_id='load',outcome='success'))
    op.once('owned-children.json', {'host':11})
    op.once('watchdog-operation.json', dict(run_id='fresh',operation_id=None))
    monkeypatch.setattr(op,'binding',lambda:binding)
    monkeypatch.setattr(op.time,'monotonic',lambda:116)
    seen=[]
    def accepted(fn, end):
        assert not (tmp_path/'START_FARMHAND').exists()
        request=op.read(tmp_path/'operation-request.json')
        assert request['operation_id']=='join'
        controller.write(tmp_path,'operation-status.json',dict(**binding,operation_id='join',
            outcome='active',operation={'deadline_monotonic':235}))
        controller.write(tmp_path,'watchdog-operation.json',dict(run_id='fresh',operation_id='join'))
        return fn()
    monkeypatch.setattr(op,'waitfor',accepted)
    monkeypatch.setattr(op,'register_games',lambda expected,end:seen.append((expected,end)))
    op.admit('ui03-farmhand-join')
    assert op.read(tmp_path/'START_FARMHAND')==dict(**binding,operation_id='join',requested_monotonic=116)
    assert seen==[({'host','farmhand'},124)]
    with pytest.raises(ValueError, match='farmhand_request_reused'):
        op.admit('ui03-farmhand-join')
