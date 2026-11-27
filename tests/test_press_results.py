"""Per-spot count of repeated identical press answers and facts.hasil_berulang."""
import inspect
import json
from types import SimpleNamespace as NS

import arti_stardew_skills as S
from arti_runtime import loop_facts, page_fact_selection as PFS, press_results as PR, raw_hands

LOC, TILE, CODE_REF, DATE1, DATE2 = "RoomA", (32, 16), "map-code:" + "2" * 20, (2, "spring", 11), (2, "spring", 12)


def _base(loc=LOC, tile=(32, 17)):
    return dict(inventory={"(T)Hoe": 1}, money=500, location=loc, location_id=loc, crops={}, tile=list(tile))


def _run(arg, tile=TILE, *, changes=(), episode=None, pending_tile=TILE, menu_opened=False):
    st = {**({"generic_pending_args": dict(kind="generic_action", tile_x=pending_tile[0], tile_y=pending_tile[1])} if pending_tile else {}),
          **({"generic_readback": dict(before_tick=100, after_tick=120, changes=list(changes))} if changes is not None else {}),
          **({"native_ui_episode": episode} if episode is not None else {}), **({"generic_menu_opened": True} if menu_opened else {})}
    return NS(call=S.SkillCall("aksi", arg, tile=None if arg == "depan" else tile), state=st)


def _menu(mtype, rcv, text="", *, unreadable=False, state="s1"):
    flag = dict(unreadable=True) if unreadable else {}
    return ([dict(path="menu", before=dict(present=True, value=None), after=dict(present=True, value=dict(type=mtype, receiver=rcv, **flag)))],
            dict(opened=dict(receiver=rcv, menu=mtype), observations=[dict(receiver=rcv, menu=mtype, text=text, state=state, linked=True, **flag)], inputs=[]))


def test_depan_and_map_code_share_spot_and_survive_step_turn_room_night():
    from arti_stardew_live7 import DayPolicy
    from arti_stardew_part_a import Campaign
    from scripts.m1_operator import live_cycle

    store, no_fx = {}, {}
    c1, e1 = _menu("Menu", "rcv:mi9", "Simbol tidak terbaca.", unreadable=True, state="s1")
    c2, e2 = _menu("Menu", "rcv:mi10", "Simbol   tidak terbaca.", unreadable=True, state="s2")
    r1, r2 = _run(CODE_REF, changes=c1, episode=e1), _run("depan", changes=c2, episode=e2)
    assert PR.note(store, r1, category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=910) == (LOC, TILE)
    assert PR.note(store, r2, category="VERIFIED", before=_base(tile=(31, 16)), after=_base(tile=(31, 16)), location=LOC, items=set(), date=DATE2, clock=1430) == (LOC, TILE)
    for clk in (910, 920):
        loop_facts.note(no_fx, "aksi", CODE_REF, effect=False, reason="observed_menu_open", clock=clk)
        PR.tag_no_effect(no_fx, r1.call, (LOC, TILE))
    loop_facts.note_step(no_fx, "down", dict(kind=raw_hands.MOVED, before=dict(location_id=LOC, pixel=(10.0, 10.0))), target=None, clock=930, keep_presses=True)
    assert f"aksi {CODE_REF}" in no_fx
    assert PR.note(store, NS(call=S.SkillCall("hadap", "32,16"), state={}), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=940) is None
    assert PR.note(store, NS(call=S.SkillCall("pergi_ke", "other"), state={}), category="VERIFIED", before=_base(), after=_base("RoomB"), location="RoomB", items=set(), date=DATE1, clock=950) is None
    camp = Campaign("main", DATE1, {}, ("s", "f", 2, "spring", 11), part="live", live=DayPolicy(press_results=dict(store)))
    camp.morning()
    row = camp.live.press_results[f"{LOC}:32,16"]
    assert row["count"] == 2 and row["since_day"] == "Y2 spring 11" and row["since_clock"] == 910 and row["summary"] == "menu yang teksnya tidak terbaca"
    src = inspect.getsource(live_cycle)
    assert 'row.get("ev") == "live7_press_results"' in src and "press_results=presses" in src


def test_readable_change_native_deltas_unknown_read_and_bound_24():
    store = {}
    c1, e1 = _menu("Menu", "rcv:1", "???", unreadable=True)
    for clk in (910, 920):
        PR.note(store, _run(CODE_REF, changes=c1, episode=e1), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=clk)
    assert store[f"{LOC}:32,16"]["count"] == 2
    c2, e2 = _menu("Board", "rcv:2", "Teks terbaca")
    PR.note(store, _run(CODE_REF, changes=c2, episode=e2), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=930)
    assert store[f"{LOC}:32,16"]["count"] == 1 and store[f"{LOC}:32,16"]["summary"] == "menu yang isinya sama"
    for clk in (940, 950):
        PR.note(store, _run("depan", changes=[]), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=clk)
    assert store[f"{LOC}:32,16"]["count"] == 2 and store[f"{LOC}:32,16"]["summary"] == "tidak ada yang berubah"
    q = [dict(path="quest:q1", before=dict(present=False, value=None), after=dict(present=True, value=dict(active=True)))]
    for items, changes, after in (({"(O)16"}, [], _base()), (set(), q, _base()), (set(), [], dict(_base(), money=510))):
        for n in (1, 2):  # a press that changed something is nothing that stayed the same, also twice
            PR.note(store, _run("depan", changes=changes), category="VERIFIED", before=_base(), after=after, location=LOC, items=items, date=DATE1, clock=960)
            assert f"{LOC}:32,16" not in store
        for n in (1, 2):
            PR.note(store, _run("depan", changes=[]), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=970)
            assert store[f"{LOC}:32,16"]["count"] == n
    assert PR.note(store, _run("xt:bin:RoomA:32,16", changes=None), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=975) is None  # no read-back: unknown
    assert PR.note(store, _run("depan", changes=[]), category="VERIFIED", before=_base(), after=None, location=LOC, items=set(), date=DATE1, clock=980) is None
    assert PR.note(store, _run("depan", pending_tile=None, changes=[]), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=990) is None
    assert PR.note(store, _run("obj:RoomA:32,16:x", changes=None), category="CLEAN_FAIL", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=1000) is None
    assert PR.note(store, _run("obj:RoomA:32,16:x", changes=None, menu_opened=True), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=1010) == (LOC, TILE)
    assert store[f"{LOC}:32,16"]["summary"] == "menu yang sama"  # opened, its words not captured
    for i in range(28):
        PR.note(store, _run("depan", pending_tile=(i, 4), changes=[]), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=600 + i)
    assert len(store) == PR.MAX_SPOTS == 24 and f"{LOC}:0,4" not in store and f"{LOC}:27,4" in store
    raw = PR.export(store)
    raw["bad"] = dict(location=LOC, tile=[1, 2], digest="x", summary="y", count=True, since_day="", since_clock=600)
    assert len(PR.load(raw)) == 24 and "bad" not in PR.load(raw)


def test_page_line_bound_and_dedup_over_budget():
    store = {}
    c1, e1 = _menu("Menu", "rcv:1", "???", unreadable=True)
    for clk in range(910, 980, 10):
        PR.note(store, _run(CODE_REF, changes=c1, episode=e1), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=clk)
    for clk in (1000, 1010, 1020):
        PR.note(store, _run("depan", pending_tile=(10, 10), changes=[]), category="VERIFIED", before=_base(), after=_base(), location=LOC, items=set(), date=DATE1, clock=clk)
    snap = NS(motor=NS(tile=(32, 17), facing=0), generic_world_targets=[dict(kind="generic_action", tile_x=32, tile_y=16)])
    line, named = PR.page_fact(store, facts=NS(location=LOC, menu_open=False), context=dict(location_id=LOC), snapshot=snap, calls=[S.SkillCall("aksi", "depan")])
    assert line.startswith("petak 32,16: ditekan 7x sejak Y2 spring 11 09:10, jawabannya selalu sama: menu yang teksnya tidak terbaca.")
    assert len(line.encode("utf-8")) <= PR.MAX_BYTES and (LOC, TILE) in named and PR.KEY in PFS.PROTECTED and PR.KEY in PFS.WORLD_ROWS and PR.KEY not in PFS.NEVER_CUT and PR.KEY in S.MINIMAL_PAGE_FACTS
    body = dict(mind=dict(rencana_hari_ini="p" * 80), skills=[dict(name="pergi_ke", args=["town"])], facts={"hp": "100/100", "di_sini": "x" * 180, PR.KEY: line}, day=dict(clock=1000))
    limit = len(json.dumps(body, ensure_ascii=False).encode()) - 1
    cut, counts = PFS.cut_to_fit(body, lambda b: len(json.dumps(b, ensure_ascii=False).encode()) <= limit)
    assert PR.KEY not in cut["facts"] and cut["facts"]["hp"] == "100/100" and cut["mind"] == body["mind"] and cut["skills"] == body["skills"] and counts["baris dunia"] >= 1
