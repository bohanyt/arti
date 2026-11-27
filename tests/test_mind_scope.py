"""Farm-scoped mind stores, legacy sidecar, scope-map tool, active-cycle voice gating,
crash-between-writes, and two-process concurrency.

UNIT_TESTED only. Invented rows and temp folders only; never reads any real mind/vault/save file.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import arti_mind as M
import arti_mind_reflect as R
from arti_runtime import experience_digest as D
from arti_runtime import goal_ladder as G
from arti_runtime import goal_ladder_runtime as GR
from arti_runtime import mind_scope as MS
from arti_runtime import stream_notes as SN
from arti_runtime.seen_receipts import SeenReceipts
import arti_stardew_live7 as L7
import arti_stardew_skills as S
from tools import mind_scope_map

ROOT = Path(__file__).resolve().parents[1]

SCOPE_A = "900111222:1001"  # invented test farm (Winter 22 = day 106)
SCOPE_B = "900333444:2002"  # invented owner farm (Spring 13 = day 13)
HASH_A = MS.scope_hash(SCOPE_A)
HASH_B = MS.scope_hash(SCOPE_B)
WINTER_22 = 106  # Y1 winter 22
SPRING_13 = 13   # Y1 spring 13
SPRING_15 = 15   # Y1 spring 15 (same day number test)


def _reply(*, new=(), done=(), drop=(), summary="aku lanjut jalan-jalan", fun=(), not_fun=()):
    return json.dumps(dict(
        today=summary,
        tomorrow=summary,
        fun=list(fun),
        not_fun=list(not_fun),
        goals=dict(keep=[], done=list(done), drop=list(drop), progress=[], calendar_links=[], new=list(new)),
        catatan=dict(new=[], found=[]),
    ))


def _want(horizon="today", text="aku mau cek dermaga", **extra):
    return dict(horizon=horizon, text=text, done_when="", watch=None, source_note="", parent="", **extra)


def _make_runtime(base: Path, *, scope: str | None, date: tuple, config: dict | None = None):
    rt = object.__new__(L7.Live7CampaignMixin)
    cfg = dict(config or {})
    rt._config = cfg
    rt._live7_mind_base = lambda: base
    rt._live7_discovery_scope = scope
    rt._live7_native_story_scope = scope
    rt._live7_calendar = ()
    rt._live7_goal_targets = ()
    rt._live7_goal_wallet = 500
    rt._live7_goal_inventory = []
    rt._live7_day_facts = {}
    rt._live7_native_place_names = {}
    rt._live7_native_names = {}
    rt._live7_mind_whys = []
    rt._journal_rows = []
    rt._journal = rt._journal_rows.append
    rt._binding_view = lambda: (NS(status="bound"), 1)
    rt._state = NS(latest=lambda _pid: NS(
        year=date[0], season=date[1], day=date[2], time_of_day=900, location="Farm", active_menu=None,
    ))
    rt._date_tuple = lambda m: (m.year, m.season, m.day)
    rt._skill_current_facts = lambda: None
    live = NS(
        previous_night={},
        ground_items=NS(goal_facts=lambda _d: [], rows=[]),
        crop_read=None,
        crop_memory=NS(crops={}),
        shop_memory=None,
        chest_memory=None,
        place_visits={},
        history=[{"location": "Farm"}],
        read_today={},
    )
    campaign = NS(date=list(date), live=live, days=[{"date": list(date), "skills": []}], part="live", epoch=(1, 1, 1, 1, 0))
    session = NS(campaign=campaign, reason=None, page_budget=None)
    facts = NS(location="Farm", time_of_day=900, inventory={}, inventory_current=True)
    return rt, session, facts


def _setup_active_cycle(
    tmp_path: Path,
    *,
    save_id: str,
    player_id: str,
    status: str = "active",
    world_ready: bool = True,
    with_lock: bool = True,
    stop_file: str | None = None,
) -> tuple[Path, Path, Path]:
    run_dir = tmp_path / "run"
    cycle_dir = run_dir / "live7" / "c1"
    cmd_dir = run_dir / "cmd"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    cmd_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": "live7_cycle.v1",
        "status": status,
        "cycle": "c1",
        "save_id": save_id,
        "player_id": player_id,
        "player_name": "Arti",
        "command_dir": str(cmd_dir),
        "live7_stop_file": str(run_dir / "LIVE7_STOP"),
    }
    (cycle_dir / "cycle_state.json").write_text(json.dumps(state), encoding="utf-8")
    (cmd_dir / "session.json").write_text(json.dumps({
        "world_ready": world_ready,
        "player_id": player_id if world_ready else 0,
        "player_name": "Arti" if world_ready else "",
    }), encoding="utf-8")
    if with_lock:
        (cycle_dir / "supervisor.lock").write_text(str(os.getpid()), encoding="utf-8")
    if stop_file:
        (cycle_dir / stop_file).write_text("stop\n", encoding="utf-8")
    return run_dir, cycle_dir, cmd_dir


# --------------------------------------------------------------------------- 1. Two scopes in one store (Winter 22 vs Spring 13)


def test_two_scopes_in_one_store_page_planner_digest_and_legacy_reflect(tmp_path):
    """Winter 22 on Farm A and Spring 13 on Farm B share one mind dir with zero cross-farm leakage."""
    base = tmp_path / "mind"

    # Farm A (Winter 22 = day 106): plan a winter want, reflect day 105, record likes & discovery
    verified_a = [dict(ref="action:105:1", day=WINTER_22 - 1, text="mancing es di danau beku")]
    payload_a = G.build_input(
        base, SCOPE_A, day=WINTER_22, target_day=WINTER_22, mode="first_morning",
        evidence=[], native=[], calendar=[], daily={}, verified=verified_a,
        remembered_day=WINTER_22 - 1, mind_scoped=True,
    )
    res_a = G.apply_reply(
        base, SCOPE_A,
        _reply(
            new=[_want("today", "aku mau cari salju di utara"), _want("season", "aku mau tabung winter 5000g")],
            summary="kemarin di farm uji aku mancing di es winter",
            fun=[{"text": "mancing es winter", "ref": "action:105:1"}],
            not_fun=[{"text": "kedinginan di salju", "ref": "action:105:1"}],
        ),
        payload_a, day=WINTER_22, target_day=WINTER_22, mode="first_morning", mind_scoped=True,
    )
    assert len(res_a["new"]) == 2
    # Verify every new row carries scope_hash
    for fname in (M.GOALS_FILE, M.JOURNEY_FILE, M.LIKES_FILE):
        rows = [json.loads(line) for line in (M.game_dir(base, "stardew") / fname).read_text(encoding="utf-8").splitlines() if line.strip()]
        assert rows and all(r.get("scope_hash") == HASH_A for r in rows), fname

    # Farm B (Spring 13 = day 13): planner input must see ZERO Farm A wants, yesterday, or likes
    verified_b = [dict(ref="action:12:1", day=SPRING_13 - 1, text="siram stroberi di kebun ladang")]
    payload_b = G.build_input(
        base, SCOPE_B, day=SPRING_13, target_day=SPRING_13, mode="first_morning",
        evidence=[], native=[], calendar=[], daily={}, verified=verified_b,
        remembered_day=SPRING_13 - 1, mind_scoped=True,
    )
    blob_b = json.dumps(payload_b, ensure_ascii=False)
    assert "salju" not in blob_b and "winter" not in blob_b.lower() and "mancing es" not in blob_b
    assert payload_b["keinginan"] == [] and payload_b["yang_kamu_suka"] == {"suka": [], "nggak_suka": []}

    # Farm B applies its own reply (Spring 13 remembering Spring 12)
    res_b = G.apply_reply(
        base, SCOPE_B,
        _reply(
            new=[_want("today", "aku mau siram stroberi spring"), _want("season", "aku mau panen spring penuh")],
            summary="kemarin di farm ladang aku tanam stroberi",
            fun=[{"text": "siram stroberi", "ref": "action:12:1"}],
            not_fun=[{"text": "batu menghalangi jalan", "ref": "action:12:1"}],
        ),
        payload_b, day=SPRING_13, target_day=SPRING_13, mode="first_morning", mind_scoped=True,
    )
    assert len(res_b["new"]) == 2

    # Play page (goal ladder ON) for Farm B shows only Farm B; Farm A shows only Farm A
    rt_b, sess_b, facts_b = _make_runtime(
        base, scope=SCOPE_B, date=(1, "spring", 13),
        config={"live7_mind_scope": True, "stardew_live7_goal_ladder": True},
    )
    offer_b = {"day": {}}
    rt_b._live7_mind_offer(sess_b, facts_b, [S.SkillCall("siram")], offer_b)
    text_b = json.dumps(offer_b, ensure_ascii=False)
    assert "stroberi" in text_b
    assert "salju" not in text_b and "winter" not in text_b.lower()

    # Play page (goal ladder OFF: render_block + yesterday_line) for Farm B
    rt_b_leg, sess_b_leg, facts_b_leg = _make_runtime(
        base, scope=SCOPE_B, date=(1, "spring", 13),
        config={"live7_mind_scope": True, "stardew_live7_goal_ladder": False},
    )
    offer_b_leg = {"day": {}}
    rt_b_leg._live7_mind_offer(sess_b_leg, facts_b_leg, [S.SkillCall("siram")], offer_b_leg)
    text_b_leg = json.dumps(offer_b_leg, ensure_ascii=False)
    assert "stroberi" in text_b_leg and "tanam stroberi" in text_b_leg
    assert "salju" not in text_b_leg and "mancing es" not in text_b_leg

    # Play page defense-in-depth: even if unfiltered goals are passed into goal_ladder_runtime.page, foreign rows drop
    unfiltered_goals = M.load_goals(base, "stardew", scoped=False)
    assert len(unfiltered_goals) == 4
    page_b = GR.page(rt_b, sess_b, facts_b, [S.SkillCall("siram")], {}, [], unfiltered_goals)
    page_b_text = json.dumps(page_b, ensure_ascii=False)
    assert "stroberi" in page_b_text and "salju" not in page_b_text and "winter" not in page_b_text.lower()

    # Experience digest for Farm B carries ONLY Farm B's wants and yesterday summary
    digest_b = D.build(base, SCOPE_B, day=SPRING_13, place="Farm", mind_scoped=True)
    assert digest_b["scope_hash"] == HASH_B
    assert "stroberi" in digest_b["text"]
    assert "salju" not in digest_b["text"] and "winter" not in digest_b["text"].lower() and "mancing es" not in digest_b["text"]

    # Experience digest for Farm A carries ONLY Farm A's wants and yesterday summary
    digest_a = D.build(base, SCOPE_A, day=WINTER_22, place="Farm", mind_scoped=True)
    assert digest_a["scope_hash"] == HASH_A
    assert "salju" in digest_a["text"] and "mancing di es" in digest_a["text"]
    assert "stroberi" not in digest_a["text"]

    # Legacy arti_mind_reflect.reflect on Farm B sees only Farm B and leaves Farm A intact
    seen_prompt = []
    def fake_complete(messages, _schema):
        seen_prompt.append(messages[1]["content"])
        return json.dumps({
            "today": "hari ini aku rawat kebun ladang",
            "tomorrow": "besok panen lagi",
            "fun": [{"text": "rawat kebun ladang", "ref": "r1"}],
            "not_fun": [],
            "goals": {"keep": [], "done": [], "drop": [], "progress": [], "new": []},
        })
    ref_b = R.reflect(
        fake_complete, base, game="stardew", day=SPRING_13, day_label="Y1 spring 13",
        verified=[{"ref": "r1", "text": "siram kebun ladang"}], whys=[], progress=[], places=["Farm"], effects={},
        scope=SCOPE_B, scoped=True,
    )
    assert ref_b["ev"] == "mind_reflection"
    assert "salju" not in seen_prompt[0] and "mancing es" not in seen_prompt[0]
    assert len(M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True)) == 2
    assert len(M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True)) == 2


# --------------------------------------------------------------------------- 2. Same day number on both farms


def test_same_day_number_does_not_overwrite_likes_journey_discoveries_or_expire_other_farm(tmp_path):
    base = tmp_path / "mind"

    # Both Farm A and Farm B write likes, journey, discoveries, and goals on day 15 (Y1 spring 15)
    M.save_likes(base, "stardew", day=SPRING_15, day_label="Y1 spring 15",
                 fun=[{"text": "suka farm alpha", "ref": "r1"}],
                 not_fun=[{"text": "capek alpha", "ref": "r1"}], scope=SCOPE_A, scoped=True)
    M.save_likes(base, "stardew", day=SPRING_15, day_label="Y1 spring 15",
                 fun=[{"text": "suka farm beta", "ref": "r2"}],
                 not_fun=[{"text": "capek beta", "ref": "r2"}], scope=SCOPE_B, scoped=True)

    likes_a = M.likes_lines(base, "stardew", before_day=16, scope=SCOPE_A, scoped=True)
    likes_b = M.likes_lines(base, "stardew", before_day=16, scope=SCOPE_B, scoped=True)
    assert any("alpha" in line for line in likes_a["suka"])
    assert not any("beta" in line for line in likes_a["suka"] + likes_a["nggak_suka"])
    assert any("beta" in line for line in likes_b["suka"])
    assert not any("alpha" in line for line in likes_b["suka"] + likes_b["nggak_suka"])

    M.save_journey_row(base, "stardew", dict(
        v=M.MIND_VERSION, schema=M.JOURNEY_SCHEMA, id="Y1 spring 15", date="Y1 spring 15",
        day=SPRING_15, reflection={"today": "ringkasan alpha hari 15"}, discoveries=[],
    ), scope=SCOPE_A, scoped=True)
    M.save_journey_row(base, "stardew", dict(
        v=M.MIND_VERSION, schema=M.JOURNEY_SCHEMA, id="Y1 spring 15", date="Y1 spring 15",
        day=SPRING_15, reflection={"today": "ringkasan beta hari 15"}, discoveries=[],
    ), scope=SCOPE_B, scoped=True)

    # Record discoveries on same day 15 for both scopes; journey rows must stay separate and retain reflections
    M.record_discoveries(base, "stardew", scope=SCOPE_A, discoveries=[dict(
        id="disc:alpha", kind="item_obtained", subject="Parsnip", text="Alpha parsnip", source="native",
    )], day=SPRING_15, day_label="Y1 spring 15", scoped=True)
    M.record_discoveries(base, "stardew", scope=SCOPE_B, discoveries=[dict(
        id="disc:beta", kind="item_obtained", subject="Kale", text="Beta kale", source="native",
    )], day=SPRING_15, day_label="Y1 spring 15", scoped=True)

    assert M.yesterday_line(base, "stardew", SPRING_15 + 1, scope=SCOPE_A, scoped=True) == "ringkasan alpha hari 15"
    assert M.yesterday_line(base, "stardew", SPRING_15 + 1, scope=SCOPE_B, scoped=True) == "ringkasan beta hari 15"
    j_a = M.load_journey(base, "stardew", scope=SCOPE_A, scoped=True)
    j_b = M.load_journey(base, "stardew", scope=SCOPE_B, scoped=True)
    assert len(j_a) == 1 and len(j_b) == 1
    assert [d["subject"] for d in j_a[0]["discoveries"]] == ["Parsnip"]
    assert [d["subject"] for d in j_b[0]["discoveries"]] == ["Kale"]

    # Season wants on Spring Farm B are NEVER expired when Winter Farm A runs expire() / apply_reply()
    payload_b = G.build_input(base, SCOPE_B, day=SPRING_15, target_day=SPRING_15, mode="first_morning",
                              evidence=[], native=[], calendar=[], daily={}, mind_scoped=True)
    G.apply_reply(base, SCOPE_B, _reply(new=[_want("season", "aku mau panen spring di ladang")]),
                  payload_b, day=SPRING_15, target_day=SPRING_15, mode="first_morning", mind_scoped=True)

    payload_a = G.build_input(base, SCOPE_A, day=WINTER_22, target_day=WINTER_22, mode="first_morning",
                              evidence=[], native=[], calendar=[], daily={}, mind_scoped=True)
    G.apply_reply(base, SCOPE_A, _reply(new=[_want("season", "aku mau gali winter di uji")]),
                  payload_a, day=WINTER_22, target_day=WINTER_22, mode="first_morning", mind_scoped=True)

    goals_b = M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True)
    assert len(goals_b) == 1 and goals_b[0]["status"] == "open" and goals_b[0]["text"] == "aku mau panen spring di ladang"

    # cap_overflow on Farm A (4 open today wants) never caps Farm B's open today wants
    raw_rows = [
        dict(v=1, id=f"t106-{i}", horizon="today", text=f"aku mau alpha {i}", status="open",
             set_on_day=WINTER_22, for_day=WINTER_22, scope_hash=HASH_A)
        for i in range(1, 5)
    ] + [
        dict(v=1, id=f"t15-{i}", horizon="today", text=f"aku mau beta {i}", status="open",
             set_on_day=SPRING_15, for_day=SPRING_15, scope_hash=HASH_B)
        for i in range(1, 4)
    ]
    out_rows, capped = M.cap_overflow(raw_rows, WINTER_22, scope_hash=HASH_A)
    assert [g["id"] for g in capped] == ["t106-1"]
    assert all(g["status"] == "open" for g in out_rows if g["scope_hash"] == HASH_B)


# --------------------------------------------------------------------------- 3. Caps and byte limits per scope (60 nights each)


def test_caps_and_byte_limits_per_scope_sixty_nights_each(tmp_path):
    """60 nights on Farm A + 60 nights on Farm B plus byte-limit pressure never drops the other farm or legacy rows."""
    base = tmp_path / "mind"

    # Seed one unmapped legacy row in each file first
    M.save_goals(base, "stardew", [dict(
        v=1, id="l1-1", horizon="long", text="aku mau jaga kenangan lama", status="done",
        set_on_day=1, for_day=None, closed_on_day=2,
    )])
    M.save_journey_row(base, "stardew", dict(
        v=1, schema=M.JOURNEY_SCHEMA, id="Y1 spring 1", date="Y1 spring 1", day=1,
        reflection={"today": "hari pertama lama"}, discoveries=[],
    ))
    M.save_likes(base, "stardew", day=1, day_label="Y1 spring 1",
                 fun=[{"text": "kenangan lama", "ref": "r0"}], not_fun=[])

    # Run 60 nights on Farm B first (so Farm B sits earlier in the file and would be dropped first if global!)
    for d in range(1, 61):
        vref = f"action:{d}:1"
        payload = G.build_input(base, SCOPE_B, day=d + 1, target_day=d + 1, mode="night",
                                evidence=[], native=[], calendar=[], daily={},
                                verified=[{"ref": vref, "day": d, "text": f"rawat ladang hari {d}"}],
                                remembered_day=d, mind_scoped=True)
        prev_ids = [g["id"] for g in M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True) if g["status"] == "open"]
        G.apply_reply(
            base, SCOPE_B,
            _reply(
                drop=prev_ids,
                new=[_want("today", f"aku mau urus kebun ladang malam ke {d}")],
                summary=f"malam ladang ke {d}",
                fun=[{"text": f"suka ladang {d}", "ref": vref}],
                not_fun=[],
            ),
            payload, day=d + 1, target_day=d + 1, mode="night", mind_scoped=True,
        )

    b_goals_before = M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True)
    b_journey_before = M.load_journey(base, "stardew", scope=SCOPE_B, scoped=True)
    b_likes_before = M.load_likes(base, "stardew", scope=SCOPE_B, scoped=True)
    assert len(b_journey_before) == 60
    assert 50 <= len(b_likes_before) <= 60 and b_likes_before[-1]["day"] == 60
    assert len(b_goals_before) == 60

    # Now run 65 nights on Farm A with long texts (pushing Farm A past GOAL_HISTORY_MAX=60, LIKES_MAX=60, and byte caps)
    for d in range(1, 66):
        vref = f"action:{d}:1"
        payload = G.build_input(base, SCOPE_A, day=d + 1, target_day=d + 1, mode="night",
                                evidence=[], native=[], calendar=[], daily={},
                                verified=[{"ref": vref, "day": d, "text": f"uji rute tambang hari {d}"}],
                                remembered_day=d, mind_scoped=True)
        prev_ids = [g["id"] for g in M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True) if g["status"] == "open"]
        G.apply_reply(
            base, SCOPE_A,
            _reply(
                drop=prev_ids,
                new=[_want("today", f"aku mau uji coba rute panjang nomor {d} di sekitar tambang gunung utara")],
                summary=f"malam uji ke {d}",
                fun=[{"text": f"suka uji panjang nomor {d} di kebun eksperimen", "ref": vref}],
                not_fun=[{"text": f"bosan uji panjang nomor {d} di gua", "ref": vref}],
            ),
            payload, day=d + 1, target_day=d + 1, mode="night", mind_scoped=True,
        )

    # Farm A is bounded to its own per-scope cap
    a_goals = M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True)
    a_closed = [g for g in a_goals if g["status"] != "open"]
    assert len(a_closed) <= M.GOAL_HISTORY_MAX
    assert sum(len(json.dumps(g, ensure_ascii=False).encode("utf-8")) + 1 for g in a_goals) <= M.GOALS_FILE_MAX_BYTES
    a_likes = M.load_likes(base, "stardew", scope=SCOPE_A, scoped=True)
    assert len(a_likes) <= M.LIKES_MAX
    assert sum(len(json.dumps(r, ensure_ascii=False).encode("utf-8")) + 1 for r in a_likes) <= M.LIKES_MAX_BYTES

    # Farm B's 60 nights are 100% intact (ZERO rows dropped by Farm A's trimming!)
    assert M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True) == b_goals_before
    assert M.load_journey(base, "stardew", scope=SCOPE_B, scoped=True) == b_journey_before
    assert M.load_likes(base, "stardew", scope=SCOPE_B, scoped=True) == b_likes_before

    # Unmapped legacy rows are ALSO 100% intact on disk
    assert M.unscoped_counts(base, "stardew") == {"goals": 1, "journey": 1, "likes": 1}


# --------------------------------------------------------------------------- 4. Legacy unscoped store + sidecar + malformed sidecar


def test_legacy_unscoped_store_without_with_and_with_torn_sidecar(tmp_path):
    base = tmp_path / "mind"
    folder = M.game_dir(base, "stardew")

    # Seed legacy unscoped rows (no scope_hash): one belonging to Farm A, one unmapped
    legacy_goals = [
        dict(v=1, id="t106-1", horizon="today", text="aku mau cek danau beku lama", status="open",
             set_on_day=WINTER_22, for_day=WINTER_22),
        dict(v=1, id="l9-1", horizon="long", text="aku mau misteri tak terpetakan", status="open",
             set_on_day=9, for_day=None),
    ]
    M.save_goals(base, "stardew", legacy_goals)
    M.save_journey_row(base, "stardew", dict(
        v=1, schema=M.JOURNEY_SCHEMA, id="Y1 winter 21", date="Y1 winter 21", day=WINTER_22 - 1,
        reflection={"today": "kemarin aku di danau beku lama"}, discoveries=[],
    ))
    M.save_likes(base, "stardew", day=WINTER_22 - 1, day_label="Y1 winter 21",
                 fun=[{"text": "es lama", "ref": "r1"}], not_fun=[])

    raw_goals_before = (folder / M.GOALS_FILE).read_text(encoding="utf-8")
    raw_journey_before = (folder / M.JOURNEY_FILE).read_text(encoding="utf-8")
    raw_likes_before = (folder / M.LIKES_FILE).read_text(encoding="utf-8")
    sidecar = folder / MS.LEGACY_SCOPES_FILE

    # (a) WITHOUT sidecar: hidden on all pages/planner/digest, untouched, count-only journal, sidecar never created
    assert M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True) == []
    assert M.yesterday_line(base, "stardew", WINTER_22, scope=SCOPE_A, scoped=True) is None
    assert M.likes_lines(base, "stardew", before_day=WINTER_22, scope=SCOPE_A, scoped=True) == {"suka": [], "nggak_suka": []}
    assert not sidecar.exists()

    rt_a, sess_a, facts_a = _make_runtime(
        base, scope=SCOPE_A, date=(1, "winter", 22),
        config={"live7_mind_scope": True, "stardew_live7_goal_ladder": False},
    )
    offer_a = {"day": {}}
    rt_a._live7_mind_offer(sess_a, facts_a, [S.SkillCall("jalan")], offer_a)
    assert "danau beku" not in json.dumps(offer_a, ensure_ascii=False)
    assert "misteri" not in json.dumps(offer_a, ensure_ascii=False)
    unscoped_evs = [r for r in rt_a._journal_rows if r.get("ev") == "mind_unscoped_rows"]
    assert len(unscoped_evs) == 1
    assert unscoped_evs[0]["counts"] == {"goals": 2, "journey": 1, "likes": 1}
    assert "danau beku" not in json.dumps(unscoped_evs[0]) and "misteri" not in json.dumps(unscoped_evs[0])

    # Unmapped rows count toward NO cap: Farm B can add 3 open today goals without being blocked by legacy t106-1
    payload_b = G.build_input(base, SCOPE_B, day=SPRING_13, target_day=SPRING_13, mode="first_morning",
                              evidence=[], native=[], calendar=[], daily={}, mind_scoped=True)
    res_b = G.apply_reply(
        base, SCOPE_B,
        _reply(new=[
            _want("today", "aku mau ladang satu"),
            _want("today", "aku mau ladang dua"),
            _want("today", "aku mau ladang tiga"),
        ]),
        payload_b, day=SPRING_13, target_day=SPRING_13, mode="first_morning", mind_scoped=True,
    )
    assert len(res_b["new"]) == 3
    assert res_b["unscoped_rows"] == {"goals": 2, "journey": 1, "likes": 1}
    assert not sidecar.exists()
    # Original legacy lines stay verbatim on disk
    disk_goals = (folder / M.GOALS_FILE).read_text(encoding="utf-8")
    for old_line in raw_goals_before.splitlines():
        assert old_line in disk_goals
    assert (folder / M.JOURNEY_FILE).read_text(encoding="utf-8").startswith(raw_journey_before.strip())
    assert (folder / M.LIKES_FILE).read_text(encoding="utf-8") == raw_likes_before

    # (b) WITH sidecar mapping t106-1 and day 105 to Farm A (l9-1 remains unmapped)
    sidecar.write_text(json.dumps({
        "schema": MS.LEGACY_SCOPES_SCHEMA,
        "goals": {"t106-1": HASH_A},
        "journey": {"Y1 winter 21": HASH_A, "105": HASH_A},
        "likes": {"105": HASH_A},
    }), encoding="utf-8")
    assert [g["id"] for g in M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True)] == ["t106-1"]
    assert M.yesterday_line(base, "stardew", WINTER_22, scope=SCOPE_A, scoped=True) == "kemarin aku di danau beku lama"
    assert any("es lama" in line for line in M.likes_lines(base, "stardew", before_day=WINTER_22, scope=SCOPE_A, scoped=True)["suka"])
    # Farm B still sees zero of Farm A's sidecar-mapped legacy rows, and l9-1 stays unmapped for both
    assert all(g["id"] != "t106-1" and g["id"] != "l9-1" for g in M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True))
    assert M.unscoped_counts(base, "stardew") == {"goals": 1, "journey": 0, "likes": 0}
    digest_a = D.build(base, SCOPE_A, day=WINTER_22, place="Farm", mind_scoped=True)
    assert "danau beku lama" in digest_a["text"] and "misteri" not in digest_a["text"]

    # (c) TORN / MALFORMED / WRONG-SCHEMA sidecar -> empty map, no crash, never overwritten
    for bad_content in (
        "{torn json",
        json.dumps({"schema": "wrong/9", "goals": {"t106-1": HASH_A}}),
        json.dumps({"schema": MS.LEGACY_SCOPES_SCHEMA, "goals": {"t106-1": "not-a-sha256"}}),
        "[]",
    ):
        sidecar.write_text(bad_content, encoding="utf-8")
        # bump mtime/size reliably
        os.utime(sidecar, (time.time() + 5, time.time() + 5))
        assert M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True) == []
        assert sidecar.read_text(encoding="utf-8") == bad_content


def test_a_malformed_scope_key_is_kept_shown_nowhere_and_never_raises(tmp_path):
    base = tmp_path / "mind"
    folder = M.game_dir(base, "stardew")
    folder.mkdir(parents=True)
    odd = json.dumps(dict(v=1, id="t1-1", horizon="today", text="aku mau aneh", status="open", set_on_day=1, for_day=1,
                          scope_hash=["x"]))
    (folder / M.GOALS_FILE).write_text(odd + chr(10), encoding="utf-8")
    assert M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True) == []
    M.save_goals(base, "stardew", [dict(v=1, id="t1-2", horizon="today", text="aku mau satu", status="open", set_on_day=1,
                                        for_day=1, scope_hash=HASH_A)], scope=SCOPE_A, scoped=True)
    lines = (folder / M.GOALS_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["t1-1", "t1-2"] and json.loads(lines[0])["scope_hash"] == ["x"]


def _legacy_days(n):
    return [dict(v=1, schema=M.JOURNEY_SCHEMA, id="Y9 spring %d" % i, date="Y9 spring %d" % i, day=90000 + i,
                 reflection={"today": "lama %d" % i}, discoveries=[]) for i in range(1, n + 1)]


def _full_store(base):
    """118 unmapped old days, then two farms' scoped days: more rows than the old file limit of 120."""
    folder = M.game_dir(base, "stardew")
    folder.mkdir(parents=True)
    (folder / M.JOURNEY_FILE).write_text("".join(json.dumps(r) + chr(10) for r in _legacy_days(118)), encoding="utf-8")
    for scope in (SCOPE_A, SCOPE_B):
        for day in (101, 102, 103):
            M.save_journey_row(base, "stardew", dict(v=1, schema=M.JOURNEY_SCHEMA, id="d%d" % day, date="d%d" % day, day=day,
                                                     reflection={"today": "hari %d" % day}, discoveries=[]),
                               scope=scope, scoped=True)
    return folder / M.JOURNEY_FILE


def _days(path):
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {(r.get("scope_hash"), r["id"]) for r in rows}


@pytest.mark.parametrize("told", [True, False])
def test_story_lines_of_a_festival_drop_no_day_of_any_farm(tmp_path, told, monkeypatch):
    """native_story.remember once wrote the day the old way, and the old limit dropped the oldest days."""
    from arti_runtime import native_story as NST
    base = tmp_path / "mind"
    path = _full_store(base)
    before = _days(path)
    monkeypatch.setattr(NST, "_KEPT", {})
    rows = [dict(item_id="run:ev", id="run:ev", first_day=140, facts=dict(event_id="festival", event_type="festival",
            date="2:summer:6", location="Temp", lines=[dict(speaker="Pembawa Acara", text="contoh kalimat buatan")],
            last_seq=1, bounded=True, complete=True))]
    NST.keep_witnessed(base, SCOPE_B, rows, day=146, mind_scoped=told)
    after = _days(path)
    assert before <= after and len(after) == len(before) + 1
    assert {s for s, _ in after - before} == {HASH_B}  # the day is written under her own farm, told or not
    assert M.unscoped_counts(base, "stardew")["journey"] == 118


def test_every_old_style_write_leaves_the_farms_rows_alone(tmp_path):
    base = tmp_path / "mind"
    path = _full_store(base)
    before = {k for k in _days(path) if k[0]}
    for n in range(1, 8):  # past the old limit among the unscoped rows alone
        M.save_journey_row(base, "stardew", dict(v=1, schema=M.JOURNEY_SCHEMA, id="Y9 summer %d" % n, date="x", day=91000 + n,
                                                 reflection={"today": "x"}, discoveries=[]))
    assert before <= _days(path) and sum(1 for k in _days(path) if not k[0]) == M.JOURNEY_MAX
    M.save_likes(base, "stardew", day=5, day_label="d5", fun=[dict(text="suka lama", ref="r")], not_fun=[])
    M.save_likes(base, "stardew", day=5, day_label="d5", fun=[dict(text="suka farm", ref="r")], not_fun=[], scope=SCOPE_A, scoped=True)
    M.save_likes(base, "stardew", day=5, day_label="d5", fun=[dict(text="suka baru", ref="r")], not_fun=[])
    assert [r["text"] for r in M.load_likes(base, "stardew", scope=SCOPE_A, scoped=True)] == ["suka farm"]
    goal = lambda i, **k: dict(v=1, id=i, horizon="today", text="aku mau " + i, status="done", set_on_day=1, for_day=1, **k)  # noqa: E731
    M.save_goals(base, "stardew", [goal("a%d" % i, scope_hash=HASH_A) for i in range(M.GOAL_HISTORY_MAX)], scope=SCOPE_A, scoped=True)
    M.save_goals(base, "stardew", M.load_goals(base, "stardew") + [goal("u%d" % i) for i in range(M.GOAL_HISTORY_MAX + 3)])
    rows = M.load_goals(base, "stardew")
    assert sum(1 for r in rows if r.get("scope_hash")) == M.GOAL_HISTORY_MAX == sum(1 for r in rows if not r.get("scope_hash"))


# --------------------------------------------------------------------------- 5. tools/mind_scope_map.py


def test_mind_scope_map_builds_sidecar_from_run_journals_only_and_refuses_mind_dir(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    # Cycle A (SCOPE_A): goals t106-1 (unique), t15-1 (conflicts with B), day 105 (unique), day 15 (conflicts with B)
    c_a = runs / "run-a" / "runtime" / "live7" / "c1"
    j_a = c_a / "day-0001-attempt-000" / "journal"
    j_a.mkdir(parents=True)
    (c_a / "cycle_state.json").write_text(json.dumps({"save_id": "900111222", "player_id": "1001"}), encoding="utf-8")
    (j_a / "g1_journal.jsonl").write_text("\n".join([
        json.dumps({"ev": "goal_planning", "day": 106, "target_day": 106, "remembered_day": 105, "new": ["t106-1", "t15-1"]}),
        json.dumps({"ev": "mind_reflection", "day": 15, "new_goals": ["w15-1"]}),
    ]) + "\n", encoding="utf-8")

    # Cycle B (SCOPE_B): goals t13-1 (unique), t15-1 (conflict!), day 12 (unique), day 15 (conflict!)
    c_b = runs / "run-b" / "runtime" / "live7" / "c1"
    j_b = c_b / "day-0001-attempt-000" / "journal"
    j_b.mkdir(parents=True)
    (c_b / "cycle_state.json").write_text(json.dumps({"save_id": "900333444", "player_id": "2002"}), encoding="utf-8")
    (j_b / "g1_journal.jsonl").write_text("\n".join([
        json.dumps({"ev": "goal_planning", "day": 13, "target_day": 13, "remembered_day": 12, "new": ["t13-1", "t15-1"]}),
        json.dumps({"ev": "mind_reflection", "day": 15, "new_goals": []}),
        json.dumps({"ev": "discovery", "day": 15, "text": "secret discovery text must never appear in sidecar"}),
    ]) + "\n", encoding="utf-8")

    # Decoy mind dir inside runs root — tool must NEVER read it
    decoy = runs / "mind" / "stardew"
    decoy.mkdir(parents=True)
    (decoy / "goals.jsonl").write_text('{"id":"decoy-1","text":"NEVER READ THIS"}\n', encoding="utf-8")

    out = tmp_path / "staging" / "legacy_scopes.json"
    rc = mind_scope_map.main(["--runs", str(runs), "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == MS.LEGACY_SCOPES_SCHEMA
    assert doc["goals"] == {"t106-1": HASH_A, "w15-1": HASH_A, "t13-1": HASH_B}
    assert "t15-1" not in doc["goals"]
    assert doc["journey"]["Y1 winter 21"] == HASH_A
    assert doc["journey"]["Y1 spring 12"] == HASH_B
    assert "Y1 spring 15" not in doc["journey"]
    assert doc["likes"]["105"] == HASH_A and doc["likes"]["12"] == HASH_B
    assert "15" not in doc["likes"]
    assert doc["counts"]["goals_ambiguous"] == 1
    assert doc["counts"]["journey_ambiguous"] == 1
    assert doc["counts"]["likes_ambiguous"] == 1
    raw_out = out.read_text(encoding="utf-8")
    assert "secret discovery text" not in raw_out and "NEVER READ THIS" not in raw_out and "decoy-1" not in raw_out

    # Refuses writing into a mind folder (by name, by ARTI_MIND_DIR, or beside mind files)
    assert mind_scope_map.main(["--runs", str(runs), "--out", str(tmp_path / "mind" / "stardew" / "legacy_scopes.json")]) == 2
    monkeypatch.setenv("ARTI_MIND_DIR", str(tmp_path / "custom_dir"))
    assert mind_scope_map.main(["--runs", str(runs), "--out", str(tmp_path / "custom_dir" / "stardew" / "legacy_scopes.json")]) == 2
    monkeypatch.delenv("ARTI_MIND_DIR", raising=False)
    existing_store = tmp_path / "other_store"
    existing_store.mkdir()
    (existing_store / "goals.jsonl").write_text("{}\n", encoding="utf-8")
    assert mind_scope_map.main(["--runs", str(runs), "--out", str(existing_store / "legacy_scopes.json")]) == 2


# --------------------------------------------------------------------------- 6. now.json & digest gating, cycle clear, no cross-scope carry


def test_now_and_digest_gating_on_bridge_status_facts_stream_notes_initiative(tmp_path, monkeypatch):
    import arti_bridge as B
    import arti_live7_status_facts as SF
    import arti_stardew_initiative as SI
    from arti_screen_privacy import ScreenPrivacy

    base = tmp_path / "mind"
    now_ts = time.time()
    run_dir, cycle_dir, cmd_dir = _setup_active_cycle(tmp_path, save_id="900111222", player_id="1001")

    # Write Farm A's now.json and experience_digest.json
    M.write_now(
        base, game="stardew", why="aku mau cek pelabuhan ladang", goal_ref="t13-1",
        goal_today=["aku mau ke pelabuhan"], game_time={"date": "Y1 spring 13", "time": 900, "place": "Farm"},
        decision_ts=now_ts, observed_ts=now_ts,
        native_facts={"farm_scope": SCOPE_A, "own_state": {"money": {"value": 999, "observed_ts": now_ts}}},
        scope=SCOPE_A, scoped=True,
    )
    M.note_doing(base, game="stardew", doing={"skill": "pergi_ke", "arg": "beach"},
                 chosen_at={"date": "Y1 spring 13", "time": 900, "place": "Farm"},
                 scope=SCOPE_A, scoped=True)
    digest_a = D.build(base, SCOPE_A, day=SPRING_13, place="Farm", now=now_ts, mind_scoped=True)
    D.write(base, digest_a)

    privacy = ScreenPrivacy()
    monkeypatch.setattr(B, "screen_privacy", privacy)
    monkeypatch.setattr(B, "_stardew_runtime", None, raising=False)
    monkeypatch.setattr(B, "_stardew_live", lambda: True)
    monkeypatch.setattr(B, "_live7_game_session_stopped", lambda: False)
    monkeypatch.setitem(B.CONFIG, "arti_mind_dir", str(base))
    monkeypatch.setitem(B.CONFIG, "live7_help_dir", str(run_dir))
    monkeypatch.setitem(B.CONFIG, "stardew_command_dir", str(cmd_dir))
    monkeypatch.setitem(B.CONFIG, "live7_mind_scope", True)
    monkeypatch.setitem(B.CONFIG, "voice_game_memory_enabled", True)
    monkeypatch.setitem(B.CONFIG, "arti_mind_intent_enabled", True)

    # (a) Active in-world cycle of matching scope -> all bridge readers show Farm A
    intent = B.mind_intent_block_now()
    assert "[NIATKU]" in intent and "pelabuhan ladang" in intent
    assert "[STARDEW]" in B.stardew_role_block_now()
    assert "[INGATAN GAME]" in D.bridge_block(B)
    assert MS.bridge_read_now(B)["scope_hash"] == HASH_A
    # StatusFactsRelay reads active scope's now.json
    relay = SF.StatusFactsRelay(dict(B.CONFIG), lambda _text: None, clock=lambda: now_ts)
    relay._poll(cycle_dir, now_ts)
    assert relay.attention_native
    # stream_notes reads active scope's now.json
    assert SN.bridge_line(B, "owner", "ptt", "halo di ladang") is True
    inbox_row = json.loads(SN._path(base).read_text(encoding="utf-8").splitlines()[-1])
    assert inbox_row["game_time"] == {"date": "Y1 spring 13", "time": 900, "place": "Farm"}

    # (b) Title screen (session.json world_ready=False) -> ignored AT ONCE even while supervisor.lock stays
    (cmd_dir / "session.json").write_text(json.dumps({"world_ready": False, "player_id": 0}), encoding="utf-8")
    os.utime(cmd_dir / "session.json", (now_ts + 2, now_ts + 2))
    assert B.mind_intent_block_now() == ""
    lived = D.bridge_block(B)  # she lived it: the day stays with her after play ends, as the past
    assert "[INGATAN GAME]" in lived and " Terakhir main " in lived and " Sekarang " not in lived
    assert MS.bridge_read_now(B) is None
    relay._poll(cycle_dir, now_ts)
    assert relay.attention_native == {}
    assert SI.game_first_facts(dict(B.CONFIG), None, now=now_ts) == ()

    # Restore in-world, then switch to host_left -> ignored AT ONCE
    (cmd_dir / "session.json").write_text(json.dumps({"world_ready": True, "player_id": "1001"}), encoding="utf-8")
    os.utime(cmd_dir / "session.json", (now_ts + 4, now_ts + 4))
    assert "[NIATKU]" in B.mind_intent_block_now()

    state = json.loads((cycle_dir / "cycle_state.json").read_text(encoding="utf-8"))
    (cycle_dir / "cycle_state.json").write_text(json.dumps(dict(state, status="host_left")), encoding="utf-8")
    os.utime(cycle_dir / "cycle_state.json", (now_ts + 6, now_ts + 6))
    assert B.mind_intent_block_now() == ""
    assert "[INGATAN GAME]" in D.bridge_block(B)
    assert MS.bridge_read_now(B) is None

    # Restore active, then add LIVE7_STOP -> ignored AT ONCE
    (cycle_dir / "cycle_state.json").write_text(json.dumps(state), encoding="utf-8")
    os.utime(cycle_dir / "cycle_state.json", (now_ts + 8, now_ts + 8))
    assert "[INGATAN GAME]" in D.bridge_block(B)
    (cycle_dir / "LIVE7_STOP").write_text("stop\n", encoding="utf-8")
    assert B.mind_intent_block_now() == ""
    assert MS.bridge_read_now(B) is None
    assert "[INGATAN GAME]" in D.bridge_block(B)
    # A voice process that never saw this farm active (a fresh bridge) reads no farm's digest
    monkeypatch.setattr(B, "_live7_memory_scope", None, raising=False)
    assert D.bridge_block(B) == ""
    # The role line names no farm: it stays while a cycle holds the game, and without one only a scoped fresh pose counts
    assert "[STARDEW]" in B.stardew_role_block_now()
    monkeypatch.setattr(B, "_stardew_live", lambda: False)
    assert B.stardew_role_block_now() == ""
    monkeypatch.setattr(B, "_stardew_live", lambda: True)

    # Remove stop, switch cycle to ANOTHER scope (Farm B) while now.json & digest still hold Farm A -> ignored AT ONCE
    (cycle_dir / "LIVE7_STOP").unlink()
    (cycle_dir / "cycle_state.json").write_text(json.dumps(dict(state, save_id="900333444", player_id="2002")), encoding="utf-8")
    (cmd_dir / "session.json").write_text(json.dumps({"world_ready": True, "player_id": "2002"}), encoding="utf-8")
    os.utime(cycle_dir / "cycle_state.json", (now_ts + 10, now_ts + 10))
    os.utime(cmd_dir / "session.json", (now_ts + 10, now_ts + 10))
    assert B.mind_intent_block_now() == ""
    assert D.bridge_block(B) == ""
    assert MS.bridge_read_now(B) is None

    # A quiet hour (a long pause) does not make her forget; a digest older than the privacy scope is never read
    (cycle_dir / "cycle_state.json").write_text(json.dumps(state), encoding="utf-8")
    (cmd_dir / "session.json").write_text(json.dumps({"world_ready": True, "player_id": "1001"}), encoding="utf-8")
    os.utime(cycle_dir / "cycle_state.json", (now_ts + 12, now_ts + 12))
    os.utime(cmd_dir / "session.json", (now_ts + 12, now_ts + 12))
    stale_ts = now_ts - M.NOW_MAX_AGE_SEC - 30
    D.write(base, dict(digest_a, written_ts=stale_ts))
    privacy._changed_at = stale_ts - 1
    assert "[INGATAN GAME]" in D.bridge_block(B)
    privacy._changed_at = stale_ts + 1
    assert D.bridge_block(B) == ""

    # Carried why / goal_today / doing_now / heading_to / blocked NEVER cross a scope change
    M.mark_state(base, game="stardew", blocked={"since_ts": now_ts - 60, "menu_open": True},
                 observed_ts=now_ts, scope=SCOPE_A, scoped=True)
    assert M.read_now(base, now=now_ts, scope_hash=HASH_A, require_scope=True)["blocked"] is not None

    row_b = M.mark_state(base, game="stardew", game_time={"date": "Y1 spring 13", "time": 610, "place": "Cabin"},
                         observed_ts=now_ts + 1, scope=SCOPE_B, scoped=True)
    assert row_b["scope_hash"] == HASH_B
    assert row_b["why"] is None and row_b["goal_today"] == []
    assert row_b["doing_now"] is None and row_b["heading_to"] is None and row_b["blocked"] is None

    # note_doing from Farm A against Farm B's now.json is refused and leaves Farm B intact
    assert M.note_doing(base, game="stardew", doing={"skill": "pergi_ke", "arg": "mine"},
                        scope=SCOPE_A, scoped=True) is None
    assert M.read_now(base, now=now_ts + 1, scope_hash=HASH_B, require_scope=True)["heading_to"] is None

    # write_now from Farm A against Farm B's now.json resets previous carried fields
    row_a2 = M.write_now(base, game="stardew", why="niat baru alpha", goal_ref=None, goal_today=[],
                         game_time={"date": "Y1 winter 22", "time": 700, "place": "Farm"},
                         decision_ts=now_ts + 2, observed_ts=now_ts + 2, scope=SCOPE_A, scoped=True)
    assert row_a2["scope_hash"] == HASH_A and row_a2["doing_now"] is None and row_a2["blocked"] is None


# --------------------------------------------------------------------------- 7. OFF = byte-identical files, pages, planner, digest, AST guard


def test_setting_off_is_byte_identical_to_absent_and_writes_no_scope_hash(tmp_path, monkeypatch):
    from datetime import datetime as _dt
    at = _dt(2030, 1, 2, 3, 4, 5)  # `reflected_at` is the wall second: two runs must not straddle one
    monkeypatch.setattr(G, "datetime", NS(now=lambda *a: at))

    def run_flow(root: Path, cfg: dict):
        base = root / "mind"
        payload = G.build_input(base, SCOPE_A, day=10, target_day=10, mode="first_morning",
                                evidence=[], native=[], calendar=[], daily={},
                                verified=[{"ref": "r1", "day": 9, "text": "mancing di dermaga"}],
                                remembered_day=9, mind_scoped=MS.enabled(cfg))
        G.apply_reply(
            base, SCOPE_A,
            _reply(new=[_want("today", "aku mau ke kota")], summary="kemarin jalan",
                   fun=[{"text": "mancing", "ref": "r1"}]),
            payload, day=10, target_day=10, mode="first_morning", mind_scoped=MS.enabled(cfg),
        )
        M.write_now(base, game="stardew", why="ke kota", goal_ref="t10-1", goal_today=["aku mau ke kota"],
                    game_time={"date": "Y1 spring 10", "time": 800, "place": "Farm"},
                    decision_ts=1700000000.0, observed_ts=1700000000.0,
                    scope=SCOPE_A if MS.enabled(cfg) else None, scoped=MS.enabled(cfg))
        digest = D.build(base, SCOPE_A, day=10, place="Farm", now=1700000000.0, mind_scoped=MS.enabled(cfg))
        D.write(base, digest)
        rt, sess, facts = _make_runtime(base, scope=SCOPE_A, date=(1, "spring", 10), config=cfg)
        offer = {"day": {}}
        rt._live7_mind_offer(sess, facts, [S.SkillCall("jalan")], offer)
        files = {
            name: (M.game_dir(base, "stardew") / name).read_bytes()
            for name in (M.GOALS_FILE, M.JOURNEY_FILE, M.LIKES_FILE, D.FILE)
        }
        files[M.NOW_FILE] = (base / M.NOW_FILE).read_bytes()
        return files, offer, payload

    absent_files, absent_offer, absent_payload = run_flow(tmp_path / "absent", {})
    false_files, false_offer, false_payload = run_flow(tmp_path / "false", {"live7_mind_scope": False})
    assert absent_files == false_files
    assert absent_offer == false_offer
    assert absent_payload == false_payload
    for fname in (M.GOALS_FILE, M.JOURNEY_FILE, M.LIKES_FILE, M.NOW_FILE):
        assert b"scope_hash" not in absent_files[fname], fname


def test_every_mind_scope_reference_in_reactor_files_sits_under_the_setting():
    for name in ("arti_bridge.py", "arti_stardew_live7.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8-sig"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        seams = [
            n for n in ast.walk(tree)
            if (isinstance(n, ast.Name) and n.id == "mind_scope")
            or (isinstance(n, ast.alias) and n.name == "mind_scope")
        ]
        for seam in seams:
            node = seam
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.If) and "live7_mind_scope" in ast.unparse(node.test):
                    break
            else:
                pytest.fail(f"mind_scope reference outside live7_mind_scope guard in {name}")


# --------------------------------------------------------------------------- 9. Crash between two writes


def test_crash_between_two_writes_preserves_other_scope_and_recovers_cleanly(tmp_path, monkeypatch):
    base = tmp_path / "mind"

    # Seed Farm A cleanly
    payload_a = G.build_input(base, SCOPE_A, day=WINTER_22, target_day=WINTER_22, mode="first_morning",
                              evidence=[], native=[], calendar=[], daily={},
                              verified=[{"ref": "r-a", "day": WINTER_22 - 1, "text": "alpha utuh"}],
                              remembered_day=WINTER_22 - 1, mind_scoped=True)
    G.apply_reply(base, SCOPE_A, _reply(new=[_want("today", "aku mau jaga alpha")], summary="alpha utuh",
                                        fun=[{"text": "alpha", "ref": "r-a"}]),
                  payload_a, day=WINTER_22, target_day=WINTER_22, mode="first_morning", mind_scoped=True)
    a_goals_before = M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True)
    a_journey_before = M.load_journey(base, "stardew", scope=SCOPE_A, scoped=True)
    a_likes_before = M.load_likes(base, "stardew", scope=SCOPE_A, scoped=True)

    # Simulate crash during Farm B apply_reply right AFTER save_goals and BEFORE save_journey_row
    real_save_journey = M.save_journey_row
    def crash_once(*args, **kwargs):
        raise OSError("simulated power loss after save_goals")
    monkeypatch.setattr(M, "save_journey_row", crash_once)

    payload_b = G.build_input(base, SCOPE_B, day=SPRING_13, target_day=SPRING_13, mode="first_morning",
                              evidence=[], native=[], calendar=[], daily={},
                              verified=[{"ref": "r-b", "day": SPRING_13 - 1, "text": "beta baru"}],
                              remembered_day=SPRING_13 - 1, mind_scoped=True)
    with pytest.raises(OSError, match="simulated power loss"):
        G.apply_reply(base, SCOPE_B, _reply(new=[_want("today", "aku mau beta")], summary="beta baru",
                                            fun=[{"text": "beta", "ref": "r-b"}]),
                      payload_b, day=SPRING_13, target_day=SPRING_13, mode="first_morning", mind_scoped=True)

    # Farm A is 100% untouched, and Farm B readers work cleanly despite partial commit
    assert M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True) == a_goals_before
    assert M.load_journey(base, "stardew", scope=SCOPE_A, scoped=True) == a_journey_before
    assert M.load_likes(base, "stardew", scope=SCOPE_A, scoped=True) == a_likes_before
    assert len(M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True)) == 1
    assert M.load_journey(base, "stardew", scope=SCOPE_B, scoped=True) == []
    assert "beta" in D.build(base, SCOPE_B, day=SPRING_13, place="Farm", mind_scoped=True)["text"]

    # Restore writer and retry Farm B -> succeeds cleanly, Farm A still untouched
    monkeypatch.setattr(M, "save_journey_row", real_save_journey)
    payload_b2 = G.build_input(base, SCOPE_B, day=SPRING_13, target_day=SPRING_13, mode="first_morning",
                               evidence=[], native=[], calendar=[], daily={},
                               verified=[{"ref": "r-b", "day": SPRING_13 - 1, "text": "beta pulih"}],
                               remembered_day=SPRING_13 - 1, mind_scoped=True)
    res_b2 = G.apply_reply(base, SCOPE_B, _reply(summary="beta pulih", fun=[{"text": "beta", "ref": "r-b"}]),
                           payload_b2, day=SPRING_13, target_day=SPRING_13, mode="first_morning", mind_scoped=True)
    assert res_b2["new"] == []
    assert M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True) == a_goals_before
    assert M.yesterday_line(base, "stardew", SPRING_13, scope=SCOPE_B, scoped=True) == "beta pulih"


# --------------------------------------------------------------------------- 10. Two real processes writing concurrently


_WORKER_SCRIPT = r"""
import sys
from pathlib import Path
import arti_mind as M

base = Path(sys.argv[1])
scope = sys.argv[2]
tag = sys.argv[3]
n = int(sys.argv[4])

for i in range(1, n + 1):
    goals = M.load_goals(base, "stardew", scope=scope, scoped=True)
    goals.append({
        "v": 1,
        "id": f"t{i}-{tag}",
        "horizon": "today",
        "text": f"aku mau {tag} ronde {i}",
        "status": "done",
        "set_on_day": i,
        "for_day": i,
        "closed_on_day": i,
        "scope_hash": M._scope_hash(scope),
    })
    M.save_goals(base, "stardew", goals, scope=scope, scoped=True)
    M.save_journey_row(base, "stardew", {
        "v": 1,
        "schema": M.JOURNEY_SCHEMA,
        "id": M.plan_dates.label("stardew", i),
        "date": M.plan_dates.label("stardew", i),
        "day": i,
        "reflection": {"today": f"ringkasan {tag} {i}"},
        "discoveries": [],
    }, scope=scope, scoped=True)
    M.save_likes(base, "stardew", day=i, day_label=M.plan_dates.label("stardew", i),
                 fun=[{"text": f"suka {tag} {i}", "ref": f"r-{tag}-{i}"}], not_fun=[],
                 scope=scope, scoped=True)
    M.write_now(base, game="stardew", why=f"niat {tag} {i}", goal_ref=None, goal_today=[f"tujuan {tag} {i}"],
                game_time={"date": M.plan_dates.label("stardew", i), "time": 700 + i, "place": "Farm"},
                scope=scope, scoped=True)
"""


def test_two_processes_concurrent_scoped_writes_no_corruption_or_lost_rows(tmp_path):
    base = tmp_path / "mind"
    base.mkdir()
    script = tmp_path / "worker.py"
    script.write_text(_WORKER_SCRIPT, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    rounds = 20

    p_a = subprocess.Popen(
        [sys.executable, "-B", str(script), str(base), SCOPE_A, "alpha", str(rounds)],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    p_b = subprocess.Popen(
        [sys.executable, "-B", str(script), str(base), SCOPE_B, "beta", str(rounds)],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out_a, err_a = p_a.communicate(timeout=60)
    out_b, err_b = p_b.communicate(timeout=60)
    assert p_a.returncode == 0, f"worker A failed: {out_a}\n{err_a}"
    assert p_b.returncode == 0, f"worker B failed: {out_b}\n{err_b}"

    # Every file must parse cleanly with zero torn lines and all rows for BOTH scopes intact
    folder = M.game_dir(base, "stardew")
    for fname in (M.GOALS_FILE, M.JOURNEY_FILE, M.LIKES_FILE):
        lines = [line for line in (folder / fname).read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == rounds * 2, f"{fname}: expected {rounds * 2}, got {len(lines)}"
        for line in lines:
            parsed = json.loads(line)
            assert MS.is_scope_hash(parsed.get("scope_hash"))

    assert len(M.load_goals(base, "stardew", scope=SCOPE_A, scoped=True)) == rounds
    assert len(M.load_goals(base, "stardew", scope=SCOPE_B, scoped=True)) == rounds
    assert len(M.load_journey(base, "stardew", scope=SCOPE_A, scoped=True)) == rounds
    assert len(M.load_journey(base, "stardew", scope=SCOPE_B, scoped=True)) == rounds
    assert len(M.load_likes(base, "stardew", scope=SCOPE_A, scoped=True)) == rounds
    assert len(M.load_likes(base, "stardew", scope=SCOPE_B, scoped=True)) == rounds

    now_row = json.loads((base / M.NOW_FILE).read_text(encoding="utf-8"))
    assert now_row["scope_hash"] in (HASH_A, HASH_B)
    expected_tag = "alpha" if now_row["scope_hash"] == HASH_A else "beta"
    assert expected_tag in now_row["why"]
    assert all(expected_tag in t for t in now_row["goal_today"])
