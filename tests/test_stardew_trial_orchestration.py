from scripts import stardew_trial_watchdog as watchdog
"""No game/provider: operation ownership and exact config cleanup boundaries."""
import hashlib
import json

import pytest

from scripts.stardew_trial_watchdog import OperationLease, restore_configs


def guard():
    return OperationLease(run_id="trial", controller={"pid":10},
        watchdog={"pid":20}, containment_id="nonce", started=100, deadline=600)


def lease(**changes):
    value = dict(run_id="trial", controller={"pid":10}, watchdog={"pid":20},
        containment_id="nonce", operation_id="op1", kind="ui_setup",
        started_monotonic=101, deadline_monotonic=221)
    value.update(changes)
    return value


def test_external_heartbeat_and_bounded_operation_gap():
    g = guard()
    g.check(None, now=101, pulse=101)
    with pytest.raises(ValueError, match="operator_lost"):
        g.check(None, now=130, pulse=101)
    g = guard()
    g.check(lease(), now=101, pulse=101)
    g.check(lease(), now=180, pulse=101)  # 79s remote silence, healthy controller
    with pytest.raises(ValueError, match="operation_expired"):
        g.check(lease(), now=221, pulse=101)


@pytest.mark.parametrize("field,value", [("run_id","other"), ("controller",{"pid":11}),
    ("watchdog",{"pid":21}), ("containment_id","other"), ("kind","anything"),
    ("started_monotonic",99), ("started_monotonic",102),
    ("deadline_monotonic",222), ("deadline_monotonic",float("nan"))])
def test_bad_lease_rejected(field, value):
    with pytest.raises(ValueError):
        guard().check(lease(**{field:value}), now=101, pulse=101)


def test_no_mutation_replacement_replay_or_clock_regression():
    g=guard(); g.check(lease(),now=101,pulse=101)
    for bad in (lease(operation_id="op2"), lease(deadline_monotonic=220),
                lease(started_monotonic=100)):
        with pytest.raises(ValueError): g.check(bad,now=110,pulse=101)
    with pytest.raises(ValueError): g.check(lease(),now=100,pulse=101)
    g.check(None,now=110,pulse=110)
    with pytest.raises(ValueError): g.check(lease(),now=111,pulse=111)


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_completed_operation_returns_to_fresh_heartbeat(outcome):
    g=guard(); g.check(lease(),now=101,pulse=101)
    g.complete("op1", outcome, now=110)
    assert g.active is None
    with pytest.raises(ValueError,match="operator_lost"):
        g.check(None,now=140,pulse=101)


@pytest.mark.parametrize("pulse", [99, 102, float("nan"), True])
def test_active_lease_does_not_hide_bad_pulse_clock(pulse):
    g=guard();g.check(lease(),now=101,pulse=101)
    with pytest.raises(ValueError):g.check(lease(),now=101,pulse=pulse)


def config_spec(tmp_path):
    original=b'{"enabled": false}\r\n'
    trial=b'{"enabled":true,"value":1,"items":[1,2]}'
    (tmp_path/'original.bin').write_bytes(original)
    (tmp_path/'trial.json').write_bytes(trial)
    spec=dict(path=str(tmp_path/'config.json'), original_path=str(tmp_path/'original.bin'),
        original_sha256=hashlib.sha256(original).hexdigest(),
        trial_path=str(tmp_path/'trial.json'), trial_sha256=hashlib.sha256(trial).hexdigest(),
        trial_semantics=json.loads(trial))
    return original,trial,spec


@pytest.mark.parametrize('mode',['original','exact_trial','formatting'])
def test_config_restored_exactly_and_idempotently(tmp_path,mode):
    original,trial,spec=config_spec(tmp_path)
    current={'original':original,'exact_trial':trial,
        'formatting':b'{\r\n "items": [1, 2], "value": 1, "enabled": true\r\n}'}[mode]
    (tmp_path/'config.json').write_bytes(current)
    assert restore_configs([spec])==[True]
    assert (tmp_path/'config.json').read_bytes()==original
    assert restore_configs([spec])==[True]


@pytest.mark.parametrize('current', [b'{"enabled":false,"value":1,"items":[1,2]}',
    b'{"enabled":true,"value":true,"items":[1,2]}',
    b'{"enabled":true,"value":1,"items":[2,1]}',
    b'{"enabled":false,"enabled":true,"value":1,"items":[1,2]}',
    b'{"enabled":true,"value":NaN,"items":[1,2]}',
    b'{"enabled":true,"value":1e400,"items":[1,2]}'])
def test_semantic_drift_preserved(tmp_path,current):
    _,_,spec=config_spec(tmp_path)
    (tmp_path/'config.json').write_bytes(current)
    assert restore_configs([spec])==[False]
    assert (tmp_path/'config.json').read_bytes()==current


def test_retained_temporary_semantics_cannot_drift(tmp_path):
    _,trial,spec=config_spec(tmp_path)
    (tmp_path/'config.json').write_bytes(trial+b' ')
    spec['trial_semantics']['value']=2
    assert restore_configs([spec])==[False]


@pytest.mark.parametrize('outcome',['success','failure'])
def test_controller_operation_protocol_clears_and_cannot_renew(tmp_path,outcome):
    from scripts.stardew_trial_controller import operation_update, write
    g=guard()
    write(tmp_path,'watchdog-operation.json',dict(run_id='trial',operation_id=None))
    request=dict(**g.binding,operation_id='op1',kind='ui_setup',requested_monotonic=101)
    write(tmp_path,'operation-request.json',request)
    operation_update(tmp_path,g,now=101,pulse=101)
    first=dict(g.active)
    operation_update(tmp_path,g,now=180,pulse=101)
    assert g.active==first
    write(tmp_path,'operation-complete.json',dict(**g.binding,operation_id='op1',outcome=outcome))
    operation_update(tmp_path,g,now=181,pulse=181)
    assert g.active is None
    operation_update(tmp_path,g,now=182,pulse=181)
    assert g.active is None  # old request is not a new grant
    with pytest.raises(ValueError,match='operator_lost'):
        operation_update(tmp_path,g,now=207,pulse=181)


@pytest.mark.parametrize('request_change',[{'requested_monotonic':99},{'requested_monotonic':102},
    {'run_id':'wrong'},{'controller':{'pid':9}},{'watchdog':{'pid':9}},{'kind':'unknown'}])
def test_controller_rejects_bad_operation_request(tmp_path,request_change):
    from scripts.stardew_trial_controller import operation_update, write
    g=guard()
    write(tmp_path,'watchdog-operation.json',dict(run_id='trial',operation_id=None))
    request=dict(**g.binding,operation_id='op1',kind='ui_setup',requested_monotonic=101)
    request.update(request_change)
    write(tmp_path,'operation-request.json',request)
    with pytest.raises(ValueError):operation_update(tmp_path,g,now=101,pulse=101)


@pytest.mark.parametrize('case',['stop','containment','unconfirmed','regressed'])
def test_watchdog_stop_and_containment_dominate_restore_gate(tmp_path,case):
    from scripts.stardew_trial_watchdog import run_watchdog
    from types import SimpleNamespace
    original,trial,spec=config_spec(tmp_path)
    target=tmp_path/'config.json';target.write_bytes(trial+b' ')
    now=[100]; killed=[]
    def terminate():killed.append(True)
    def wait():
        assert target.read_bytes()==trial+b' '  # never restore before all gone
        return case!='unconfirmed'
    p=SimpleNamespace(alive=lambda:True,contained=lambda:case!='containment',
        terminate=terminate,wait=wait,close=lambda:None)
    (tmp_path/'heartbeat.json').write_text(json.dumps(dict(run_id='trial',monotonic=100)))
    if case in {'stop','unconfirmed'}:(tmp_path/'STOP').touch()
    def sleep(_):
        now[0]=99
        (tmp_path/'heartbeat.json').write_text(json.dumps(dict(run_id='trial',monotonic=100)))
    reason=run_watchdog(dict(run_id='trial',containment_id='nonce',control_dir=str(tmp_path),
        deadline_monotonic=200,processes=[{'pid':10}],config_restores=[spec]),
        factory=lambda _:p,clock=lambda:now[0],sleep=sleep,identity_factory=lambda:{'pid':20})
    assert killed==[True]
    if case=='containment': assert reason=='OWNED_CONTAINMENT_LOST'
    if case=='stop': assert reason=='REQUESTED_STOP'
    assert target.read_bytes()==(trial+b' ' if case=='unconfirmed' else original)


def test_supervisor_bound_is_independent_and_immutable():
    from scripts.stardew_trial_controller import supervisor_check
    pulse=dict(run_id='proof',monotonic=313,autonomous_started=15)
    assert supervisor_check(pulse,run_id='proof',now=314,bridge_started=10,
        previous=312,autonomous_started=15)==(313,15,'AUTONOMOUS_DEADLINE')
    for altered in (dict(pulse,autonomous_started=16),dict(pulse,autonomous_started=None),
                    dict(pulse,monotonic=300)):
        with pytest.raises(ValueError):
            supervisor_check(altered,run_id='proof',now=314,bridge_started=10,
                previous=312,autonomous_started=15)


@pytest.mark.parametrize('outcome',['success','failure'])
@pytest.mark.parametrize('now',[221,222])
def test_late_completion_cannot_erase_expired_operation(tmp_path,outcome,now):
    from scripts.stardew_trial_controller import operation_update, write
    g=guard();g.check(lease(),now=101,pulse=101)
    write(tmp_path,'operation-complete.json',dict(**g.binding,operation_id='op1',outcome=outcome))
    with pytest.raises(ValueError,match='operation_expired'):
        operation_update(tmp_path,g,now=now,pulse=now)
    assert g.active is not None
    # Independent watchdog must reject removal even when it misses completion.
    g=guard();g.check(lease(),now=101)
    with pytest.raises(ValueError,match='operation_expired'):g.check(None,now=now)
    assert g.active is not None


def test_nonfinite_exponent_overflow_is_not_valid_config_json():
    from scripts.stardew_trial_watchdog import strict_json
    with pytest.raises(ValueError,match='nonfinite_config_number'):
        strict_json('{"value":1e400}')


def test_distinct_overflow_numbers_cannot_authorize_restore(tmp_path):
    _,_,spec=config_spec(tmp_path)
    trial=b'{"value":1e400}'
    current=b'{"value":2e400}'
    (tmp_path/'trial.json').write_bytes(trial)
    (tmp_path/'config.json').write_bytes(current)
    spec.update(trial_sha256=hashlib.sha256(trial).hexdigest(),trial_semantics=json.loads(trial))
    assert restore_configs([spec])==[False]
    assert (tmp_path/'config.json').read_bytes()==current


@pytest.mark.parametrize('raw,now,subcondition,exception', [
    (None, 101, 'missing', 'FileNotFoundError'),
    (b'{', 101, 'parse_failure', 'JSONDecodeError'),
    (b'\xff', 101, 'parse_failure', 'UnicodeDecodeError'),
    (b'type-error', 101, 'invalid_type', 'TypeError'),
    (b'{"run_id":"trial"}', 101, 'missing_key', 'KeyError'),
    (b'{"run_id":"other","monotonic":100}', 101, 'wrong_run_id', None),
    (b'{"monotonic":100}', 101, 'wrong_run_id', None),
    (b'{"run_id":"trial","monotonic":null}', 101, 'non_numeric', None),
    (b'{"run_id":"trial","monotonic":true}', 101, 'non_numeric', None),
    (b'{"run_id":"trial","monotonic":"private-value"}', 101, 'non_numeric', None),
    pytest.param(b'{"run_id":"trial","monotonic":"' + b'private' * 10000 + b'"}',
        101, 'non_numeric', None, id='large-private-candidate'),
    (b'{"run_id":"trial","monotonic":[]}', 101, 'non_numeric', None),
    (b'{"run_id":"trial","monotonic":{}}', 101, 'non_numeric', None),
    (b'{"run_id":"trial","monotonic":NaN}', 101, 'nonfinite', None),
    (b'{"run_id":"trial","monotonic":Infinity}', 101, 'nonfinite', None),
    (b'{"run_id":"trial","monotonic":-Infinity}', 101, 'nonfinite', None),
    (b'{"run_id":"trial","monotonic":99}', 101, 'regressed', None),
    (b'{"run_id":"trial","monotonic":102}', 101, 'future', None),
    (b'{"run_id":"trial","monotonic":100}', 130, 'stale', None),
])
def test_controller_lost_retains_exact_poll_and_restores_after_cleanup(
        tmp_path, monkeypatch, raw, now, subcondition, exception):
    from pathlib import Path
    from types import SimpleNamespace
    from scripts.stardew_trial_watchdog import run_watchdog

    original, trial, spec = config_spec(tmp_path)
    target = tmp_path / 'config.json'
    target.write_bytes(trial + b' ')
    heartbeat = tmp_path / 'heartbeat.json'
    heartbeat.write_text(json.dumps(dict(run_id='trial', monotonic=100)))
    ticks, sleeps, reads, cleanup = [100], [], [], []
    read_bytes = watchdog.ipc.read_bytes

    def read(path):
        if path == heartbeat:
            reads.append(ticks[0])
        return read_bytes(path)

    monkeypatch.setattr(watchdog.ipc, 'read_bytes', read)
    loads = json.loads
    def parse(data, *args, **kwargs):
        if data == 'type-error':
            raise TypeError('private parser error')
        return loads(data, *args, **kwargs)
    monkeypatch.setattr(json, 'loads', parse)

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) == 1  # first failing poll is terminal, no grace/retry
        ticks[0] = now
        if raw is None:
            heartbeat.unlink()
        else:
            heartbeat.write_bytes(raw)

    def wait():
        assert target.read_bytes() == trial + b' '
        cleanup.append('wait')
        return True

    process = SimpleNamespace(alive=lambda: True,
        terminate=lambda: cleanup.append('terminate'), wait=wait,
        close=lambda: cleanup.append('close'))
    result = run_watchdog(dict(run_id='trial', deadline_monotonic=200,
        control_dir=str(tmp_path), processes=[{'pid': 10}], config_restores=[spec]),
        factory=lambda _: process, clock=lambda: ticks[0], sleep=sleep)
    encoded = (tmp_path / 'watchdog-result.json').read_text()
    retained = json.loads(encoded)
    assert result == retained['reason'] == 'CONTROLLER_LOST'
    detail = retained['controller_lost']
    assert detail['subcondition'] == subcondition
    assert detail['now_monotonic'] == now
    assert detail['previous_accepted_heartbeat'] == 100
    assert detail['comparison_heartbeat'] == 100
    assert reads == [100, now] and sleeps == [0.2]
    assert cleanup == ['terminate', 'wait', 'close']
    assert retained['cleanup_confirmed'] and retained['config_restored'] == [True]
    assert target.read_bytes() == original
    assert len(encoded) < 1200 and 'private' not in encoded
    # Result must be strict JSON even for a NaN/Infinity candidate.
    json.loads(encoded, parse_constant=lambda _: pytest.fail('nonfinite JSON output'))
    if exception:
        assert detail['exception_class'] == exception
    if raw is not None:
        assert detail['snapshot'] == dict(byte_length=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    if subcondition == 'missing_key':
        assert detail['key'] == 'monotonic'
    if raw == b'{':
        assert detail['parse_position'] == 1
    if subcondition in {'non_numeric', 'nonfinite', 'regressed', 'future', 'stale'}:
        value = json.loads(raw)['monotonic']
        candidate = detail['candidate']
        assert candidate['type'] == type(value).__name__
        assert candidate['finite'] == (None if subcondition == 'non_numeric' else subcondition != 'nonfinite')
        if subcondition == 'non_numeric':
            assert 'value' not in candidate
        else:
            assert candidate['value'] == (str(value) if subcondition == 'nonfinite' else value)


@pytest.mark.parametrize('case,expected', [
    ('recover', 'REQUESTED_STOP'),
    ('permanent', 'CONTROLLER_LOST'),
    ('frozen_clock', 'CONTROLLER_LOST'),
    ('initial', 'CONTROLLER_LOST'),
    ('stop', 'REQUESTED_STOP'),
    ('deadline', 'WATCHDOG_DEADLINE'),
    ('stale', 'CONTROLLER_LOST'),
    ('lease', 'operation_expired'),
    ('regressed_clock', 'WATCHDOG_DEADLINE'),
    ('process_exit', 'OWNED_PROCESS_EXITED'),
    ('containment', 'OWNED_CONTAINMENT_LOST'),
    ('malformed', 'CONTROLLER_LOST'),
    ('wrong_run', 'CONTROLLER_LOST'),
    ('missing', 'CONTROLLER_LOST'),
    ('other_io', 'CONTROLLER_LOST'),
    ('late_success', 'CONTROLLER_LOST'),
    ('retry_clock_regressed', 'WATCHDOG_CLOCK_REGRESSED'),
])
def test_heartbeat_access_retry_never_renews_liveness_or_operation(
        tmp_path, monkeypatch, case, expected):
    from pathlib import Path
    from types import SimpleNamespace
    from scripts.stardew_trial_watchdog import run_watchdog

    now, reads, sleeps, cleanup = [100.0], [], [], []
    original, trial, config = config_spec(tmp_path)
    target = tmp_path/'config.json'
    target.write_bytes(trial)
    heartbeat = tmp_path / 'heartbeat.json'
    operation = dict(run_id='trial', controller={'pid':10}, watchdog={'pid':20},
        containment_id='nonce', operation_id='ui01', kind='ui_setup',
        started_monotonic=100, deadline_monotonic=220)
    if case == 'lease':
        operation['deadline_monotonic'] = 100.21
    def publish():
        heartbeat.write_text(json.dumps(dict(run_id='trial', monotonic=now[0],
            operation=operation)))
    publish()
    original_read = watchdog.ipc.read_bytes
    def read(path):
        if path != heartbeat:
            return original_read(path)
        reads.append(now[0])
        if len(reads) == 1 and case != 'initial':
            return original_read(path)
        if case in {'recover', 'late_success'} and len(reads) >= 3:
            # A real new read, not a cached/synthetic timestamp, permits ACK.
            heartbeat.write_text(json.dumps(dict(run_id='trial', monotonic=now[0], operation=None)))
            return original_read(path)
        if case in {'malformed', 'wrong_run'} and len(reads) == 3:
            return b'{' if case == 'malformed' else b'{"run_id":"other","monotonic":100}'
        if case == 'missing':
            raise FileNotFoundError(2, 'missing')
        if case == 'other_io':
            raise OSError(5, 'I/O error')
        error = PermissionError(13, 'private access failure')
        error.winerror = 32
        raise error
    monkeypatch.setattr(watchdog.ipc, 'read_bytes', read)
    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) < 40  # also bounded when the clock stops advancing
        if len(sleeps) == 1:
            now[0] = {'deadline':240, 'stale':130,
                'regressed_clock':99}.get(case, 100.2)
            if case == 'stop':
                (tmp_path/'STOP').touch()
        elif case == 'recover' and len(reads) == 3:
            assert json.loads((tmp_path/'watchdog-operation.json').read_text())['operation_id'] is None
            (tmp_path/'STOP').touch()
        else:
            # Unreadable bytes cannot clear/advance the accepted operation.
            assert json.loads((tmp_path/'watchdog-operation.json').read_text())['operation_id'] == 'ui01'
            if case != 'frozen_clock':
                now[0] += .6 if case == 'late_success' else seconds
            if case == 'retry_clock_regressed':
                now[0] = 100.1
    def wait():
        assert target.read_bytes() == trial  # restoration must follow cleanup
        return True
    process = SimpleNamespace(
        alive=lambda: not (case == 'process_exit' and len(reads)>1),
        contained=lambda: not (case == 'containment' and len(reads)>1),
        terminate=lambda: cleanup.append('terminate'),
        wait=wait, close=lambda: cleanup.append('close'))
    result = run_watchdog(dict(run_id='trial', containment_id='nonce',
        deadline_monotonic=240, control_dir=str(tmp_path), processes=[{'pid':10}],
        config_restores=[config]),
        factory=lambda _:process, identity_factory=lambda:{'pid':20},
        clock=lambda:now[0], sleep=sleep)
    report = json.loads((tmp_path/'watchdog-result.json').read_text())
    assert result == expected
    assert report['cleanup_confirmed'] and cleanup[-1] == 'close'
    assert report['config_restored'] == [True] and target.read_bytes() == original
    if case in {'permanent', 'frozen_clock', 'initial'}:
        detail = report['controller_lost']
        assert detail['subcondition'] == 'read_failure'
        assert detail['errno'] == 13 and detail['winerror'] == 32
        assert detail['previous_accepted_heartbeat'] == (None if case == 'initial' else 100)
        assert 'snapshot' not in detail
        assert len(reads) <= 27
        if case == 'permanent':
            assert 100.2 < now[0] <= 100.701
    if case == 'recover':
        assert report['heartbeat_access']['failures'] == 1
        assert report['heartbeat_access']['recovered_episodes'] == 1
        assert report['heartbeat_access']['last_error']['winerror'] == 32
    if case in {'initial', 'missing', 'other_io'}:
        assert len(reads) == (1 if case == 'initial' else 2)
    if case == 'late_success':
        assert report['controller_lost']['subcondition'] == 'access_retry_expired'
        assert report['heartbeat_access']['recovered_episodes'] == 0


@pytest.mark.parametrize('heartbeat_value', [100, 129.999])
def test_valid_heartbeat_keeps_boundary_and_stop_precedence(tmp_path, heartbeat_value):
    from types import SimpleNamespace
    from scripts.stardew_trial_watchdog import run_watchdog

    heartbeat = tmp_path / 'heartbeat.json'
    heartbeat.write_text(json.dumps(dict(run_id='trial', monotonic=heartbeat_value)))
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        heartbeat.write_bytes(b'{')
        (tmp_path / 'STOP').touch()
    process = SimpleNamespace(alive=lambda: True, terminate=lambda: None,
        wait=lambda: True, close=lambda: None)
    assert run_watchdog(dict(run_id='trial', deadline_monotonic=200,
        control_dir=str(tmp_path), processes=[{'pid':10}]), factory=lambda _:process,
        clock=lambda:129.999, sleep=sleep) == 'REQUESTED_STOP'
    assert sleeps == [0.2]
    assert 'controller_lost' not in json.loads((tmp_path/'watchdog-result.json').read_text())


@pytest.mark.parametrize('case', ['initial_missing', 'unconfirmed', 'drift', 'deadline', 'non_object'])
def test_heartbeat_diagnostics_preserve_initial_floor_and_cleanup_gates(tmp_path, case):
    from types import SimpleNamespace
    from scripts.stardew_trial_watchdog import run_watchdog

    original, trial, spec = config_spec(tmp_path)
    target = tmp_path / 'config.json'
    current = b'{"owner":"changed"}' if case == 'drift' else trial
    target.write_bytes(current)
    if case == 'non_object':
        (tmp_path / 'heartbeat.json').write_bytes(b'[]')
    ticks = iter([100, 200 if case == 'deadline' else 100])
    closed = []
    process = SimpleNamespace(alive=lambda: True, terminate=lambda: None,
        wait=lambda: case != 'unconfirmed', close=lambda: closed.append(True))
    def run():
        return run_watchdog(dict(run_id='trial', deadline_monotonic=200,
            control_dir=str(tmp_path), processes=[{'pid': 10}], config_restores=[spec]),
            factory=lambda _: process, clock=lambda: next(ticks),
            sleep=lambda _: pytest.fail('failure must not retry'))
    if case == 'non_object':
        # Canonical non-object .get raises AttributeError, outside the read catch.
        # Keep WATCHDOG_ERROR/cleanup, do not silently relabel that separate path.
        with pytest.raises(AttributeError):
            run()
    else:
        assert run() == ('WATCHDOG_DEADLINE' if case == 'deadline' else 'CONTROLLER_LOST')
    result = json.loads((tmp_path / 'watchdog-result.json').read_text())
    if case in {'deadline', 'non_object'}:
        assert 'controller_lost' not in result
        assert result['reason'] == ('WATCHDOG_DEADLINE' if case == 'deadline' else 'WATCHDOG_ERROR')
    else:
        detail = result['controller_lost']
        assert detail['subcondition'] == 'missing'
        assert detail['previous_accepted_heartbeat'] is None
        assert detail['comparison_heartbeat'] == 70
    assert result['cleanup_confirmed'] == (case != 'unconfirmed')
    assert result['config_restored'] == ([] if case == 'unconfirmed' else [case != 'drift'])
    assert target.read_bytes() == (current if case in {'unconfirmed', 'drift'} else original)
    assert closed == [True]
