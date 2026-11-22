"""Real Windows controller/job/lease integration, disposable Python only."""
import hashlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts.stardew_trial_controller import write
from scripts.stardew_trial_watchdog import process_identity

pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows owned process containment')
SCRIPTS=Path(__file__).parents[1]/'scripts'


def await_value(folder,name,predicate=lambda value:True,seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            value=json.loads((folder/name).read_text())
            if predicate(value): return value
        except (OSError,ValueError): pass
        time.sleep(.02)
    raise AssertionError(name)


@pytest.mark.parametrize('trigger,order',[
    *[(v,'together') for v in ('stop','controller_death','watchdog_death','deadline','silent_gap')],
    ('stop','host_first'),('bridge_early','host_first'),('staged_join','host_first')])
def test_operation_containment_and_unattended_restore(tmp_path,trigger,order):
    base=sys._base_executable
    sleeper=[base,'-c','import time; time.sleep(120)']
    run_id='disposable-'+trigger
    write(tmp_path,'controller-manifest.json',dict(run_id=run_id,containment_id='nonce',
        host=sleeper,farmhand=sleeper,bridge=sleeper,
        game_dir=str(tmp_path),source_dir=str(tmp_path),game_launch_order=order,
        operation_ids={'ui01-launch-title':'setup1','ui02-host-load':'load2','ui03-farmhand-join':'join3'}))
    original=b'{"enabled":false}\r\n'; trial=b'{"enabled":true}'
    (tmp_path/'original.bin').write_bytes(original)
    (tmp_path/'trial.json').write_bytes(trial)
    (tmp_path/'config.json').write_bytes(b'{\r\n "enabled": true\r\n}')
    controller=subprocess.Popen([base,str(SCRIPTS/'stardew_trial_controller.py'),str(tmp_path)],
        creationflags=subprocess.CREATE_NO_WINDOW)
    unrelated=subprocess.Popen(sleeper,creationflags=subprocess.CREATE_NO_WINDOW)
    watchdog=None
    child_handles=[]
    api=ctypes.WinDLL("kernel32",use_last_error=True)
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    api.OpenProcess.restype=wintypes.HANDLE
    api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    api.WaitForSingleObject.restype=wintypes.DWORD
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    try:
        spec=await_value(tmp_path,'controller-identity.json')
        deadline=time.monotonic()+(4 if trigger=='deadline' else 50)
        write(tmp_path,'manifest.json',dict(run_id=run_id,containment_id='nonce',
            control_dir=str(tmp_path),deadline_monotonic=deadline,processes=[spec],
            config_restores=[dict(path=str(tmp_path/'config.json'),
                original_path=str(tmp_path/'original.bin'),original_sha256=hashlib.sha256(original).hexdigest(),
                trial_path=str(tmp_path/'trial.json'),trial_sha256=hashlib.sha256(trial).hexdigest(),
                trial_semantics=json.loads(trial))]))
        pulse=dict(run_id=run_id,monotonic=time.monotonic())
        write(tmp_path,'operator-heartbeat.json',pulse);write(tmp_path,'heartbeat.json',pulse)
        watchdog=subprocess.Popen([base,str(SCRIPTS/'stardew_trial_watchdog.py'),
            '--manifest',str(tmp_path/'manifest.json')],creationflags=subprocess.CREATE_NO_WINDOW)
        await_value(tmp_path,'controller-ready.json')
        ready=await_value(tmp_path,'watchdog-ready.json')
        await_value(tmp_path,'watchdog-operation.json')
        binding={k:ready[k] for k in ('run_id','controller','watchdog','containment_id')}
        write(tmp_path,'operation-request.json',dict(**binding,operation_id='setup1',kind='ui_setup',
            requested_monotonic=time.monotonic()))
        await_value(tmp_path,'operation-status.json',lambda v:v['outcome']=='active')
        await_value(tmp_path,'watchdog-operation.json',lambda v:v['operation_id']=='setup1')
        (tmp_path/'LAUNCH_GAMES').touch()
        children=await_value(tmp_path,'owned-children.json')
        assert set(children)==({'host'} if order=='host_first' else {'host','farmhand'})
        if trigger=='staged_join':
            for completed,next_op in [('setup1','load2'),('load2','join3')]:
                write(tmp_path,'operator-heartbeat.json',dict(run_id=run_id,monotonic=time.monotonic()))
                write(tmp_path,'operation-complete.json',dict(**binding,operation_id=completed,outcome='success'))
                await_value(tmp_path,'operation-status.json',lambda v:v['outcome']=='success')
                await_value(tmp_path,'watchdog-operation.json',lambda v:v['operation_id'] is None)
                write(tmp_path,'operation-request.json',dict(**binding,operation_id=next_op,kind='ui_setup',
                    requested_monotonic=time.monotonic()))
                await_value(tmp_path,'operation-status.json',lambda v:v['outcome']=='active' and v['operation_id']==next_op)
                await_value(tmp_path,'watchdog-operation.json',lambda v:v['operation_id']==next_op)
            write(tmp_path,'START_FARMHAND',dict(**binding,operation_id='join3',requested_monotonic=time.monotonic()))
            children=await_value(tmp_path,'owned-children.json',lambda v:set(v)=={'host','farmhand'})
        from scripts.stardew_m1_operator import process_snapshot
        parents={v['pid']:v['parent'] for v in process_snapshot()}
        assert all(parents[pid]==controller.pid for pid in children.values())
        child_ids=[process_identity(pid) for pid in children.values()]
        for child in child_ids:
            handle=api.OpenProcess(0x100000,False,child["pid"])
            assert handle
            child_handles.append(handle)
            assert process_identity(child["pid"])==child  # pin the original child before triggering teardown
        if trigger=='silent_gap':
            # Actual 27s with NO caller pulses; the old controller failed at 25s.
            time.sleep(27)
            assert controller.poll() is None and watchdog.poll() is None
            assert time.monotonic()-pulse['monotonic']>25
            write(tmp_path,'operator-heartbeat.json',dict(run_id=run_id,monotonic=time.monotonic()))
            write(tmp_path,'operation-complete.json',dict(**binding,operation_id='setup1',outcome='success'))
            await_value(tmp_path,'operation-status.json',lambda v:v['outcome']=='success')
            await_value(tmp_path,'watchdog-operation.json',lambda v:v['operation_id'] is None)
            (tmp_path/'STOP').touch()
        elif trigger in ('stop','staged_join'): (tmp_path/'STOP').touch()
        elif trigger=='bridge_early':
            (tmp_path/'START_BRIDGE').touch()
            result=await_value(tmp_path,'controller-result.json')
            assert result['error']['code']=='games_not_started'
            assert set(result['owned_children'])=={'host'}
        elif trigger=='controller_death': controller.terminate()
        elif trigger=='watchdog_death': watchdog.terminate()
        watchdog.wait(timeout=10);controller.wait(timeout=5)
        for child,handle in zip(child_ids,child_handles):
            # KILL_ON_JOB_CLOSE can finish asynchronously after the watchdog
            # exits. Wait on the retained original process, never a reused PID.
            assert api.WaitForSingleObject(handle,5000)==0
            with pytest.raises(ValueError): process_identity(child['pid'])
        assert unrelated.poll() is None
        if trigger!='watchdog_death':
            result=await_value(tmp_path,'watchdog-result.json')
            assert result['cleanup_confirmed'] and result['config_restored']==[True]
            assert (tmp_path/'config.json').read_bytes()==original
            if trigger=='controller_death': assert result['reason']=='OWNED_PROCESS_EXITED'
        else:
            # Kernel closes the job on watchdog death; a dead watchdog cannot
            # attest/config-restore. Never invent a successful cleanup receipt.
            assert not (tmp_path/'watchdog-result.json').exists()
    finally:
        for handle in child_handles:
            api.CloseHandle(handle)
        for process in (controller,watchdog,unrelated):
            if process is not None:
                if process.poll() is None: process.terminate()
                process.wait(timeout=5)
