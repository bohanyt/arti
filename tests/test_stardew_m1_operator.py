from scripts.stardew_m1_operator import conflicting_processes, validate_ceiling
import pytest

BASE=r'C:\Python\python.exe'
VENV=r'C:\repo\venv\Scripts\python.exe'
def ident(pid,image): return dict(pid=pid,image=image,created_filetime=pid*1000)
def row(pid,parent,exe): return dict(pid=pid,parent=parent,exe=exe)

def test_exact_venv_redirector_parent_is_not_an_unrelated_python():
    rows=[row(20,10,'python.exe'),row(10,8,'python.exe'),row(8,1,'powershell.exe')]
    assert conflicting_processes(rows,own=ident(20,BASE),parent=ident(10,VENV),base_image=BASE,launcher_image=VENV)==[]

def test_unrelated_python_and_games_remain_conflicts():
    rows=[row(20,10,'python.exe'),row(10,8,'python.exe'),row(30,8,'python.exe'),row(40,8,'Stardew Valley.exe'),row(50,8,'StardewModdingAPI.exe')]
    assert [v['pid'] for v in conflicting_processes(rows,own=ident(20,BASE),parent=ident(10,VENV),base_image=BASE,launcher_image=VENV)]==[30,40,50]

def test_wrong_parent_image_is_not_exempted():
    rows=[row(20,10,'python.exe'),row(10,8,'python.exe')]
    assert conflicting_processes(rows,own=ident(20,BASE),parent=ident(10,r'C:\other\python.exe'),base_image=BASE,launcher_image=VENV)==[rows[1]]

def test_parent_identity_must_match_actual_process_tree():
    with pytest.raises(ValueError,match='caller_parent_mismatch'):
        conflicting_processes([row(20,99,'python.exe')],own=ident(20,BASE),parent=ident(10,VENV),base_image=BASE,launcher_image=VENV)

def test_caller_cannot_claim_a_different_interpreter():
    with pytest.raises(ValueError,match='caller_image_mismatch'):
        conflicting_processes([row(20,10,'python.exe')],own=ident(20,r'C:\other\python.exe'),parent=ident(10,VENV),base_image=BASE,launcher_image=VENV)

@pytest.mark.parametrize('requested',[0,-1,True,float('nan'),601,3000])
def test_whole_run_ceiling_rejects_invalid_or_unbounded_values(requested):
    with pytest.raises(ValueError): validate_ceiling(requested,maximum=600)

def test_lower_whole_run_deadline_is_retained():
    assert validate_ceiling(570,maximum=600)==570


@pytest.mark.parametrize('receipt',[None,{}, {'authority_url':'wrong','seam':'reviewed_ordinary_m1','max_attempts':3}])
def test_third_same_seam_requires_exact_owner_extension(monkeypatch,receipt):
    import scripts.stardew_m1_operator as op
    monkeypatch.setattr(op,'window_remaining',lambda _:100)
    window={'children':[dict(seam='reviewed_ordinary_m1',postflight_complete=True) for _ in range(2)],
            'owner_same_seam_extension':receipt}
    with pytest.raises(ValueError,match='afk_child_limit'):
        op.window_admission(window,'reviewed_ordinary_m1')


@pytest.mark.parametrize('count,resolved,allowed',[(2,True,True),(3,True,False),(2,False,False)])
def test_exact_owner_extension_is_one_extra_child_not_a_budget_reset(monkeypatch,count,resolved,allowed):
    import scripts.stardew_m1_operator as op
    monkeypatch.setattr(op,'window_remaining',lambda _:100)
    window={'children':[dict(seam='reviewed_ordinary_m1',postflight_complete=resolved) for _ in range(count)],
            'owner_same_seam_extension':dict(authority_url='https://github.example.invalid/owner/private-lab-repository/issues/example-c',
                seam='reviewed_ordinary_m1',max_attempts=3)}
    if allowed:op.window_admission(window,'reviewed_ordinary_m1')
    else:
        with pytest.raises(ValueError):op.window_admission(window,'reviewed_ordinary_m1')


def completion_window(count=3):
    return dict(children=[dict(run_id=f'prior-{i}', seam='reviewed_ordinary_m1',
        postflight_complete=True, reserved=dict(game_seconds=600,autonomous_seconds=300,
            provider=24,continuation=24,physical=48)) for i in range(count)],
        owner_m1_completion_extension=dict(
            authority_url='https://github.example.invalid/owner/private-lab-repository/issues/example-d',
            seam='reviewed_ordinary_m1',prior_children=3,max_attempts=5))


@pytest.mark.parametrize('count,allowed',[(0,True),(2,False),(3,True),(4,True),(5,False)])
def test_completion_extension_has_finite_total_ceiling(monkeypatch,count,allowed):
    import scripts.stardew_m1_operator as op
    monkeypatch.setattr(op,'window_remaining',lambda _:100)
    if allowed: op.window_admission(completion_window(count),'reviewed_ordinary_m1')
    else:
        with pytest.raises(ValueError,match='afk_child_limit'):
            op.window_admission(completion_window(count),'reviewed_ordinary_m1')


@pytest.mark.parametrize('fault',['missing','url','seam','maximum','prior_count','prior_seam','unresolved'])
def test_completion_extension_cannot_change_scope_or_skip_cleanup(monkeypatch,fault):
    import scripts.stardew_m1_operator as op
    monkeypatch.setattr(op,'window_remaining',lambda _:100)
    window=completion_window()
    receipt=window['owner_m1_completion_extension']
    if fault=='missing': window.pop('owner_m1_completion_extension')
    elif fault=='url': receipt['authority_url']='wrong'
    elif fault=='seam': receipt['seam']='different'
    elif fault=='maximum': receipt['max_attempts']=6
    elif fault=='prior_count': receipt['prior_children']=0
    elif fault=='prior_seam': window['children'][0]['seam']='different'
    else: window['children'][-1]['postflight_complete']=False
    with pytest.raises(ValueError): op.window_admission(window,'reviewed_ordinary_m1')


def test_completion_reservation_preserves_every_prior_entry(tmp_path,monkeypatch):
    import json
    import scripts.stardew_m1_operator as op
    monkeypatch.setattr(op,'window_remaining',lambda _:100)
    window=completion_window()
    path=tmp_path/'window.json';path.write_text(json.dumps(window))
    op.reserve_child(dict(window_path=str(path),seam='reviewed_ordinary_m1'),'fresh-fourth')
    result=json.loads(path.read_text())
    assert result['children'][:3]==window['children']
    assert len(result['children'])==4
    assert result['children'][3]['reserved']==window['children'][0]['reserved']
    assert result['children'][3]['postflight_complete'] is False
    with pytest.raises(ValueError,match='prior_child_unresolved'):
        op.reserve_child(dict(window_path=str(path),seam='reviewed_ordinary_m1'),'fresh-fifth')
