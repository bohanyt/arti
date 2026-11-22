"""Skills tahap 1 (pure module): registry -> existing objectives / rules, owner grammar, filtering, boundary prompt,
choice decoding and the deterministic step program. No runtime, no IO. Evidence ceiling UNIT_TESTED."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

import arti_stardew_economy as economy
import arti_stardew_fishing as fishing
import arti_stardew_interact as interact
import arti_stardew_interact_b as interact_b
import arti_stardew_runtime as R
import arti_stardew_skills as S
import arti_stardew_skills_runtime as SR
import arti_stardew_tool_work as tool_work
import arti_stardew_world_extras as extras
from arti_stardew_decision import decode_game_decision, runtime_choice_admitted
from arti_stardew_episode import EpisodeCriterion, parse_episode_plan

TAHAP1 = {"tutup_menu", "jalan_ke", "simpan_ke_chest", "tebang", "pungut_drop", "pegang", "pecah_batu", "pergi_ke",
          "tidur", "siram"}
BRIEF = TAHAP1 | {"cangkul", "isi_gembor", "makan", "tangkap_ikan", "jual", "beli", "buat", "kirim", "elus",
                  "naik_kuda", "rawat_hewan", "ikuti_festival", "tanam", "panen", "ngobrol_hadiah"} | S.WAVE1_NAMES
EXAMPLE_ARGS = {"jalan_ke": S.SkillCall("jalan_ke", "10,12", tile=(10, 12)), "pergi_ke": S.SkillCall("pergi_ke", "kota"),
                "pegang": S.SkillCall("pegang", "kapak"), "pungut_drop": S.SkillCall("pungut_drop", "kayu"),
                "simpan_ke_chest": S.SkillCall("simpan_ke_chest", "kayu", 5), "jual": S.SkillCall("jual", "parsnip", 1),
                "beli": S.SkillCall("beli", "parsnip seeds", 1), "buat": S.SkillCall("buat", "wood fence"),
                "kirim": S.SkillCall("kirim", "parsnip", 1), "elus": S.SkillCall("elus", "kucing")}
FINISH_REASONS = {"observed_tool_effect", "observed_inventory_objective", "observed_paired_transfer_objective",
                  interact_b.FINISH_EAT, interact_b.FINISH_DAY, fishing.FINISH_REASON, extras.FINISH,
                  "observed_fish_caught_dropped_at_feet",  # ADR 107: a landed catch that did not fit the bag
                  "observed_location_change_objective", "observed_home_location_objective", "observed_goal_tile", *economy.FINISH.values(),
                  "observed_equipped_item", "observed_menu_closed", "observed_approach_arrived", "observed_morning_plan_input",
                  "observed_all_animals_cared_for", "observed_festival_seated", interact_b.FINISH_DIALOGUE_CLOSED,
                  "exited_mines", "observed_rod_attachment", "observed_fruit_pickup", "observed_ore_pickup",
                  "observed_item_pickup_objective", "observed_requested_donation", "observed_animal_adoption",
                  "observed_crab_pot_baited", "observed_crab_pot_collected",  # W1 gap B3 S24 linked phases
                  "observed_quest_delivery",  # W1 gap B4 S28 hand-in reconcile
                  # Live7 lawan (owner 2026-10-23): the combat rule's kill, or the mod's combat block with no monster near
                  "observed_monster_killed", "observed_area_clear"}


def call_for(name):
    return EXAMPLE_ARGS.get(name, S.SkillCall(name))


# ================================================================== registry

def test_registry_covers_the_brief_and_plan_7_tahap1_is_unlocked():
    # Sat gift: terima/tolak_hadiah are offered only by Live7 recovery for a pinned native gift prompt.
    gift = {"terima_hadiah", "tolak_hadiah"}
    generic = {name for name, spec in S.REGISTRY.items() if spec.family == S.F_GENERIC}
    assert set(S.REGISTRY) - gift - generic == BRIEF and gift <= set(S.REGISTRY)
    assert all(S.REGISTRY[n].preconditions == ("gift_prompt_offered",) for n in gift)
    assert not S.PRECONDITIONS["gift_prompt_offered"](S.SkillFacts(menu_open=True), None)
    assert TAHAP1 <= S.TAHAP1_UNLOCKED
    # PLAN §7: skills needing a v2 family wait for its acceptance step (never before VERIFIED LIVE).
    for name in ("jual", "beli", "buat", "kirim", "tangkap_ikan", "elus", "naik_kuda"):
        assert S.skill_locked(name) and S.skill_locked(name).startswith("acceptance_")
        assert S.skill_locked(name, {name}) is None  # owner opt-in (stardew_skills_unlocked)
    assert set(S.COMPOSITES) == {"rutinitas_pagi"}
    steps = [n for n, _ in S.COMPOSITES["rutinitas_pagi"]]
    assert steps.index("siram") < steps.index("tebang") < steps.index("simpan_ke_chest")
    assert len(steps) <= 8 and S.COMPOSITE_MAX_RUN <= 6


@pytest.mark.parametrize("name", sorted(BRIEF))
def test_every_skill_pins_an_existing_objective_with_its_existing_grammar(name):
    spec = S.REGISTRY[name]
    call = call_for(name)
    text = S.objective_text(call, S.SkillFacts(inventory={S.WOOD: 5}))
    assert spec.finish_rule and set(spec.finish_reasons) <= FINISH_REASONS
    assert spec.cost_decisions > 0 and spec.cost_inputs > 0 and spec.cost_seconds > 0 and spec.failure_modes
    assert set(spec.preconditions) <= set(S.PRECONDITIONS)
    family = spec.family
    if family == S.F_WAVE1:
        assert spec.parser == "none" and spec.objective.startswith("composition:")
        assert S.parse_skill_goal(S.SKILLS_V1_GOALS[name]).calls == (S.SkillCall(name),)
    elif family == S.F_TOOL:
        assert tool_work.parse_tool_work_goal(text) == spec.objective.split(":")[1]
    elif family == S.F_GATHER:
        assert spec.parser == "override" and S.one_tree_no_store_plan() == (EpisodeCriterion("one_tree", None, S.WOOD, False),)
        # The canonical text itself is parsed by NO grammar (the override is the only pin): no accidental objective.
        assert S.legacy_skill_call(text) is None and parse_episode_plan(text) is None
    elif family == S.F_PICKUP:
        plan = parse_episode_plan(text)
        assert plan == (EpisodeCriterion("additional", 1, S.WOOD, False, None),)
    elif family == S.F_STORE:
        assert re.fullmatch(SR._RESOURCE_GOAL, text, re.I) and text.endswith("lalu simpan") and " 5 kayu" in text
    elif family == S.F_NAV:
        objective = interact.parse_interact_objective(text)
        assert objective is not None and objective.kind == "enter_location"
    elif family == S.F_ROUTINE:
        assert interact_b.parse_routine_objective(text).kind == spec.objective.split(":")[1]
    elif family == S.F_FISH:
        assert fishing.parse_fishing_goal(text)
    elif family in (S.F_ECONOMY_MENU, S.F_ECONOMY_BIN):
        assert economy.parse_economy_objective(text).kind == spec.objective.split(":")[1]
    elif family in (S.F_EXTRAS, S.F_ANIMALS):
        assert extras.parse_extras_objective(text).kind == spec.objective.split(":")[1]
    else:  # equip / menu / walk: no runtime objective, a skill-level observed verdict
        assert spec.objective in ("none", "completion_tile|none")
        assert S.legacy_skill_call(text) is None and parse_episode_plan(text) is None
    # Every pinned objective maps back to the same skill (both directions agree).
    if spec.parser not in ("none", "override", "resource_grammar (decide_owner_turn)", "parse_episode_plan"):
        assert S.legacy_skill_call(text).name == name


@pytest.mark.parametrize("name", sorted(BRIEF))
def test_aliases_parse_to_their_skill(name):
    spec = S.REGISTRY[name]
    assert spec.aliases and len(spec.summary) <= 120
    for alias in spec.aliases:
        if spec.arg in (None, "item", "item_n"):
            call = S.parse_skill_call(alias)
            assert call is not None and call.name == name, alias


@pytest.mark.parametrize("text,expected", [
    ("tebang", [("tebang", None)]),
    ("chop a tree", [("tebang", None)]),
    ("pegang kapak", [("pegang", "kapak")]),
    ("hold the axe", [("pegang", "the axe")]),
    ("jalan ke 10,12", [("jalan_ke", "10,12")]),
    ("walk to the chest", [("jalan_ke", "chest")]),
    ("pergi ke kota", [("pergi_ke", "kota")]),
    ("pulang", [("pergi_ke", "rumah")]),
    ("simpan kayu ke chest", [("simpan_ke_chest", "kayu")]),
    ("store 3 wood in the chest", [("simpan_ke_chest", "kayu")]),
    ("pecah 1 batu", [("pecah_batu", None)]),
    ("siram 1 petak", [("siram", None)]),
    ("isi gembor", [("isi_gembor", None)]),
    ("makan", [("makan", None)]),
    ("tidur", [("tidur", None)]),
    ("tangkap 1 ikan", [("tangkap_ikan", None)]),
    ("jual 1 parsnip", [("jual", "parsnip")]),
    ("elus kucing", [("elus", "kucing")]),
    ("naik kuda", [("naik_kuda", None)]),
    ("tutup menu", [("tutup_menu", None)]),
    ("pegang kapak; tebang; simpan kayu ke chest", [("pegang", "kapak"), ("tebang", None), ("simpan_ke_chest", "kayu")]),
    ("tebang lalu simpan kayu ke chest", [("tebang", None), ("simpan_ke_chest", "kayu")]),
    ("pegang kapak lalu jalan ke 0,8", [("pegang", "kapak"), ("jalan_ke", "0,8")]),
    ("tebang satu pohon, pungut kayunya, lalu simpan ke chest", [("tebang", None), ("simpan_ke_chest", "kayu")]),
])
def test_owner_goal_grammar(text, expected):
    plan = S.parse_skill_goal(text)
    assert plan is not None and [(c.name, c.arg) for c in plan.calls] == expected and not plan.composite
    assert not any(c.optional for c in plan.calls)


def test_composite_and_refusals():
    plan = S.parse_skill_goal("Rutinitas pagi!")
    assert plan.composite == "rutinitas_pagi" and all(c.optional for c in plan.calls)
    assert S.parse_skill_goal("morning routine").composite == "rutinitas_pagi"
    for text in ("ngobrol dengan Lewis", "kumpulkan 5 kayu", "buka jurnal", "rapikan kebun", "",
                 "rutinitas pagi; tebang",                      # no nested composite
                 "tebang; tebang; tebang; tebang; tebang; tebang; tebang",  # > COMPOSITE_MAX_RUN parts
                 "pegang pedang"):                                 # unknown tool word
        assert S.parse_skill_goal(text) is None, text
    assert S.parse_skill_call("x" * 200) is None


def test_store_count_is_bounded_by_what_is_carried():
    facts = S.SkillFacts(inventory={S.WOOD: 7})
    assert S.objective_text(S.SkillCall("simpan_ke_chest", "kayu"), facts) == "kumpulkan 7 kayu lalu simpan"
    assert S.objective_text(S.SkillCall("simpan_ke_chest", "kayu", 3), facts) == "kumpulkan 3 kayu lalu simpan"
    assert S.objective_text(S.SkillCall("simpan_ke_chest", "kayu", 50), facts) == "kumpulkan 7 kayu lalu simpan"
    assert S.objective_text(S.SkillCall("jual", "parsnip", None)) == "jual semua parsnip"


# ================================================================== preconditions / admissible list

def farm(**kw):
    base = dict(location="Farm", time_of_day=700, energy=200.0, player_free=True, equipped="(T)Hoe",
                inventory={"(T)Axe": 1, "(T)Hoe": 1, S.WOOD: 5}, toolbar=("(T)Axe", "(T)Hoe"), trees=2, chests=1)
    base.update(kw)
    return S.SkillFacts(**base)


def test_preconditions_are_observable_facts():
    assert S.precondition_failures(S.SkillCall("tebang"), farm()) == []
    assert S.precondition_failures(S.SkillCall("tebang"), farm(trees=0)) == ["tree_in_view"]
    assert S.precondition_failures(S.SkillCall("tebang"), farm(energy=5.0)) == ["energy_ok"]
    assert S.precondition_failures(S.SkillCall("simpan_ke_chest", "kayu"), farm(chests=0)) == ["chest_in_view"]
    assert S.precondition_failures(S.SkillCall("pegang", "beliung"), farm()) == ["tool_owned"]  # D-B7b-2: owned anywhere
    assert S.precondition_failures(S.SkillCall("siram"), farm()) == ["can_owned", "dry_soil_in_view"]
    assert S.precondition_failures(S.SkillCall("tutup_menu"), farm()) == ["menu_closable"]  # D-B4-5
    # A menu up that nothing can close (live: ItemGrabMenu with no exit offered) never offers tutup_menu.
    assert S.precondition_failures(S.SkillCall("tutup_menu"), replace(farm(), menu_open=True)) == ["menu_closable"]
    assert S.precondition_failures(S.SkillCall("tutup_menu"), replace(farm(), menu_open=True, menu_closable=True)) == []
    assert "flag:economy" in S.precondition_failures(S.SkillCall("kirim", "parsnip"), farm())
    assert S.precondition_failures(S.SkillCall("kirim", "parsnip"), farm(flags=frozenset({"economy"}))) == []


def test_model_list_is_admissible_skills_only():
    names = [c.name for c in S.admissible_skills(farm())]
    assert {"tebang", "simpan_ke_chest", "pegang", "cangkul", "jalan_ke", "pergi_ke"} <= set(names)
    assert not {"tidur", "jual", "beli", "buat", "kirim"} & set(names)            # owner-only
    assert not {"tangkap_ikan", "elus", "naik_kuda"} & set(names)                   # locked until acceptance
    assert not {"siram", "isi_gembor", "pecah_batu", "tutup_menu", "makan", "pungut_drop"} & set(names)  # facts
    assert "tebang" not in [c.name for c in S.admissible_skills(farm(trees=0))]
    assert "tebang" not in [c.name for c in S.admissible_skills(farm(), exclude={"tebang"})]
    # Locked skills enter only with the owner's opt-in and their own facts / flags.
    unlocked = S.admissible_skills(farm(flags=frozenset({"extras"})), unlocked={"elus"})
    assert "elus" in [c.name for c in unlocked]
    # pergi_ke never offers the location the actor is already in.
    assert S.arg_choices(S.REGISTRY["pergi_ke"], farm())[0] == "rumah"
    assert "farm" not in S.arg_choices(S.REGISTRY["pergi_ke"], farm())
    assert "rumah" not in S.arg_choices(S.REGISTRY["pergi_ke"], farm(location="FarmHouse"))


# ================================================================== the boundary prompt (skills only)

def offer(facts=None, calls=None, outcomes=()):
    facts = facts or farm()
    return S.skill_offer(goal_text="rapikan kebun", calls=calls or S.admissible_skills(facts), facts=facts,
                         outcomes=outcomes, budget={"model_left": 5, "skills_left": 7}, trigger="skill_finished")


def boundary_request(skill_offer, play=True):
    memory = NS(play_mode="local_activity" if play else "direct")
    return NS(request_nonce="n1", goal_id="g1", goal_revision=2, context_ref="ctx-1", reason=S.SKILL_BOUNDARY_REASON,
              skill_offer=skill_offer, snapshot=NS(working_memory=memory))


def test_boundary_payload_has_skills_and_no_buttons_or_coordinates():
    payload = offer()
    messages = S.render_skill_messages(boundary_request(payload))
    user = json.loads(messages[1]["content"])
    assert user["mode"] == "skills" and user["echo"] == {"request_nonce": "n1", "goal_id": "g1", "goal_revision": 2,
                                                          "context_ref": "ctx-1"}
    assert "supported_actions" not in user and "snapshot" not in user and "pad_buttons" not in user
    text = messages[1]["content"]
    for button in ("ui_press", "ui_nav", "world_key", "approach_target", "use_tool", "target_token", "tile_x"):
        assert button not in text
    assert {s["name"] for s in user["skills"]} == {c.name for c in S.admissible_skills(farm())}
    assert all(set(s) <= {"name", "does", "args"} for s in user["skills"])
    assert "operation \"skill\"" in messages[0]["content"] and "never press" in messages[0]["content"]


def test_boundary_prompt_fits_the_render_reserve_even_when_large():
    """≤ 8192 render target (the provider's own fit check, schema included): outcomes / inventory trim first."""
    policy, _ = R.stardew_decision_provider_admission(__import__("tests.test_stardew_decision_provider",
                                                                 fromlist=["BASE"]).BASE)
    provider = R.ProductionDecisionProvider(policy, session=object(), background_session=object(),
                                            continuation_enabled=True, text_chat_fn=lambda *a, **k: ("", 0))
    big = farm(inventory={f"(O){i}": i + 1 for i in range(400)}, flags=frozenset({"economy", "extras"}))
    outcomes = [{"skill": "tebang", "status": "failed", "result": "x" * 180}] * 60
    payload = offer(big, calls=S.admissible_skills(big, unlocked=set(S.REGISTRY)), outcomes=outcomes)
    request = boundary_request(payload)
    messages = provider._render_messages(request)
    encoded = R._encoded_provider_request(policy.model, messages, R._decision_provider_extra_payload(True, messages=messages))
    assert R._provider_input_token_upper_bound(encoded) <= R._STARDEW_DECISION_INPUT_TOKEN_RESERVE == 8192
    # A normal boundary is far below the reserve (no trimming needed).
    small = provider._render_messages(boundary_request(offer()))
    encoded = R._encoded_provider_request(policy.model, small, R._decision_provider_extra_payload(True, messages=small))
    assert R._provider_input_token_upper_bound(encoded) < 7000  # ~5.6 k, mostly the strict schema


def decision(choice):
    return decode_game_decision(json.dumps(dict(v=1, request_nonce="n1", goal_id="g1", goal_revision=2,
                                                context_ref="ctx-1", speech="", choice=choice, activity=None)))


def skill_act(**args):
    return dict(kind="act", operation="skill", target_ref=None, args=args, evidence_refs=[])


def test_skill_choice_decoding():
    calls = S.admissible_skills(farm())
    assert S.skill_choice(decision(skill_act(name="tebang")), calls) == S.SkillCall("tebang")
    assert S.skill_choice(decision(skill_act(name="pegang", arg="kapak")), calls) == S.SkillCall("pegang", "kapak")
    assert S.skill_choice(decision(skill_act(name="simpan_ke_chest")), calls).arg == "kayu"
    assert S.skill_choice(decision(dict(kind="finish", scope="goal")), calls) == "finish"
    assert S.skill_choice(decision(dict(kind="ask_owner")), calls) == "ask_owner"
    for bad in (skill_act(name="tidur"), skill_act(name="jual", arg="parsnip"), skill_act(name="nope"),
                skill_act(name="tebang", arg="x"), skill_act(name="pegang", arg="pedang"),
                skill_act(name="tebang", extra=1), dict(kind="inspect", source="state", question="q", scope=None),
                dict(kind="act", operation="approach_target", target_ref="t", args={}, evidence_refs=[])):
        assert S.skill_choice(decision(bad), calls) is None, bad


def test_codec_never_admits_a_skill_act_as_a_button_flag_off_unchanged():
    """A model 'skill' act outside skill mode stays refused exactly as before (choice_unavailable)."""
    assert not runtime_choice_admitted(decision(skill_act(name="tebang")))


# ================================================================== deterministic step program

def mem(**kw):
    base = dict(progress_marker="ctx-1", last_action=None, last_inspect_status="current", gather_progress=None,
                transfer_progress=None, approach_progress=None, tool_work=None, last_finish=None)
    base.update(kw)
    return NS(**base)


def snap(**kw):
    base = dict(working_memory=mem(), motor=NS(tile=(3, 8), active_menu=None, is_player_free=True),
                approach_targets=(), exit_target=None, transfer_targets=(), world_interaction_target=None,
                move_through_target=None, equipped_tool=None, use_tool_available=False, gather_tool_target=None,
                tool_work_target=None, selectable_items=(), select_item_available=False, ui_key_target=None,
                ui_pad_target=None, pad_button_offer=None, economy_offer=None, reflex_fish_target=None,
                routine_objective=None, water_context=None, movement_context=NS(location="Farm"))
    base.update(kw)
    return NS(**base)


AXE = dict(slot=0, qualified_item_id="(T)Axe", tile_x=3, tile_y=8, facing=2)
SEL_AXE = dict(receiver_token="toolbar", target_token="tool-0", slot=0, qualified_item_id="(T)Axe", stack=1, quality=0)
TREE = dict(ref="ter:Farm:3:9:Tree", tile=[3, 9])
CHEST = dict(ref="obj:Farm:4:8:(BC)130", tile=[4, 8])


def gp(status, **kw):
    return NS(status=status, qualified_item_id=S.WOOD, quality=0, **kw)


def step(name, s, **kw):
    return S.plan_step(S.SkillCall(name), s, **kw)


def test_protocol_inspect_first_unreconciled_fails_terminal_done():
    assert step("tebang", snap(working_memory=mem(last_inspect_status=None))).choice["kind"] == "inspect"
    bad = NS(cleanup_confirmed=False, lifecycle="completed")
    assert step("tebang", snap(working_memory=mem(last_action=bad))) == S.Step("fail", reason="action_unreconciled")
    done = mem(progress_marker="terminal", last_finish=NS(reason="observed_tool_effect"))
    assert step("pecah_batu", snap(working_memory=done)) == S.Step("done", reason="observed_tool_effect")
    assert step("makan", snap(), finish_ready=True).choice == S.FINISH


def test_gather_select_approach_swing_pickup_finish():
    work = mem(gather_progress=gp("WORK_REMAINING"))
    s = snap(working_memory=work, equipped_tool=dict(AXE, qualified_item_id="(T)Hoe"), select_item_available=True,
             selectable_items=(SEL_AXE,), approach_targets=(TREE,))
    assert step("tebang", s).choice == S.act("select_item", SEL_AXE)
    s = replace_ns(s, equipped_tool=AXE)
    assert step("tebang", s).choice == S.act("approach_target", {}, TREE["ref"])
    s = replace_ns(s, gather_tool_target=TREE["ref"], use_tool_available=True)
    assert step("tebang", s).choice == S.act("world_key.use_tool", dict(AXE, ticks=S.GATHER_TICKS))
    drop = dict(ref="drop:abc", tile=[2, 9], reach="DIRECT_TARGET", qualified_item_id=S.WOOD, quality=0)
    pending = snap(working_memory=mem(gather_progress=gp("PENDING_PICKUP")), approach_targets=(drop,))
    assert step("tebang", pending).choice == S.act("approach_target", {}, "drop:abc")
    now = [100.0]
    state = {"clock": lambda: now[0]}
    nothing = snap(working_memory=mem(gather_progress=gp("PENDING_PICKUP")))
    first = S.plan_step(S.SkillCall("tebang"), nothing, state=state)
    # D-B5b-2: a 25 s wait is paced by time (at most WAIT_INSPECTS_MAX inspects), not at the 0.3 s floor.
    assert first.kind == "decision" and first.choice["kind"] == "inspect" and first.pace == S.PICKUP_WAIT_S / S.WAIT_INSPECTS_MAX
    now[0] += S.PICKUP_WAIT_S  # waiting is bounded by time, not by how fast decisions come
    assert S.plan_step(S.SkillCall("tebang"), nothing, state=state).kind == "decision"
    now[0] += 0.1
    assert S.plan_step(S.SkillCall("tebang"), nothing, state=state).reason == "pickup_stalled"
    # Progress (a real act) restarts the wait window.
    state2 = {"clock": lambda: now[0]}
    S.plan_step(S.SkillCall("tebang"), nothing, state=state2)
    S.plan_step(S.SkillCall("tebang"), pending, state=state2)
    assert not [k for k in state2 if k.startswith("wait:")]
    assert step("tebang", snap(working_memory=mem(gather_progress=gp("INVENTORY_SATISFIED")))).choice == S.FINISH
    assert step("tebang", snap(working_memory=mem(gather_progress=gp("FULL_BAG")))).reason == "gather_full_bag"
    assert step("tebang", snap(working_memory=work)).reason == "no_tree"  # D-B7-4: terminal, not ask_owner


def replace_ns(ns, **kw):
    return NS(**{**vars(ns), **kw})


def test_store_offered_acts_in_order_then_finish():
    store = mem(gather_progress=gp("INVENTORY_SATISFIED"))
    transfer = dict(slot=1, source_fact_id="obj:Farm:4:8:(BC)130")
    assert step("simpan_ke_chest", snap(working_memory=store, transfer_targets=(transfer,))).choice == \
        S.act("container_transfer", transfer)
    assert step("simpan_ke_chest", snap(working_memory=store, world_interaction_target={"kind": "chest"})).choice == \
        S.act("world_interact", {})
    assert step("simpan_ke_chest", snap(working_memory=store, approach_targets=(TREE, CHEST))).choice == \
        S.act("approach_target", {}, CHEST["ref"])
    done = mem(gather_progress=gp("INVENTORY_SATISFIED"), transfer_progress={"transferred": 5, "requested": 5})
    assert step("simpan_ke_chest", snap(working_memory=done)).choice == S.FINISH
    assert step("simpan_ke_chest", snap(working_memory=store, exit_target={"x": 1})).choice == S.act("container_exit", {})


def test_tool_work_select_swing_turn_and_failures():
    progress = NS(status="WORK_REMAINING", tool_qid="(T)Pickaxe", kind="break_rock", reason="r")
    s = snap(working_memory=mem(tool_work=progress), equipped_tool=AXE, select_item_available=True,
             selectable_items=(dict(SEL_AXE, slot=3, qualified_item_id="(T)Pickaxe"),))
    assert step("pecah_batu", s).choice["operation"] == "select_item"
    pick = dict(AXE, qualified_item_id="(T)CopperPickaxe")
    s = snap(working_memory=mem(tool_work=progress), equipped_tool=pick, use_tool_available=True,
             tool_work_target="obj:Farm:3:9:Stone")
    assert step("pecah_batu", s).choice == S.act("world_key.use_tool", dict(pick, ticks=S.TOOL_TICKS))
    arrived = mem(tool_work=progress, approach_progress={"status": "ARRIVED", "target_ref": "obj:s"})
    s = snap(working_memory=arrived, equipped_tool=pick, approach_targets=(dict(ref="obj:s", tile=[4, 8]),))
    assert step("pecah_batu", s).choice == S.act("world_key.move_cardinal", {"direction": "right", "ticks": 1})
    gone = NS(status="UNAVAILABLE", tool_qid="(T)Pickaxe", kind="break_rock", reason="no_target")
    assert step("pecah_batu", snap(working_memory=mem(tool_work=gone))).kind == "fail"
    refill = NS(status="NEEDS_REFILL", tool_qid="(T)WateringCan", kind="water_tile", reason="r")
    assert step("siram", snap(working_memory=mem(tool_work=refill))).reason == "needs_refill"
    effect = NS(status="EFFECT_OBSERVED", tool_qid="(T)Hoe", kind="till_tile", reason="r")
    assert step("cangkul", snap(working_memory=mem(tool_work=effect))).choice == S.FINISH


def test_menu_close_prefers_close_cancel_advance_and_skill_level_verdict():
    keys = [{"key": "advance", "target_token": "a"}, {"key": "close", "target_token": "c"}]
    key = {"receiver_token": "rcv:1", "menu_type": "DialogueBox", "kind": "dialogue", "keys": keys, "options": []}
    s = snap(ui_key_target=key, motor=NS(tile=(1, 1), active_menu={"type": "DialogueBox"}, is_player_free=False))
    assert step("tutup_menu", s).choice == S.act("ui_key", {"receiver_token": "rcv:1", "menu_type": "DialogueBox",
                                                            "kind": "dialogue", "key": "close", "target_token": "c"})
    pad = {"pins": {"receiver_token": "r", "menu_type": "GameMenu", "context": "game_menu", "target_token": "t"},
           "snapped_id": 7, "press": {"A": "x", "B": "close"}, "nav": True, "tabs": []}
    s = snap(ui_pad_target=pad, motor=NS(tile=(1, 1), active_menu={"type": "GameMenu"}, is_player_free=False))
    assert step("tutup_menu", s).choice["args"]["btn"] == "B"
    assert step("tutup_menu", snap()) == S.Step("done", reason="observed_menu_closed")


def test_equip_walk_nav_extras_fish_economy():
    assert S.plan_step(S.SkillCall("pegang", "kapak"), snap(equipped_tool=AXE)) == \
        S.Step("done", reason="observed_equipped_item")
    # D-B7b-2: not in the toolbar -> a paced wait for the runtime's item_to_toolbar stage, then item_not_in_toolbar.
    now = [0.0]
    equip_state = {"clock": lambda: now[0]}
    assert S.plan_step(S.SkillCall("pegang", "kapak"), snap(), state=equip_state).kind == "decision"
    now[0] += S.EQUIP_STAGE_WAIT_S + 1
    assert S.plan_step(S.SkillCall("pegang", "kapak"), snap(), state=equip_state).reason == "item_not_in_toolbar"
    walk = S.SkillCall("jalan_ke", "5,8", tile=(5, 8))
    state = {}
    assert S.plan_step(walk, snap(), state=state).choice["args"] == {"direction": "right",
                                                                     "ticks": S.WALK_TICKS_PER_STEP}
    # Discarded decisions (no new published move) never count as blocked; two zero-displacement moves do.
    assert S.plan_step(walk, snap(), state=state).kind == "decision"
    def moved(rid, a, b):
        return snap(working_memory=mem(last_action=NS(request_id=rid, cleanup_confirmed=True, lifecycle="completed",
                                                      before={"tile": a}, after={"tile": b})))
    assert S.plan_step(walk, moved("m1", [3, 8], [3, 8]), state=state).kind == "decision"
    assert S.plan_step(walk, moved("m1", [3, 8], [3, 8]), state=state).kind == "decision"  # same receipt
    assert S.plan_step(walk, moved("m2", [3, 8], [3, 8]), state=state).reason == "walk_blocked"
    assert S.plan_step(walk, snap(motor=NS(tile=(5, 8), active_menu=None, is_player_free=True))).choice == S.FINISH
    target_walk = S.SkillCall("jalan_ke", "chest")
    state = {}
    assert S.plan_step(target_walk, snap(approach_targets=(CHEST,)), state=state).choice["target_ref"] == CHEST["ref"]
    there = snap(working_memory=mem(approach_progress={"status": "ARRIVED", "target_ref": CHEST["ref"]}))
    assert S.plan_step(target_walk, there, state=state) == S.Step("done", reason="observed_approach_arrived")
    objective = interact.InteractObjective("enter_location", alias="Town", target="Town", start_location="Farm")
    warp = {"kind": "warp", "source_fact_id": "warp:Farm:1,1->Town"}
    assert S.plan_step(S.SkillCall("pergi_ke", "kota"), snap(move_through_target=warp),
                       state={"nav_objective": objective}).choice == S.act("move_through", {})
    pet = snap(world_interaction_target={"kind": "pet"})
    assert S.plan_step(S.SkillCall("elus", "kucing"), pet).choice == S.act("world_interact", {})
    assert S.plan_step(S.SkillCall("tangkap_ikan"), snap(reflex_fish_target={"x": 1})).choice == S.act("reflex_fish", {})
    pad = {"pins": {"receiver_token": "r", "menu_type": "ShopMenu", "context": "shop", "target_token": "t"},
           "snapped_id": 3, "press": {"A": "buy", "B": "close"}, "nav": True, "tabs": [], "payload": {"A": {"qty": 2}}}
    press = S.plan_step(S.SkillCall("beli", "parsnip seeds", 2), snap(ui_pad_target=pad)).choice
    assert press["operation"] == "ui_press" and press["args"]["btn"] == "A" and press["args"]["qty"] == 2
    scan = S.plan_step(S.SkillCall("beli", "parsnip seeds"), snap(ui_pad_target=dict(pad, press={"B": "close"})))
    assert scan.choice["operation"] == "ui_nav" and scan.choice["args"]["dir"] == "down"
    assert S.plan_step(S.SkillCall("jual", "parsnip"), snap()).reason == "shop_not_open"


def test_activity_declaration_for_skill_acts_only():
    act = S.act("approach_target", {}, "ter:Farm:3:9:Tree")
    activity = S.activity_for(S.SkillCall("tebang"), act)
    assert activity["activity"] == "skill tebang" and activity["target"] == "ter:Farm:3:9:Tree"
    assert all(0 < len(str(v)) <= 160 for v in activity.values() if v is not None)
    assert S.activity_for(S.SkillCall("tebang"), S.INSPECT) is None



def test_offered_skills_are_capped_and_the_render_fits_with_120_registered(monkeypatch):
    """F7: a large registry never overflows the boundary request: ≤ MAX_OFFERED offered, render trimmed to fit."""
    for i in range(120):
        monkeypatch.setitem(S.REGISTRY, f"extra_{i:03d}", replace(S.REGISTRY["cangkul"], name=f"extra_{i:03d}",
                                                                  preconditions=(), summary="x" * 110))
    facts = farm()
    every = set(S.REGISTRY)
    calls = S.admissible_skills(facts, unlocked=every, limit=S.MAX_OFFERED)
    assert len(calls) == S.MAX_OFFERED == 24
    assert len(S.admissible_skills(facts, unlocked=every)) > 120
    policy, _ = R.stardew_decision_provider_admission(__import__("tests.test_stardew_decision_provider",
                                                                 fromlist=["BASE"]).BASE)
    provider = R.ProductionDecisionProvider(policy, session=object(), background_session=object(),
                                            continuation_enabled=True, text_chat_fn=lambda *a, **k: ("", 0))
    big = offer(facts, calls=S.admissible_skills(facts, unlocked=every))  # even uncapped, the render trims the skills list
    messages = provider._render_messages(boundary_request(big))
    encoded = R._encoded_provider_request(policy.model, messages, R._decision_provider_extra_payload(True, messages=messages))
    assert R._skill_prompt_token_estimate(encoded) <= R._STARDEW_DECISION_INPUT_TOKEN_RESERVE
    assert R._provider_input_token_upper_bound(encoded) <= R._STARDEW_DECISION_INPUT_TOKEN_HARD_BOUND
    assert 1 <= len(S.rendered_skill_names(messages)) < 120


def test_choice_arg_must_be_offered():
    """F3: the decoder accepts only an arg the boundary listed for that skill."""
    facts = farm()
    calls = S.admissible_skills(facts)
    args = S.offered_args(calls, facts)
    assert args["simpan_ke_chest"] == ["kayu"]
    assert S.skill_choice(decision(skill_act(name="simpan_ke_chest", arg="kayu")), calls, args).arg == "kayu"
    assert S.skill_choice(decision(skill_act(name="simpan_ke_chest", arg="batu")), calls, args) is None
    assert S.skill_choice(decision(skill_act(name="pegang", arg="beliung")), calls, args) is None  # not in toolbar
    assert S.skill_choice(decision(skill_act(name="pergi_ke", arg="farm")), calls, args) is None   # already there
