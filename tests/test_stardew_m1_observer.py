import asyncio
from types import SimpleNamespace as N
import pytest
from scripts.stardew_m1_observer import OneOwnerIngress,readiness_deadline
from scripts.stardew_m1_supervisor import OWNER,paired_deposit_candidate,gameplay

def test_readiness_clips_to_operation_and_absolute_deadlines():
    status={'outcome':'active','operation_id':'bridge1','operation':{'deadline_monotonic':145}}
    assert readiness_deadline(status,'bridge1',999)==142
    assert readiness_deadline(status,'bridge1',130)==130

@pytest.mark.parametrize('outcome,operation',[('success','bridge1'),('active','other'),('failure','bridge1')])
def test_readiness_does_not_accept_wrong_or_completed_lease(outcome,operation):
    with pytest.raises(ValueError):readiness_deadline(dict(outcome=outcome,operation_id=operation),'bridge1',999)

def test_only_one_exact_post_gate_owner_goal_reaches_real_queue():
    calls=[]; rejected=[]
    ingress=OneOwnerIngress(lambda *a,**kw:calls.append((a,kw)) or True,OWNER,lambda:rejected.append(True))
    assert not ingress(OWNER,trigger_type='ptt')
    ingress.armed=True
    assert not ingress('go to tree 5',trigger_type='ptt')
    assert not ingress(OWNER,trigger_type='mic')
    assert ingress(OWNER,trigger_type='ptt')
    ingress.armed=True
    assert not ingress(OWNER,trigger_type='ptt')
    assert len(calls)==1 and len(rejected)==4

def goal():
    return N(last_finish=N(status='achieved',reason='observed_paired_transfer_objective'),
        gather_progress=N(one_tree_effect={'request_id':'tree'},deposit_quantity=2,starting_count=7),
        transfer_progress=dict(status='PAIRED_TRANSFER',qualified_item_id='(O)388',requested=2,transferred=2,request_ids=['one'],actor_count=7))

def test_old_wood_or_inventory_only_finish_cannot_complete_m1():
    g=goal()
    assert not paired_deposit_candidate(g,0)
    assert paired_deposit_candidate(g,2)
    g.last_finish.reason='observed_inventory_objective'
    assert not paired_deposit_candidate(g,2)

@pytest.mark.parametrize('change',[{'transferred':1},{'qualified_item_id':'(O)390'},{'request_ids':[]},{'requested':True},{'actor_count':6},{'actor_count':8}])
def test_partial_or_unbound_transfer_is_not_completion(change):
    g=goal();g.transfer_progress.update(change)
    assert not paired_deposit_candidate(g,5)

@pytest.mark.parametrize('mode,reason',[('mutation','UNCERTAIN_GATHER'),('activity_uncertain','UNCERTAIN_FINISH'),('activity_achieved','SEMANTIC_COMPLETION'),('mixed_uncertain','UNCERTAIN_FINISH'),('mixed_achieved','SEMANTIC_COMPLETION'),('early_stop','inspect_unavailable')])
def test_terminal_evidence_stops_without_second_goal_or_replay(tmp_path,monkeypatch,mode,reason):
    import scripts.stardew_m1_supervisor as sup
    import scripts.stardew_m1_operator as op
    import arti_stardew_command as command
    transitions=[];stops=[];calls=[]
    monkeypatch.setattr(op,'configure',lambda folder:None)
    monkeypatch.setattr(op,'complete',lambda key:transitions.append(('complete',key)))
    monkeypatch.setattr(op,'admit',lambda key:transitions.append(('admit',key)) or {'operation':{'deadline_monotonic':400}})
    monkeypatch.setattr(sup.time,'monotonic',lambda:100)
    monkeypatch.setattr(command,'baca_sesi',lambda _:N(session_id='s',player_id=2,screen_id=0))
    state=N(player_id=2,screen_id=0,year=1,season='summer',day=27,diterima_pada=100,time_of_day=600,active_menu=None)
    g=N(gather_progress=None,gather_uncertain=True,last_action=None,no_progress_blocked=False,last_finish=None)
    if mode!='mutation':
        g=goal();g.last_finish=None
        g.gather_progress.qualified_item_id='(O)388';g.gather_progress.current_count=7
        g.gather_uncertain=False;g.last_action=None;g.no_progress_blocked=False
        g.activity_episode=N(outcome=N(status='achieved' if mode=='activity_achieved' else 'uncertain',reason='observed_paired_transfer_objective'))
        if mode.startswith('mixed'):
            g.activity_episode.outcome.status='partial'
            g.last_finish=N(status='achieved' if mode=='mixed_achieved' else 'uncertain',reason='observed_paired_transfer_objective')
    runtime=N(state_store=N(latest=lambda _:state),observation_store=N(get=lambda *a:None),active_goal=g,status='READY',decision_provider=N(physical_requests=1),physical_action_publications=1)
    trial=N(runtime=runtime,stop_requested=False,budget=N(decisions=1),request_stop=lambda reason:stops.append(('request',reason)),stop=lambda reason:stops.append(('stop',reason)))
    ingress=N(armed=False)
    bridge=N(_stardew_bridge_trial=trial,_stardew_runtime=runtime,queue_voice_trigger=lambda *a,**kw:calls.append((a,kw)) or True)
    result=dict(player=2,screen=0,native_session='s',result='READINESS_OBSERVED')
    def failed_main_loop():
        runtime.decision_provider.physical_requests=4
        runtime.physical_action_publications=2
        trial.budget.decisions=3
        g.continuation_decisions=2
        monkeypatch.setattr(sup.time,'monotonic',lambda:114)
        raise ValueError('inspect_unavailable')
    task=N(done=lambda:mode=='early_stop',result=failed_main_loop)
    def run():return asyncio.run(gameplay(tmp_path,bridge,runtime,{'stardew_command_dir':'ignored'},result,{}, {'deadline_monotonic':600},task,ingress))
    if reason=='SEMANTIC_COMPLETION':run()
    else:
        with pytest.raises((AssertionError,ValueError),match=reason):run()
    assert transitions==[('complete','bridge01-readiness'),('admit','auto01-m1')]
    assert calls==[((OWNER,),{'trigger_type':'ptt'})]
    assert stops==[('request',reason),('stop',reason)]
    assert result['result']==('COMPLETION_OBSERVED_REQUIRES_POSTCHECK' if reason=='SEMANTIC_COMPLETION' else 'BLOCKED') and result['reason']==reason
    assert (tmp_path/'runtime-final-private.json').exists()
    assert not (tmp_path/'supervisor-heartbeat.json').exists()
    if mode=='early_stop':
        assert result['provider_requests']==4 and result['physical_inputs']==2
        assert result['decisions_reserved']==3 and result['continuation_decisions']==2
        assert result['autonomous_seconds']==14
