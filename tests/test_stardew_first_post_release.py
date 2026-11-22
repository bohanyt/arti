"""Deterministic first-post-release witnesses; no scheduler timing lottery."""
import pytest
from arti_stardew import StardewStateStore
from tests.test_stardew_runtime_move import state, PID

BINDING=("command-1","runtime-1","observation-epoch-7",4)

def seeded():
    now=[10.]
    store=StardewStateStore(jam=lambda:now[0])
    store.catat(dict(ev="hello",seq=1,player_id=PID,screen_id=0,player_name="Arti",is_main_player=False,is_split_screen=False))
    store.catat(state())
    now[0]=11.
    return store,now

@pytest.mark.parametrize("overwrite",[False,True])
def test_first_post_release_interleaving_keeps_original_sample(overwrite):
    store,now=seeded()
    # Legacy fallback reproduces both loss windows on the uncorrected source.
    token=(store.arm_first_post_release(request_id="req-interleave",player_id=PID,
           screen_id=0,binding=BINDING) if hasattr(store,"arm_first_post_release") else None)
    boundary=token.boundary if token else store.latest(PID)
    now[0]=12.;store.catat(state(3,tile=(4,8)))
    first=store.latest(PID)
    if overwrite:
        now[0]=13.;store.catat(state(4,tile=(5,8)))
    if token:
        consumed=store.consume_first_post_release(token,timeout=0,max_age=2)
    else:
        # Existing runtime's second latest() advances the release boundary.
        second_boundary=store.latest(PID)
        now[0]=13.5;store.catat(state(5,tile=(6,8)))
        consumed=store.wait_next(PID,second_boundary.revisi,0)
    print(dict(request="req-interleave",identity=(PID,0,BINDING),
        release=(boundary.revisi,token.released_at if token else boundary.diterima_pada),first=(first.revisi,first.diterima_pada),
        consumed=(consumed.revisi,consumed.diterima_pada)))
    assert consumed is first
    assert consumed.revisi==boundary.revisi+1
    assert store.consume_first_post_release(token,timeout=0,max_age=2) is None

@pytest.mark.parametrize("fault",["foreign","screen","hello","stale","missing","cancel"])
def test_invalid_or_missing_capture_is_not_latest_fallback(fault):
    store,now=seeded()
    token=store.arm_first_post_release(request_id="one",player_id=PID,screen_id=0,binding=BINDING)
    now[0]=12.
    event=state(3)
    if fault=="foreign":event["player_id"]=77
    if fault=="screen":event["screen_id"]=1
    if fault not in {"missing","cancel"}:store.catat(event)
    if fault=="hello":store.catat(dict(ev="hello",seq=1,player_id=PID,screen_id=0,player_name="Arti",is_main_player=False,is_split_screen=False))
    if fault=="stale":
        now[0]=15.;store.catat(state(4))  # latest is fresh; captured witness is not
    if fault=="cancel":store.cancel_first_post_release(token)
    assert store.consume_first_post_release(token,timeout=0,max_age=.5) is None
    assert store._first_post_release is None


def test_frame_in_the_arm_clock_tick_is_the_post_release_sample():
    """Live 2026-10-06 (M1 attempts 10l, 10p): GetTickCount64 resolution is 15.6 ms; the first frame
    after arm can carry the arm's own timestamp. Arm-then-record order is the proof, not the clock."""
    store,now=seeded()
    token=store.arm_first_post_release(request_id="same-tick",player_id=PID,screen_id=0,binding=BINDING)
    store.catat(state(3))  # same clock value as the arm
    sample=store.consume_first_post_release(token,timeout=0,max_age=.5)
    assert sample is not None and sample.revisi==token.boundary.revisi+1


def test_one_active_request_and_no_cross_request_consumption():
    store,now=seeded()
    first=store.arm_first_post_release(request_id="one",player_id=PID,screen_id=0,binding=BINDING)
    with pytest.raises(ValueError,match="busy"):
        store.arm_first_post_release(request_id="two",player_id=PID,screen_id=0,binding=BINDING)
    store.cancel_first_post_release(first)
    second=store.arm_first_post_release(request_id="two",player_id=PID,screen_id=0,binding=BINDING)
    now[0]=12.;store.catat(state(3))
    assert store.consume_first_post_release(first,timeout=0,max_age=1) is None
    assert store.consume_first_post_release(second,timeout=0,max_age=1).tile==(3,8)
    assert store.latest(PID).revisi==2


def test_cancel_wakes_waiter_without_lock_inversion():
    import threading
    store,now=seeded()
    token=store.arm_first_post_release(request_id="stop",player_id=PID,screen_id=0,binding=BINDING)
    waiting=threading.Event();done=threading.Event();result=[]
    def consume():
        waiting.set();result.append(store.consume_first_post_release(token,timeout=10,max_age=1));done.set()
    thread=threading.Thread(target=consume);thread.start()
    assert waiting.wait(1)
    store.cancel_first_post_release(token)
    assert done.wait(1)
    thread.join();assert result==[None]


def test_foreign_event_before_arm_cannot_be_renumbered():
    store,now=seeded();foreign=state(3);foreign["player_id"]=77;store.catat(foreign)
    with pytest.raises(ValueError,match="boundary_unavailable"):
        store.arm_first_post_release(request_id="gap",player_id=PID,screen_id=0,binding=BINDING)
    assert store.latest(PID).revisi==1 and store.latest(77).revisi==2

from tests.test_stardew_runtime_move import rig

@pytest.mark.parametrize("fault",[None,"date","screen","stale","epoch"])
def test_runtime_uses_witness_not_overwritten_latest(rig,monkeypatch,fault):
    rt,motor,obs,raw,calls,publish,folder=rig
    request=rt._begin_generation(text="move",trigger_type="ptt",viewer_name=None,
        reason="owner_turn",replace_goal=True)
    pin=request.snapshot.movement_context
    now=[motor.latest(PID).diterima_pada+.01]
    motor._jam=lambda:now[0]
    token=motor.arm_first_post_release(request_id="move-witness",player_id=PID,screen_id=0,binding=(pin,"attempt"))
    before={"revisi":token.boundary.revisi}
    now[0]+=.01;event=state(4,tile=(4,8))
    if fault=="date":event["payload"]["date"]["day"]=2
    if fault=="screen":event["screen_id"]=1
    motor.catat(event);first=motor.latest(PID)
    now[0]+=(1. if fault=="stale" else .01);motor.catat(state(5,tile=(5,8)))
    if fault=="epoch":obs._records[(PID,"ui.context")]=__import__('dataclasses').replace(obs.get(PID,"ui.context"),session_id="new-epoch")
    monkeypatch.setattr(motor,"wait_next",lambda *a:pytest.fail("approach must not use latest-only waiting"))
    result=rt._move_readback(request,before,folder=folder,approach_witness=token)
    if fault:assert result is None
    else:
        assert result["revisi"]==first.revisi and result["tile"]==[4,8]
        assert rt.active_goal.last_effect_motor is first
    assert motor._first_post_release is None


def test_owner_stop_cancels_store_witness(rig):
    rt,motor=rig[:2]
    token=motor.arm_first_post_release(request_id="stop-me",player_id=PID,screen_id=0,binding=BINDING)
    rt.stop()
    assert motor.consume_first_post_release(token,timeout=0,max_age=1) is None
    assert motor._first_post_release is None
