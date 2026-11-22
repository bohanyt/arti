"""Finite M1 process admission and operation transitions. No gameplay decisions."""
import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[1]))
from arti_runtime import ipc
from pathlib import Path
import ctypes, datetime, hashlib, json, os, subprocess, sys, time
from ctypes import wintypes
sys.dont_write_bytecode=True
R=Path(__file__).resolve().parents[1]
E=None
F=None

def configure(folder):
    global E,F
    E=Path(folder).resolve()
    F=E/'phase2-evidence'
sys.path.insert(0,str(R))
from scripts.stardew_trial_controller import write, game_launch_order
from scripts.stardew_trial_watchdog import process_identity, strict_json, semantic_identity
def read(p): return json.loads(ipc.read_text(p, encoding='utf-8-sig'))
def sha(p): return hashlib.sha256(ipc.read_bytes(p)).hexdigest()
def process_snapshot():
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('pid',wintypes.DWORD),('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),('parent',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),('exe',wintypes.WCHAR*260)]
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD];api.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    api.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)];api.Process32NextW.argtypes=api.Process32FirstW.argtypes
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=api.CreateToolhelp32Snapshot(2,0)
    assert handle not in (None,ctypes.c_void_p(-1).value)
    rows=[]
    try:
        entry=Entry();entry.dwSize=ctypes.sizeof(entry);valid=api.Process32FirstW(handle,ctypes.byref(entry))
        while valid:
            rows.append(dict(pid=entry.pid,parent=entry.parent,exe=entry.exe));valid=api.Process32NextW(handle,ctypes.byref(entry))
    finally: api.CloseHandle(handle)
    return rows
def event(kind,**value):
    F.mkdir(exist_ok=True)
    v=dict(kind=kind,monotonic=time.monotonic(),utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),**value)
    with ipc.open_shared(F/'events.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps(v)+'\n'); f.flush(); os.fsync(f.fileno())
    return v
def once(name,value):
    with (E/name).open('x',encoding='utf-8') as f: json.dump(value,f,indent=2)
def waitfor(fn,end):
    while time.monotonic()<end:
        assert not (E/'STOP').exists(),'STOP'
        value=fn()
        if value: return value
        time.sleep(.025)
    raise ValueError('LOCAL_TRANSACTION_TIMEOUT')
def maybe(name):
    p=E/name
    if not p.exists(): return None
    try: return read(p)
    except (json.JSONDecodeError,PermissionError): return None
def binding():
    n=read(E/'namespace.json'); r=read(E/'watchdog-ready.json')
    assert not (E/'STOP').exists(),'STOP'
    assert r['run_id']==n['run_id'] and r['containment_id']==n['containment_id']
    for key in ('controller','watchdog'): assert process_identity(r[key]['pid'])==r[key]
    assert time.monotonic()<r['deadline_monotonic']
    return {k:r[k] for k in ('run_id','controller','watchdog','containment_id')}
def admit(key):
    start=time.monotonic(); b=binding(); n=read(E/'namespace.json')
    assert key in n['operation_ids']
    staged_join = key == 'ui03-farmhand-join' and game_launch_order(
        read(E/'plan-manifest.json')) == 'host_first'
    if staged_join:
        require(not (E/'START_FARMHAND').exists(), 'farmhand_request_reused')
        require(read(E/'operation-complete.json') == dict(**b,
            operation_id=n['operation_ids']['ui02-host-load'], outcome='success'),
            'host_load_not_completed')
        require(set(read(E/'owned-children.json')) == {'host'}, 'host_stage_invalid')
    ack=read(E/'watchdog-operation.json'); assert ack==dict(run_id=b['run_id'],operation_id=None)
    if key=='bridge01-readiness': assert not (E/'START_BRIDGE').exists()
    op=n['operation_ids'][key]
    event('operation_request_boundary',operation_id=op)
    write(E,'operator-heartbeat.json',dict(run_id=b['run_id'],monotonic=time.monotonic()))
    write(E,'operation-request.json',dict(**b,operation_id=op,kind=('bridge_start' if key.startswith('bridge') else 'autonomous' if key.startswith('auto') else 'ui_setup'),requested_monotonic=time.monotonic()))
    def accepted():
        s=maybe('operation-status.json'); a=maybe('watchdog-operation.json')
        if not s or not a or s.get('operation_id')!=op or s.get('outcome')!='active' or a.get('operation_id')!=op: return None
        assert all(s.get(k)==v for k,v in b.items()) and a['run_id']==b['run_id']
        assert s['operation']['deadline_monotonic']-3>time.monotonic()
        return s
    s=waitfor(accepted,start+5); assert binding()==b
    event('operation_admitted',status=s,watchdog_ack=read(E/'watchdog-operation.json'),transaction_start=start)
    if key.startswith('bridge'):
        assert time.monotonic()<min(start+5,s['operation']['deadline_monotonic']-3)
        assert not (E/'STOP').exists()
        (E/'START_BRIDGE').touch(exist_ok=False)
        event('START_BRIDGE',operation_id=op)
    if staged_join:
        require(time.monotonic() < min(start+5,s['operation']['deadline_monotonic']-3),
            'farmhand_admission_expired')
        require(binding() == b, 'farmhand_binding_changed')
        write(E,'START_FARMHAND',dict(**b,operation_id=op,requested_monotonic=time.monotonic()))
        event('START_FARMHAND',operation_id=op)
        register_games({'host','farmhand'}, min(start+8,s['operation']['deadline_monotonic']-3))
    return s
def complete(key):
    start=time.monotonic(); b=binding(); n=read(E/'namespace.json'); op=n['operation_ids'][key]
    s=read(E/'operation-status.json')
    assert s['outcome']=='active' and s['operation_id']==op
    assert time.monotonic()<s['operation']['deadline_monotonic']
    event('operation_completion_boundary',prior_status=s,heartbeat=read(E/'heartbeat.json'),last_operator_heartbeat=read(E/'operator-heartbeat.json'))
    write(E,'operator-heartbeat.json',dict(run_id=b['run_id'],monotonic=time.monotonic()))
    write(E,'operation-complete.json',dict(**b,operation_id=op,outcome='success'))
    def done():
        v=maybe('operation-status.json'); a=maybe('watchdog-operation.json')
        return v and a and v.get('operation_id')==op and v.get('outcome')=='success' and a==dict(run_id=b['run_id'],operation_id=None)
    waitfor(done,min(start+5,s['operation']['deadline_monotonic']))
    event('operation_completed',operation_id=op)
def require(ok, reason):
    if not ok: raise ValueError(reason)

def validate_ceiling(value, *, maximum):
    require(type(value) is int and 0 < value <= maximum, 'invalid_ceiling')
    return value

def same_image(a,b):
    return a.replace('/', '\\').casefold()==b.replace('/', '\\').casefold()

def conflicting_processes(rows, *, own, parent, base_image, launcher_image):
    require(same_image(own['image'],base_image),'caller_image_mismatch')
    current=[v for v in rows if v['pid']==own['pid']]
    require(len(current)==1 and current[0]['parent']==parent['pid'],'caller_parent_mismatch')
    allowed={own['pid']}
    if same_image(parent['image'],launcher_image): allowed.add(parent['pid'])
    return [v for v in rows if v['exe'].casefold() in ('python.exe','pythonw.exe','stardew valley.exe','stardewmoddingapi.exe') and v['pid'] not in allowed]

def inventory(root, mtime=False):
    root=Path(root)
    return {str(p.relative_to(root)):dict(sha256=sha(p),bytes=p.stat().st_size,**({'mtime_ns':p.stat().st_mtime_ns} if mtime else {})) for p in sorted(root.rglob('*')) if p.is_file()}

def window_remaining(window):
    deadline=datetime.datetime.fromisoformat(window['deadline_utc'].replace('Z','+00:00'))
    pickup=datetime.datetime.fromisoformat(window['pickup_utc'].replace('Z','+00:00'))
    now=datetime.datetime.now(datetime.timezone.utc)
    require(0 < (deadline-pickup).total_seconds() <= 21600 and pickup <= now < deadline,'afk_window_expired')
    return (deadline-now).total_seconds()

def window_admission(window,seam):
    window_remaining(window)
    children=window['children']
    # One direct-owner extension; previous attempts/reservations remain counted.
    extension=window.get('owner_same_seam_extension')
    extra=(seam=='reviewed_ordinary_m1' and extension==dict(
        authority_url='https://github.example.invalid/owner/private-lab-repository/issues/example-c',
        seam='reviewed_ordinary_m1',max_attempts=3))
    completion=(seam=='reviewed_ordinary_m1' and len(children)>=3
        and all(c['seam']==seam for c in children[:3])
        and window.get('owner_m1_completion_extension')==dict(
            authority_url='https://github.example.invalid/owner/private-lab-repository/issues/example-d',
            seam='reviewed_ordinary_m1',prior_children=3,max_attempts=5))
    require(len(children)<(5 if completion else 3)
        and sum(c['seam']==seam for c in children)<(5 if completion else 3 if extra else 2),'afk_child_limit')
    require(all(c.get('postflight_complete') is True for c in children),'prior_child_unresolved')

def reserve_child(plan, run_id):
    path=Path(plan['window_path']); window=read(path)
    window_admission(window,plan['seam'])
    require(all(v['run_id']!=run_id for v in window['children']),'run_reused')
    # Every prior reservation stays counted; each adds 600/300s,24/24/48. No refund/replay.
    window['children'].append(dict(run_id=run_id,seam=plan['seam'],postflight_complete=False,reserved=dict(game_seconds=600,autonomous_seconds=300,provider=24,continuation=24,physical=48)))
    write(path.parent,path.name,window)

def preflight():
    require(sys.flags.optimize == 0, 'optimized_assertions_forbidden')
    source=read(E/'source-identities.json'); plan=read(E/'plan-manifest.json')
    if game_launch_order(plan) == 'host_first':
        ids=plan.get('operation_ids', {})
        require(ids == read(E/'namespace.json')['operation_ids']
            and all(k in ids for k in ('ui01-launch-title','ui02-host-load','ui03-farmhand-join'))
            and len(set(ids.values())) == len(ids), 'launch_operation_ids_invalid')
    def git(*args):return subprocess.check_output(['git','-C',str(R),*args],text=True).strip()
    require(str(R)==source['checkout'],'source_checkout_mismatch')
    require(git('rev-parse','HEAD')==source['source'],'source_head_mismatch')
    require(git('rev-parse','HEAD^{tree}')==source['tree'],'source_tree_mismatch')
    require(git('branch','--show-current')==source['branch'],'source_branch_mismatch')
    require(not git('status','--porcelain'),'source_dirty')
    for name,v in source['source_files'].items():
        require(sha(R/name)==v['sha256'] and git('rev-parse','HEAD:'+name)==v['git_blob'],'source_drift:'+name)
    for name,v in source['binaries'].items():
        require(sha(Path(name))==v['sha256'] and Path(name).stat().st_size==v['bytes'],'binary_drift:'+name)
    reviewed=read(E/'reviewed-source.json')
    require(reviewed['integrated_head']==source['source'] and reviewed['independent_review']=='PASS' and reviewed['ci']=='PASS' and reviewed['dispatch']=='1000000001','source_not_reviewed_integrated')
    require(bool(reviewed['review_url']) and bool(reviewed['ci_url']) and bool(reviewed['pr_url']),'review_provenance_missing')
    require(git('rev-parse',reviewed['reviewed_head']+'^{tree}')==source['tree'],'reviewed_tree_mismatch')
    require(subprocess.call(['git','-C',str(R),'merge-base','--is-ancestor',reviewed['reviewed_head'],'HEAD'])==0,'reviewed_head_not_integrated')
    base=plan['base_python']; launcher=str(R/'venv/Scripts/python.exe')
    require(base in source['binaries'] and launcher in source['binaries'],'interpreter_pins_missing')
    require(plan['controller']==[base,str(R/'scripts/stardew_trial_controller.py'),str(E)],'controller_command_mismatch')
    require(plan['watchdog']==[base,str(R/'scripts/stardew_trial_watchdog.py'),'--manifest',str(E/'watchdog-manifest.json')],'watchdog_command_mismatch')
    require(plan['bridge']==[launcher,'-B',str(R/'scripts/stardew_m1_observer.py'),str(E)],'bridge_command_mismatch')
    game=Path(plan['game_dir'])
    require(plan['host']==[str(game/'Stardew Valley.exe')],'host_command_mismatch')
    require(plan['farmhand']==[str(game/'StardewModdingAPI.exe'),'--mods-path',str(game/'Mods-ARTI-Gate131')],'farmhand_command_mismatch')
    for k in ('host','farmhand'):require(plan[k][0] in source['binaries'],'game_pin_missing')
    require(plan['control_dir']==str(E) and plan['source_dir']==str(R),'manifest_paths_mismatch')
    window_admission(read(Path(plan['window_path'])),plan['seam'])
    for name in ['STOP','START_BRIDGE','START_FARMHAND','LAUNCH_GAMES','controller-identity.json','controller-manifest.json','watchdog-manifest.json','watchdog-ready.json','operation-status.json','owned-children.json','operator-approval.json','phase2-admission.json','command','journal']:
        require(not (E/name).exists(),'namespace_consumed:'+name)
    restores=read(E/'config-restores-private.json')
    for v in restores:
        require(sha(Path(v['path']))==sha(Path(v['original_path']))==v['original_sha256'],'original_config_drift')
        require(sha(Path(v['trial_path']))==v['trial_sha256'],'trial_config_drift')
        require(semantic_identity(strict_json(ipc.read_bytes(Path(v['trial_path']))))==semantic_identity(v['trial_semantics']),'trial_semantics_mismatch')
    original=read(E/'bridge-config-original.bin'); temporary=read(E/'bridge-config-trial-private.json')
    for k in ('stardew_decision_provider_kind','stardew_decision_model','stardew_decision_effort','stardew_decision_timeout_seconds'):
        require(original.get(k)==temporary.get(k),'provider_profile_changed:'+k)
    after=inventory(read(E/'save-summary.json')['root'],True)
    require(after==read(E/'all-saves-prehash-private.json'),'protected_save_drift')
    journal={str(p.relative_to(R)):dict(sha256=sha(p),bytes=p.stat().st_size) for p in (R/'data/stardew').rglob('*') if p.is_file()}
    require(journal==read(E/'prior-journal-inventory.json'),'prior_journal_drift')
    require(inventory(read(E/'prior-command-root.json')['path'])==read(E/'prior-command-inventory.json'),'prior_command_drift')
    expected=read(E/'execution-identities.json')
    for k,v in expected.items(): require(sha(E/k)==v,'packet_drift:'+k)
    p=read(E/'phase2-process-preflight.json')
    require(not p['conflicts'] and not p['tcp'] and not p['udp'],'process_port_preflight_failed')
    age=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(p['checked_at'].replace('Z','+00:00'))).total_seconds()
    require(0 <= age < 60,'process_port_preflight_stale')
    fresh=process_snapshot(); own=process_identity(); parent=process_identity(os.getppid())
    conflicts=conflicting_processes(fresh,own=own,parent=parent,base_image=base,launcher_image=launcher)
    once('admission-process-snapshot.json',dict(rows=fresh,own=own,parent=parent,conflicts=conflicts))
    require(not conflicts,'CONFLICTING_PROCESS')
    pins=[process_identity(v['ProcessId']) for v in p['unrelated'] if v['Name'].lower() in ('explorer.exe','steam.exe','codex.exe')]
    once('phase2-unrelated-pins.json',pins); once('phase2-saves-prehash-private.json',after)
    event('fresh_revalidation',hashes=expected,source=source['source'],save_files=len(after),source_config_saves_journal_unchanged=True,unrelated_pins=pins)
    return expected

def register_games(expected, end):
    plan=read(E/'plan-manifest.json'); b=binding()
    def registered():
        children=maybe('owned-children.json')
        return children if children and set(children)==expected else None
    children=waitfor(registered,end)
    child_ids={k:process_identity(pid) for k,pid in children.items()}
    parents={v['pid']:v for v in process_snapshot()}
    for key,v in child_ids.items():
        require(parents[v['pid']]['parent']==b['controller']['pid'], 'game_parent_mismatch')
        require(same_image(v['image'],plan[key][0]), 'game_image_mismatch')
    require(binding()==b, 'game_registration_binding_changed')
    event('games_registered',children=child_ids,parent_chain=[parents[v['pid']] for v in child_ids.values()])
    return child_ids


def start():
    assert not F.exists(),'ADMISSION_ALREADY_ATTEMPTED'
    expected=preflight()
    plan=read(E/'plan-manifest.json'); n=read(E/'namespace.json')
    once('operator-approval.json',read(E/'reviewed-source.json'))
    restores=read(E/'config-restores-private.json')
    once('controller-manifest.json',plan)
    t0=time.monotonic(); utc=datetime.datetime.now(datetime.timezone.utc); deadline=t0+min(570,window_remaining(read(Path(plan['window_path']))))
    once('phase2-admission.json',dict(run_id=n['run_id'],phase2_authorized=True,t0_monotonic=t0,t0_utc=utc.isoformat(),deadline_monotonic=deadline,deadline_utc=(utc+datetime.timedelta(seconds=deadline-t0)).isoformat(),dispatch='1000000001',namespace_previously_unconsumed=True,max_attempts=1))
    reserve_child(plan, n['run_id'])
    event('controller_spawn_admission',t0=t0,deadline=deadline)
    with (E/'controller.log').open('xb') as f:
        controller=subprocess.Popen(plan['controller'],cwd=R,stdout=f,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    ident=waitfor(lambda:maybe('controller-identity.json'),t0+5)
    assert ident==process_identity(controller.pid)
    once('watchdog-manifest.json',dict(run_id=n['run_id'],control_dir=str(E),containment_id=n['containment_id'],deadline_monotonic=deadline,processes=[ident],config_restores=restores))
    stamp=time.monotonic()
    write(E,'operator-heartbeat.json',dict(run_id=n['run_id'],monotonic=stamp))
    write(E,'heartbeat.json',dict(run_id=n['run_id'],monotonic=stamp,operation=None))
    with (E/'watchdog.log').open('xb') as f:
        watchdog=subprocess.Popen(plan['watchdog'],cwd=R,stdout=f,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    ready=waitfor(lambda:maybe('watchdog-ready.json'),t0+18)
    assert ready['controller']==ident and ready['watchdog']==process_identity(watchdog.pid)
    assert ready['pinned_processes']==1 and ready['deadline_monotonic']==deadline
    cr=waitfor(lambda:maybe('controller-ready.json'),t0+18)
    assert cr['contained'] is True and cr['run_id']==n['run_id'] and cr['pid']==ident['pid'] and cr['deadline_monotonic']==deadline
    waitfor(lambda:maybe('watchdog-operation.json'),t0+18)
    event('containment_acknowledged',ready=ready,controller_ready=cr)
    s=admit('ui01-launch-title')
    for v in restores:
        target=Path(v['path']); trial=Path(v['trial_path']); original=Path(v['original_path'])
        assert sha(target)==sha(original)==v['original_sha256']
        assert sha(trial)==v['trial_sha256'] and not (E/'STOP').exists()
        temporary=target.with_name(target.name+'.startup-only-install.tmp')
        with temporary.open('xb') as f:
            f.write(ipc.read_bytes(trial));f.flush();os.fsync(f.fileno())
        os.replace(temporary,target)
        assert sha(target)==v['trial_sha256']
        event('temporary_config_installed',path=str(target),sha256=v['trial_sha256'])
    assert binding()['controller']==ident and time.monotonic()<s['operation']['deadline_monotonic']
    (E/'LAUNCH_GAMES').touch(exist_ok=False)
    event('LAUNCH_GAMES')
    expected={'host'} if game_launch_order(plan)=='host_first' else {'host','farmhand'}
    child_ids=register_games(expected,min(time.monotonic()+8,s['operation']['deadline_monotonic']-3))
    print(json.dumps(dict(ready=ready,operation=s,children=child_ids)))
def status():
    names=['phase2-admission.json','watchdog-ready.json','controller-ready.json','owned-children.json','operation-status.json','operator-heartbeat.json','heartbeat.json','watchdog-operation.json','controller-result.json','watchdog-result.json','operator-result.json']
    out={k:maybe(k) for k in names}; out['now']=time.monotonic();out['STOP']=(E/'STOP').exists()
    configs=[]
    for v in read(E/'config-restores-private.json'):
        p=Path(v['path']); data=ipc.read_bytes(p)
        configs.append(dict(path=str(p),sha256=hashlib.sha256(data).hexdigest(),matches_original=sha(p)==v['original_sha256'],matches_trial=sha(p)==v['trial_sha256'],semantic_equal_trial=semantic_identity(strict_json(data))==semantic_identity(v['trial_semantics'])))
    out['config_observations']=configs
    event('read_only_status',evidence=out)
    print(json.dumps(out))
if __name__=='__main__':
    try:
        cmd=sys.argv[1]
        configure(sys.argv[2])
        if cmd=='start': start()
        elif cmd=='transition':
            complete(sys.argv[3]); print(json.dumps(admit(sys.argv[4])))
        elif cmd=='status': status()
        elif cmd=='stop':
            event('operator_terminal_stop',reason=sys.argv[3]);(E/'STOP').touch(exist_ok=True)
        else: raise ValueError('UNKNOWN_OPERATION')
    except BaseException as exc:
        if E is None: raise
        event('local_transaction_failure',error_type=type(exc).__name__,reason=str(exc))
        if (E/'controller-identity.json').exists(): (E/'STOP').touch(exist_ok=True)
        raise
