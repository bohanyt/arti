"""Local finite observer for CT1000000001; production decisions remain untouched."""
from pathlib import Path
import asyncio,dataclasses,json,time,os
from collections.abc import Mapping
OWNER='Gather wood from one observed permitted nearby tree, then store the newly obtained wood in an observed permitted chest.'
def encode(x):
    if dataclasses.is_dataclass(x): return {f.name:encode(getattr(x,f.name)) for f in dataclasses.fields(x) if f.name!='approach_controller'}
    if isinstance(x,Mapping):return {str(k):encode(v) for k,v in x.items()}
    if isinstance(x,(tuple,list,set)):return [encode(v) for v in x]
    if x is None or isinstance(x,(str,int,float,bool)):return x
    return str(x)

def finish_observations(goal):
    episode=getattr(goal,'activity_episode',None)
    return tuple(v for v in (goal.last_finish,episode.outcome if episode is not None else None) if v is not None)

def paired_deposit_candidate(goal, wood_gain):
    """Candidate only: raw tree/inventory/container evidence still needs review."""
    finishes=finish_observations(goal)
    transfer=goal.transfer_progress or {}
    requested=transfer.get('requested')
    gather=goal.gather_progress
    return (not any(v.status=='uncertain' for v in finishes)
        and any(v.status=='achieved' and v.reason=='observed_paired_transfer_objective' for v in finishes)
        and transfer.get('status')=='PAIRED_TRANSFER'
        and transfer.get('qualified_item_id')=='(O)388'
        and type(requested) is int and 0 < requested <= wood_gain
        and gather is not None and bool(gather.one_tree_effect)
        and requested==gather.deposit_quantity
        and transfer.get('actor_count')==gather.starting_count
        and transfer.get('transferred')==requested and bool(transfer.get('request_ids')))
def prepare(E,cfg,pre,ns,sha):
    from scripts.stardew_m1_trial import ENVELOPE,identity
    from scripts.stardew_trial_controller import write
    from arti_stardew_command import baca_sesi
    live=baca_sesi(cfg['stardew_command_dir'])
    assert live.world_ready and not live.is_main_player and not live.is_split_screen
    assert not live.input_busy and not live.inflight_request_id
    for key in ('world_press_enabled','use_tool_enabled','select_item_enabled','world_interaction_enabled','container_transfer_enabled','container_exit_enabled'):
        assert live.mentah.get(key) is True,'NATIVE_GATE_UNAVAILABLE:'+key
    dll=next(Path(p) for p in pre['binaries'] if p.endswith('ArtiStardewBridge.dll'))
    actual=identity(root=Path(pre['checkout']),artifact=dll)
    assert actual['source']==pre['source'] and actual['tree']==pre['tree'] and not actual['dirty']
    assert not (E/'approval-private.json').exists()
    grant=dict(envelope=ENVELOPE,status='APPROVED',owner_resolution_url='https://github.example.invalid/owner/private-lab-repository/issues/example-e',modes=['bridge'],identity=actual,profile={k:cfg[k] for k in ('stardew_decision_provider_kind','stardew_decision_model','stardew_decision_effort')},session=live.session_id,issued_at=time.time(),expires_at=time.time()+340,save_resolved=True,limits={'provider':24,'input':48,'seconds':300,'steps':24},journal_path=str(E/'journal'),config_sha256=sha(Path(pre['checkout'])/'config_local.json'),bridge={'owner_ingress':'existing_ptt_asr','ordinary_voice_authorized':True,'gameplay_budget_scope':'stardew_only','retain_raw_audio':False,'retain_transcript':False})
    write(E,'approval-private.json',grant)
    for suffix,value in {'APPROVAL':E/'approval-private.json','CONFIG':Path(pre['checkout'])/'config_local.json','ARTIFACT':dll,'REPORT':E/'bridge-trial-report.json'}.items():os.environ['ARTI_STARDEW_BRIDGE_TRIAL_'+suffix]=str(value)

async def gameplay(E,bridge,runtime,cfg,result,supervision,ready,task,ingress):
    from scripts.stardew_trial_controller import write
    from arti_stardew_command import baca_sesi
    from scripts.stardew_m1_operator import configure,complete,admit
    configure(E)
    trial=bridge._stardew_bridge_trial
    assert trial and trial.runtime is runtime and not trial.stop_requested
    write(E,'bridge-readiness.json',encode(result))
    complete('bridge01-readiness'); op=admit('auto01-m1')
    injected=time.monotonic();supervision['autonomous_started']=injected
    # The observer thread is the sole heartbeat publisher. Updating this
    # shared scalar cannot race a second fixed-.tmp rename or regress a stamp.
    before=runtime.state_store.latest(result['player'])
    pin=(before.player_id,before.screen_id,before.year,before.season,before.day)
    def capture():
        return encode(dict(monotonic=time.monotonic(),status=runtime.status,goal=runtime.active_goal,state=runtime.state_store.latest(result['player']),observations={d:runtime.observation_store.get(result['player'],d) for d in ('scene.current_location','progress.inventory','progress.player','ui.context','ui.active_menu')},provider=runtime.decision_provider.physical_requests,inputs=runtime.physical_action_publications))
    write(E,'gameplay-before-private.json',capture())
    result.update(result='BLOCKED',reason='M1_NOT_COMPLETED',owner_ingress=1,ingress_provenance='injected_text_existing_ptt_queue_not_physical_mic')
    ingress.armed=True
    accepted=bridge.queue_voice_trigger(OWNER,trigger_type='ptt')
    write(E,'owner-ingress.json',dict(run_id=E.name,text=OWNER,count=1,accepted=accepted,monotonic=injected,operator_target_hints=False))
    assert accepted,'OWNER_QUEUE_REJECTED'
    end=min(ready['deadline_monotonic']-3,op['operation']['deadline_monotonic']-3,injected+297)
    saw_goal=False
    wood_gain=0
    try:
        while time.monotonic()<end:
            assert not (E/'STOP').exists(),'STOP'
            if task.done(): task.result();raise ValueError('BRIDGE_EXITED')
            assert bridge._stardew_runtime is runtime and trial.runtime is runtime,'RUNTIME_IDENTITY_CHANGED'
            assert not trial.stop_requested,trial.stop_reason or 'TRIAL_STOP_REQUESTED'
            live=baca_sesi(cfg['stardew_command_dir'])
            assert (live.session_id,live.player_id,live.screen_id)==(result['native_session'],result['player'],result['screen']),'NATIVE_IDENTITY_CHANGED'
            state=runtime.state_store.latest(result['player'])
            assert state and (state.player_id,state.screen_id,state.year,state.season,state.day)==pin,'ACTOR_OR_DATE_CHANGED'
            assert time.monotonic()-state.diterima_pada<3,'RECEIVER_STALE'
            assert state.time_of_day<1800,'SAFE_CLOCK_LIMIT'
            assert not state.active_menu or 'ItemGrabMenu' in state.active_menu,'UNEXPECTED_MENU'
            assert runtime.status not in ('FAIL_CLOSED','OFF'),'RUNTIME_'+runtime.status
            goal=runtime.active_goal;saw_goal=saw_goal or goal is not None
            result.update(provider_requests=runtime.decision_provider.physical_requests,physical_inputs=runtime.physical_action_publications,
                decisions_reserved=trial.budget.decisions,
                continuation_decisions=max(result.get('continuation_decisions',0),getattr(goal,'continuation_decisions',0)),
                autonomous_seconds=time.monotonic()-injected)
            with (E/'observations-private.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(capture())+'\n')
            if goal:
                progress=goal.gather_progress
                if progress and progress.qualified_item_id=='(O)388':
                    wood_gain=max(wood_gain,progress.current_count-progress.starting_count)
                    if progress.one_tree_effect:
                        wood_gain=max(wood_gain,progress.deposit_quantity)
                    result['observed_wood_gain_peak']=wood_gain
                assert not goal.gather_uncertain,'UNCERTAIN_GATHER'
                assert not (goal.last_action and goal.last_action.world_effect_status=='uncertain'),'UNCERTAIN_ACTION'
                assert not goal.no_progress_blocked,'NO_PROGRESS_BLOCKED'
                finishes=finish_observations(goal)
                if finishes:
                    write(E,'finish-evidence.json',encode(goal))
                    if any(v.status=='uncertain' for v in finishes):raise ValueError('UNCERTAIN_FINISH')
                    if paired_deposit_candidate(goal,wood_gain):
                        result.update(result='COMPLETION_OBSERVED_REQUIRES_POSTCHECK',reason='SEMANTIC_COMPLETION');return
                assert not (goal.last_action and goal.last_action.lifecycle!='completed'),'ACTION_NOT_COMPLETED'
            if saw_goal and goal is None:raise ValueError('GOAL_ENDED_REQUIRES_EVIDENCE_REVIEW')
            if result['provider_requests']>=24 or result['physical_inputs']>=48 or trial.budget.decisions>=24:raise ValueError('BUDGET_EXHAUSTED')
            await asyncio.sleep(.2)
        raise ValueError('BOUNDED_M1_DEADLINE')
    except BaseException as exc:
        result.update(result='BLOCKED',reason=str(exc) or type(exc).__name__)
        raise
    finally:
        # Main-loop provider work may have blocked this observer between polls.
        # Preserve the pre-stop world evidence; its write cannot prevent cleanup.
        try:
            write(E,'runtime-final-private.json',capture())
        finally:
            trial.request_stop(result['reason'])
            try:
                trial.stop(result['reason'])
            finally:
                result.update(provider_requests=runtime.decision_provider.physical_requests,
                    physical_inputs=runtime.physical_action_publications,
                    decisions_reserved=trial.budget.decisions,
                    continuation_decisions=max(result.get('continuation_decisions',0),getattr(runtime.active_goal,'continuation_decisions',0)),
                    autonomous_seconds=time.monotonic()-injected)
