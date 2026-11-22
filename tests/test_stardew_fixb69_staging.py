import json
import time
import pytest
from scripts.m1_operator import w13_preflight as gate
from scripts.m1_operator import stage_event

def receipt(location='Beach',date=(6,'summer',26)):
    identity=dict(session_id='session',runtime_instance_id='runtime',player_id=42,screen_id=0,context_generation=5,location_id=location)
    return dict(op='inspect_context',status='completed',success=True,completed_unix_ms=int(time.time()*1000),**identity,
        context=dict(**identity,mode='gameplay',is_player_free=True,can_player_move=True,animal_routine=dict(date=list(date))))

@pytest.mark.parametrize('date',[(6,'summer',26),(6,'winter',18)])
def test_stardew_w13_haley_plain_beach_valid_date(date):
    assert gate.plain_beach(receipt(date=date))==date

@pytest.mark.parametrize('fault',['night_market','festival','winter15','winter16','winter17','summer28','stale','future','identity','busy','missing_date'])
def test_stardew_w13_haley_refuses_incompatible_native_fixture_before_file_publish(tmp_path,fault):
    r=receipt()
    if fault=='night_market':r['location_id']=r['context']['location_id']='BeachNightMarket'
    elif fault=='festival':r['location_id']=r['context']['location_id']='Beach-Jellies'
    elif fault.startswith('winter'):r['context']['animal_routine']['date']=[6,'winter',int(fault[6:])]
    elif fault=='summer28':r['context']['animal_routine']['date']=[6,'summer',28]
    elif fault=='stale':r['completed_unix_ms']-=2500
    elif fault=='future':r['completed_unix_ms']+=2500
    elif fault=='identity':r['context']['session_id']='different'
    elif fault=='busy':r['context']['is_player_free']=False
    elif fault=='missing_date':r['context'].pop('animal_routine')
    p=tmp_path/'context.json';p.write_text(json.dumps(r))
    with pytest.raises(ValueError):gate.plain_beach(r)
    assert stage_event.stage('control',mod=tmp_path,trial_check=lambda:False,context_path=p)==2
    assert not (tmp_path/'operator-event-staging').exists()

def test_stardew_w13_haley_cli_requires_native_context_before_tools(tmp_path,capsys):
    assert stage_event.main(['control','--mod-dir',str(tmp_path)])==2
    assert '--context' in capsys.readouterr().err
    assert not (tmp_path/'operator-event-staging').exists()
