"""Bounded operator process: no gameplay inputs and no provider logic.

Wait for exact-identity watchdog containment before launching any owned children.
This process and all subsequently spawned children are in the watchdog's job.
"""
import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[1]))
from arti_runtime import ipc
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager

if __package__:
    from .stardew_trial_watchdog import OperationLease, process_identity
else:
    from stardew_trial_watchdog import OperationLease, process_identity


_OWNED_FILES = frozenset({
    'controller-manifest.json', 'controller-identity.json', 'controller-ready.json',
    'controller-result.json', 'watchdog-ready.json', 'watchdog-operation.json',
    'operation-complete.json', 'operation-request.json', 'operation-status.json',
    'operator-heartbeat.json', 'supervisor-heartbeat.json', 'heartbeat.json',
    'terminal-stop.json', 'operator-result.json', 'owned-children.json',
    'host.log', 'farmhand.log', 'bridge.log',
    'STOP', 'LAUNCH_GAMES', 'START_FARMHAND', 'START_BRIDGE',
})


@contextmanager
def file_access(path, operation):
    """Annotate the original OSError, never retry or expose private paths/data."""
    try:
        yield
    except OSError as exc:
        name = path.name
        if operation == 'write_text' and getattr(exc, 'ipc_phase', None) == 'publish':
            operation = 'replace'
        exc.controller_file_access = dict(
            file=name if name in _OWNED_FILES else 'unclassified', operation=operation)
        raise


def read_json(path):
    with file_access(path, 'read_text'):
        content = ipc.read_text(path)
    return json.loads(content)


def file_exists(path):
    with file_access(path, 'stat'):
        return path.exists()


def open_output(path):
    with file_access(path, 'open_wb'):
        return path.open('wb')


def error_details(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    return dict(type=type(exc).__name__, errno=getattr(exc, 'errno', None),
        winerror=getattr(exc, 'winerror', None),
        line=frames[-1].lineno if frames else None,
        code=str(exc) if type(exc) is ValueError else None,
        file_access=getattr(exc, 'controller_file_access', None),
        frames=[dict(file=Path(f.filename).name, function=f.name, line=f.lineno)
                for f in frames[-12:]])


def write(folder, name, value):
    path = folder / name
    with file_access(path, 'write_text'):
        ipc.write_json(path, value, critical='heartbeat' not in name)


def identity():
    return process_identity()


def require(condition, code):
    if not condition:
        raise ValueError(code)


def supervisor_check(value, *, run_id, now, bridge_started, previous, autonomous_started):
    require(value['run_id'] == run_id, 'supervisor_identity_mismatch')
    stamp = value['monotonic']
    require(type(stamp) in (int, float) and math.isfinite(stamp), 'supervisor_clock_invalid')
    require(previous <= stamp <= now and now - stamp < 5, 'supervisor_lost')
    start = value.get('autonomous_started')
    if start is not None:
        require(type(start) in (int, float) and math.isfinite(start), 'autonomous_clock_invalid')
        require(bridge_started <= start <= now, 'autonomous_clock_invalid')
        if autonomous_started is not None:
            require(start == autonomous_started, 'autonomous_identity_changed')
        autonomous_started = start
    elif autonomous_started is not None:
        raise ValueError('autonomous_identity_lost')
    # Leave one second for independent termination before the 300s ceiling.
    if autonomous_started is not None and now >= autonomous_started + 299:
        return stamp, autonomous_started, 'AUTONOMOUS_DEADLINE'
    return stamp, autonomous_started, None


def read_pulse(path, clock=time.monotonic):
    value = read_json(path)
    return value, clock()  # Sample after reading a concurrently published stamp.


class SupervisorPulse:
    """One validated sample can cover brief read contention, never fresh liveness."""

    def __init__(self, *, run_id, bridge_started):
        self.run_id, self.bridge_started = run_id, bridge_started
        self.last, self.autonomous_started = None, None
        self.access_started, self.attempts, self.error = None, 0, None
        self.evidence = dict(failures=0, recovered_episodes=0)

    def poll(self, path, *, clock=time.monotonic):
        retry = False
        try:
            value, now = read_pulse(path, clock=clock)
        except PermissionError as exc:
            now = clock()
            self.evidence['failures'] += 1
            self.evidence['last_error'] = dict(now_monotonic=now,
                errno=exc.errno, winerror=getattr(exc, 'winerror', None),
                file_access=getattr(exc, 'controller_file_access', None))
            if self.access_started is None:
                self.access_started, self.error = now, exc
            self.attempts += 1
            if self.last is None or self.attempts > 25:
                raise
            value, retry = self.last, True
        if self.access_started is not None:
            # Even a readable later poll cannot resurrect an expired recovery.
            if now >= self.access_started + .5:
                raise self.error
            require(now >= self.access_started, 'supervisor_access_clock_regressed')
        previous = self.last['monotonic'] if self.last is not None else 0
        _, autonomous, reason = supervisor_check(value, run_id=self.run_id,
            now=now, bridge_started=self.bridge_started, previous=previous,
            autonomous_started=self.autonomous_started)
        if retry:
            end = min(self.access_started + .5, previous + 5)
            if autonomous is not None:
                end = min(end, autonomous + 299)
            return now, reason, max(0., min(.02, end - now))
        self.last, self.autonomous_started = value, autonomous
        if self.access_started is not None:
            self.evidence['recovered_episodes'] += 1
        self.access_started, self.attempts, self.error = None, 0, None
        return now, reason, None


def operation_update(folder, lease, *, pulse, now=None, clock=time.monotonic):
    """Admit once with fresh caller evidence; never extend an active operation."""
    completion_path = folder / 'operation-complete.json'
    request_path = folder / 'operation-request.json'
    done = read_json(completion_path) if file_exists(completion_path) else None
    request = read_json(request_path) if file_exists(request_path) else None
    now = clock() if now is None else now
    if done is not None:
        require(all(done.get(k)==v for k,v in lease.binding.items()), 'completion_identity_mismatch')
        op = done.get('operation_id')
        if lease.active and op == lease.active['operation_id']:
            lease.complete(op, done.get('outcome'), now=now)
            write(folder, 'operation-status.json', dict(**lease.binding,
                operation_id=op, outcome=done['outcome'], operation=None))
        else:
            require(op in lease.used, 'completion_operation_mismatch')
    if request is not None:
        require(all(request.get(k)==v for k,v in lease.binding.items()), 'request_identity_mismatch')
        op = request.get('operation_id')
        if op not in lease.used:
            require(lease.active is None, 'operation_already_active')
            # Watchdog must witness the old lease release before another starts.
            ack = read_json(folder / 'watchdog-operation.json')
            require(ack.get('run_id')==lease.binding['run_id'], 'watchdog_ack_mismatch')
            if ack.get('operation_id') is None:
                stamp, kind = request.get('requested_monotonic'), request.get('kind')
                require(type(stamp) in (int,float) and math.isfinite(stamp)
                    and lease.started <= stamp <= now and now-stamp < 5,
                    'operation_request_stale')
                require(kind in lease.CAPS, 'operation_kind_invalid')
                value = dict(**lease.binding, operation_id=op, kind=kind,
                    started_monotonic=now,
                    deadline_monotonic=min(lease.deadline, now+lease.CAPS[kind]))
                lease.check(value, now=now, pulse=pulse)
                write(folder, 'operation-status.json', dict(**lease.binding,
                    operation_id=op, outcome='active', operation=value))
    lease.check(lease.active, now=now, pulse=pulse)
    return now


def game_launch_order(cfg):
    order = cfg.get('game_launch_order', 'together')
    require(order in ('together', 'host_first'), 'game_launch_order_invalid')
    return order


def launch_games(folder, cfg, processes, lease, *, pulse, clock=time.monotonic):
    """Launch only the admitted setup stage, inside the existing controller job."""
    staged = game_launch_order(cfg) == 'host_first'
    farm_request = file_exists(folder / 'START_FARMHAND')
    if farm_request:
        require(staged and 'host' in processes, 'farmhand_requested_before_host')
    names = ()
    if file_exists(folder / 'LAUNCH_GAMES') and not processes:
        names = ('host',) if staged else ('host', 'farmhand')
    elif farm_request and 'farmhand' not in processes:
        names = ('farmhand',)

    def check_stage(name):
        require(not file_exists(folder / 'STOP'), 'launch_stopped')
        key = 'ui01-launch-title' if name == 'host' else 'ui03-farmhand-join'
        op = cfg['operation_ids'][key]
        require(lease.active is not None and lease.active['operation_id'] == op
            and lease.active['kind'] == 'ui_setup', 'launch_operation_mismatch')
        require(process_identity(lease.binding['watchdog']['pid']) == lease.binding['watchdog'],
            'watchdog_identity_mismatch')
        ack = read_json(folder / 'watchdog-operation.json')
        require(ack == dict(run_id=lease.binding['run_id'], operation_id=op),
            'launch_watchdog_ack_mismatch')
        request = None
        if name == 'farmhand':
            request = read_json(folder / 'START_FARMHAND')
            require(all(request.get(k) == v for k, v in lease.binding.items())
                and request.get('operation_id') == op, 'farmhand_request_identity_mismatch')
            done = read_json(folder / 'operation-complete.json')
            host_load = cfg['operation_ids']['ui02-host-load']
            require(host_load in lease.used and done == dict(**lease.binding,
                operation_id=host_load, outcome='success'), 'host_load_not_completed')
        now = clock()
        lease.check(lease.active, now=now, pulse=pulse)
        if request is not None:
            stamp = request.get('requested_monotonic')
            require(type(stamp) in (int, float) and math.isfinite(stamp)
                and lease.active['started_monotonic'] <= stamp <= now and now-stamp < 5,
                'farmhand_request_stale')

    for name in names:
        if staged: check_stage(name)
        with open_output(folder / (name + '.log')) as output:
            # Opening the log may take time: recheck the lease immediately before spawn.
            if staged: check_stage(name)
            processes[name] = subprocess.Popen(cfg[name], cwd=cfg['game_dir'],
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
    if names:
        write(folder, 'owned-children.json', {n:p.pid for n,p in processes.items()})


def run(folder):
    cfg = read_json(folder / 'controller-manifest.json')
    run_id = cfg['run_id']
    own_identity = identity()
    write(folder, 'controller-identity.json', own_identity)
    started = time.monotonic()
    processes = {}
    reason = 'CONTROLLER_ERROR'
    error = None
    supervisor = None
    try:
        while not file_exists(folder / 'watchdog-ready.json'):
            if time.monotonic() - started > 20:
                raise ValueError('watchdog_not_ready')
            time.sleep(.05)
        ready = read_json(folder / 'watchdog-ready.json')
        require(ready['run_id'] == run_id and ready['pinned_processes'] == 1,
            'watchdog_ready_mismatch')
        require(started < ready['deadline_monotonic'] <= started + 600, 'watchdog_deadline_invalid')
        require(ready['controller']==own_identity and ready['containment_id']==cfg['containment_id'],
            'watchdog_containment_mismatch')
        require(process_identity(ready['watchdog']['pid'])==ready['watchdog'], 'watchdog_identity_mismatch')
        lease = OperationLease(run_id=run_id, controller=own_identity,
            watchdog=ready['watchdog'], containment_id=cfg['containment_id'],
            started=ready['started_monotonic'], deadline=ready['deadline_monotonic'])
        write(folder, 'controller-ready.json', dict(run_id=run_id, contained=True,
            pid=os.getpid(), deadline_monotonic=ready['deadline_monotonic']))
        bridge_started = None
        while time.monotonic() < ready['deadline_monotonic']:
            if file_exists(folder / 'STOP'):
                reason = 'REQUESTED_STOP'; break
            if file_exists(folder / 'terminal-stop.json'):
                terminal = read_json(folder / 'terminal-stop.json')
                require(terminal['run_id'] == run_id, 'terminal_identity_mismatch')
                reason = 'BRIDGE_TERMINAL_STOP'; break
            if file_exists(folder / 'operator-result.json'):
                terminal=read_json(folder / 'operator-result.json')
                require(terminal['run_id']==run_id, 'result_identity_mismatch')
                reason = 'BRIDGE_TERMINAL_RESULT'; break
            if any(process.poll() is not None for process in processes.values()):
                reason = 'OWNED_CHILD_EXITED'; break
            require(process_identity(ready['watchdog']['pid'])==ready['watchdog'], 'watchdog_identity_mismatch')
            pulse, now = read_pulse(folder / 'operator-heartbeat.json')
            require(pulse['run_id']==run_id and pulse.get('monotonic') is not None,
                'operator_identity_mismatch')
            # Check the existing lease before touching any requested transition.
            lease.check(lease.active, now=now, pulse=pulse['monotonic'])
            if bridge_started is not None:
                pulse_path = folder / 'supervisor-heartbeat.json'
                if supervisor.last is None and not file_exists(pulse_path):
                    if now - bridge_started >= 45:
                        reason = 'BRIDGE_SUPERVISOR_START_TIMEOUT'; break
                else:
                    now, limit_reason, retry_delay = supervisor.poll(pulse_path)
                    lease.check(lease.active, now=now, pulse=pulse['monotonic'])
                    if limit_reason:
                        reason = limit_reason; break
                    if retry_delay is not None:
                        # No admission, launch or heartbeat publication from
                        # unreadable bytes. Outer STOP/process checks keep running.
                        end = lease.deadline
                        if lease.active is not None:
                            end = min(end, lease.active['deadline_monotonic'])
                        else:
                            end = min(end, pulse['monotonic'] + 25)
                        time.sleep(min(retry_delay, end - now))
                        continue
            now = operation_update(folder, lease, pulse=pulse['monotonic'])
            write(folder, 'heartbeat.json', dict(run_id=run_id, monotonic=now, operation=lease.active))
            launch_games(folder, cfg, processes, lease, pulse=pulse['monotonic'])
            if file_exists(folder / 'START_BRIDGE') and 'bridge' not in processes:
                require({'host', 'farmhand'} <= processes.keys(), 'games_not_started')
                with open_output(folder / 'bridge.log') as output:
                    bridge_started = time.monotonic()
                    supervisor = SupervisorPulse(run_id=run_id, bridge_started=bridge_started)
                    processes['bridge'] = subprocess.Popen(cfg['bridge'], cwd=cfg['source_dir'],
                        stdout=output, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                write(folder, 'owned-children.json', {n:p.pid for n,p in processes.items()})
            time.sleep(.1)
        else:
            reason = 'CONTROLLER_DEADLINE'
    except BaseException as exc:
        reason = 'CONTROLLER_SAFETY_FAILURE'
        error = error_details(exc)
    finally:
        try:
            write(folder, 'controller-result.json', dict(run_id=run_id, reason=reason,
                owned_children={n:p.pid for n,p in processes.items()}, error=error,
                **({'supervisor_access': supervisor.evidence}
                   if supervisor is not None and supervisor.evidence['failures'] else {})))
        finally:
            (folder / 'STOP').touch(exist_ok=True)
        # Allow watchdog to terminate this process and its complete job together.
        time.sleep(10)


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve())
