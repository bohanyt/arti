"""The flagged mailbox beside her door, once a day, as she walks out past it. Synthetic facts only."""
import time
from types import SimpleNamespace as NS

import pytest

import arti_stardew_live7 as L
import arti_stardew_skills as S
from arti_runtime import mail_pause as P, ship_pause as B
from arti_runtime.route_pause import pause_line

TRIP = S.SkillCall("pergi_ke", "community center")
DATE = (2, "spring", 10)


BOX = dict(kind="mailbox", ref="xt:mailbox:Farm:54,16", x=54, y=16)


def frame(count=13, age=0.0, targets=(BOX,), mode="gameplay", **kw):
    return NS(**dict(dict(facts=dict(mailbox_count=count, mode=mode, extras_targets=list(targets)), received_monotonic=time.monotonic() - age,
                          available=True, stale=False, authoritative=True, requires_resync=False), **kw))


def owner(on=True, rec=None, fence=None, box=False):
    journal = []
    me = NS(_config={**({P.SETTING: True} if on else {}), **({B.SETTING: True} if box else {})}, _live7_route_pause_fence=lambda p, f: fence,
            _live7_route_scene=lambda location: {}, _binding_view=lambda: (None, 7), _obs=NS(get=lambda pid, domain: (
                rec if rec is not None else frame()) if (pid, domain) == (7, "ui.context") else None), _journal=journal.append, journal=journal)
    return me


def session(date=DATE):
    return NS(campaign=NS(live=L.DayPolicy(), date=date), session_id="synthetic", current=None)


def facts(**kw):
    return NS(**dict(dict(location="Farm", time_of_day=710, store_stacks=(), health=100, energy=200), **kw))


def due(me, s, call=TRIP, f=None, previous="Cabin_x", reserve=False):
    return L.Live7CampaignMixin._live7_route_pause_due(me, s, call, f or facts(), previous, reserve=reserve)


def test_leaving_home_past_a_flagged_mailbox_pauses_once_a_day_with_a_plain_line():
    me, s = owner(), session()
    pause = due(me, s)
    assert pause["mail"] is True and pause["map"] == "Farm" and pause["destination"] == "community center" and pause["villagers"] == []
    line = pause_line(pause, "Kebun", True)
    assert line == ("perjalanan ke community center berhenti di Kebun, kotak suratmu di dekat pintu bertanda surat; "
                    "perjalanan ke community center tetap bisa dilanjutkan setelah ini; "  # the trip is kept
                    "pergi_ke community center ditawarkan: memilihnya melanjutkan perjalanan yang sama.")
    assert not any(w in line.casefold() for w in ("buka", "baca", "harus", "13"))  # no order, no hidden count
    L.Live7CampaignMixin._live7_route_pause_start(me, s, pause)
    assert s.campaign.live.route_pause["mail"] is True and me.journal[-1]["ev"] == "live7_route_pause"
    assert due(me, s) is None
    s.campaign.date = (2, "spring", 11)
    assert due(me, s)["mail"] is True


@pytest.mark.parametrize("change", [
    dict(on=False), dict(rec=frame(0)), dict(rec=frame(None)), dict(rec=frame("13")), dict(rec=frame(age=3.0)), dict(rec=frame(stale=True)),
    dict(rec=frame(available=False)), dict(rec=frame(authoritative=False)), dict(rec=frame(requires_resync=True)), dict(rec=frame(mode="menu")),
    dict(rec=frame(targets=())), dict(rec=frame(targets=[dict(BOX, kind="shipping_bin")])), dict(rec=frame(targets=[dict(BOX, x=None)])),
    dict(fence="low_vitals"), dict(previous="BusStop"), dict(previous=None), dict(location="BusStop"),
    dict(call=S.SkillCall("pergi_ke", "home")), dict(call=S.SkillCall("pergi_ke", "farm")), dict(call=S.SkillCall("forage")),
    dict(reserve=True), dict(time_of_day=2540),
])
def test_no_pause_without_all_of_its_facts(change):
    me = owner(change.get("on", True), change.get("rec"), change.get("fence"))
    f = facts(**{k: change[k] for k in ("location", "time_of_day") if k in change})
    assert P.due(me, session(), change.get("call", TRIP), f, change.get("previous", "Cabin_x"), reserve=change.get("reserve", False)) is None


def test_a_mailbox_press_that_failed_today_is_no_reason_to_stop(monkeypatch):
    s = session()
    s.campaign.live.refs.add(BOX["ref"])  # retired for the day
    assert due(owner(), s) is None and s.campaign.live.route_pauses == set()
    for name in ("cek_pagi", "pergi_ke"):  # the press, or the trip she could not continue from the Farm
        monkeypatch.setattr(L.DayPolicy, "blocked", lambda self, call, location, *a, name=name, **k: call.name == name and location == "Farm")
        assert due(owner(), session()) is None


def test_out_of_the_hosts_door_her_own_box_is_far_away_and_the_pause_is_kept():
    s = session()
    assert due(owner(rec=frame(targets=())), s, previous="FarmHouse") is None and s.campaign.live.route_pauses == set()
    assert due(owner(), s, previous="Cabin_x")["mail"] is True


def test_the_box_on_the_way_home_still_has_its_own_pause_that_evening():
    me, s = owner(box=True), session()
    L.Live7CampaignMixin._live7_route_pause_start(me, s, due(me, s))
    evening = facts(time_of_day=2030, store_stacks=(("(O)388", 0, 7),))
    box = due(me, s, S.SkillCall("pergi_ke", "home"), evening, "Backwoods")
    assert box is not None and box.get("box") is True
    L.Live7CampaignMixin._live7_route_pause_start(me, s, box)
    assert due(me, s, S.SkillCall("pergi_ke", "home"), evening, "Backwoods") is None


def test_off_is_exactly_main():
    me, s = owner(on=False), session()
    assert due(me, s) is None and s.campaign.live.route_pauses == set() and P.enabled({}) is False
