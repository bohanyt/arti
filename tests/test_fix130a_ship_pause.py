"""ADR 228 part D: the shipping box as a reachable evening choice. Synthetic facts; attempt-85 shapes."""
from types import SimpleNamespace as NS

import pytest

import arti_stardew_live7 as L
import arti_stardew_skills as S
from arti_runtime import ship_pause as P
from arti_runtime.route_pause import pause_line

HOME = S.SkillCall("pergi_ke", "home")
BAG = (("(O)390", 0, 22), ("(O)388", 0, 7), ("(O)382", 0, 3), ("(O)378", 0, 1))  # a85 S1 Y1 winter 8: stone, wood, coal, ore


def owner(on=True, **kw):
    return NS(_config={P.SETTING: True} if on else {}, _live7_route_pause_fence=lambda p, f: None,
              _live7_route_scene=lambda location: {}, **kw)


def session(date=(1, "winter", 8)):
    return NS(campaign=NS(live=L.DayPolicy(), date=date), session_id="synthetic")


def facts(**kw):
    return NS(**dict(dict(location="Farm", time_of_day=2030, store_stacks=BAG, health=100, energy=200), **kw))


def test_home_trip_entering_the_farm_with_goods_pauses_once_with_a_plain_line():
    pause = P.due(owner(), session(), HOME, facts(), "Backwoods", reserve=False)
    assert pause["box"] is True and pause["map"] == "Farm" and pause["goods_stacks"] == 4
    assert pause["destination"] == "home" and pause["villagers"] == []
    line = pause_line(pause, "Farm", True)
    assert line == ("perjalanan ke home berhenti di Farm, dengan 4 tumpuk barang di tasmu; "
                    "pergi_ke home ditawarkan: memilihnya melanjutkan perjalanan yang sama.")
    assert not any(w in line.casefold() for w in ("jual", "kirim", "bayar", "dibayar", "uang", "harus"))


@pytest.mark.parametrize("change", [
    dict(on=False), dict(call=S.SkillCall("pergi_ke", "mine")), dict(location="Mountain"), dict(previous="Farm"),
    dict(store_stacks=()), dict(time_of_day=2310),  # 23:10 (a85 S3 Y1 winter 28): a kirim no longer fits the clock
])
def test_no_pause_without_all_of_its_facts(change):
    on = change.pop("on", True)
    call = change.pop("call", HOME)
    previous = change.pop("previous", "Backwoods")
    assert P.due(owner(on), session(), call, facts(**change), previous, reserve=False) is None


def test_route_pause_due_before_and_after_the_setting():
    """The real ADR 155 decision: no villager here, so main pauses nowhere; with the setting, the box pause."""
    due = L.Live7CampaignMixin._live7_route_pause_due
    before = due(owner(False), session(), HOME, facts(), "Backwoods", reserve=False)
    after = due(owner(True), session(), HOME, facts(), "Backwoods", reserve=False)
    assert before is None and after["box"] is True
    s = session()
    s.campaign.live.route_pauses.add(((1, "winter", 8), "Farm"))  # once per map per day, like every route pause
    assert due(owner(True), s, HOME, facts(), "Backwoods", reserve=False) is None


def test_kirim_text_says_what_the_batch_does_and_nothing_about_payment():
    offer = dict(skills=[dict(name="kirim", does="Kirim satu barang yang diizinkan lewat kotak pengiriman. Kiriman dibayar besok."),
                         dict(name="tidur", does="Tidur.")])
    P.page_text(offer)
    assert offer["skills"][0]["does"] == P.DOES and "dibayar" not in P.DOES and "8 tumpuk" in P.DOES
    assert offer["skills"][1]["does"] == "Tidur."
    spec = S.REGISTRY["kirim"]
    assert "at most 8 stacks" in spec.summary and "ship n of an item" not in spec.summary
