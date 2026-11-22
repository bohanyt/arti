"""Explicit, gated trial adapter for the existing Stardew runtime. Import is inert."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import queue
from pathlib import Path
import subprocess
import sys
import threading
import time
from dataclasses import asdict, is_dataclass

ROOT = Path(__file__).resolve().parents[1]
MODES = ("dry-run", "native-no-network", "model-no-game", "combined")
ENVELOPE = "https://github.example.invalid/owner/private-lab-repository/issues/example-a"


def identity(root=ROOT, artifact=None):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    return {"source": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
            "dll": hashlib.sha256(Path(artifact).read_bytes()).hexdigest() if artifact else None}


INPUT_APPROVAL_DEFAULT = 48
INPUT_APPROVAL_MAX = 160  # = COMBAT_INPUT_ENVELOPE_MAX = SKILLS_INPUT_ENVELOPE_MAX


def part_a_goal(text):
    from arti_stardew_part_a import recognizes
    return recognizes(text)


def part_a_approval(grant):
    return part_a_goal(grant.get("owner_text")) and grant.get("campaign") == "skills_w1_part_a"


def part_b_goal(text):
    from arti_stardew_part_b import recognizes
    return recognizes(text)


def part_c_goal(text):
    from arti_stardew_part_c import recognizes
    return recognizes(text)


def live7_goal(text):
    from arti_stardew_live7 import recognizes
    return recognizes(text)


def live7_approval(grant):
    return (live7_goal(grant.get("owner_text")) and grant.get("campaign") == "live7"
            and grant.get("automatic") is True)


def campaign_approval(grant):
    return (part_a_approval(grant) or part_b_goal(grant.get("owner_text"))
            and grant.get("campaign") == "skills_w1_part_b"
            or part_c_goal(grant.get("owner_text")) and grant.get("campaign") == "skills_w1_part_c")


def input_approval_max(owner_text):
    """Explicit standalone S15/animal approvals use their own caps; defaults stay unchanged."""
    if part_a_goal(owner_text) or part_b_goal(owner_text) or part_c_goal(owner_text):
        from arti_stardew_part_a import CAMPAIGN_INPUT
        # Owner caps: A 1500 (2026-10-19), B 1300 (2026-10-18), C 1300 (2026-10-20).
        return CAMPAIGN_INPUT["c" if part_c_goal(owner_text) else "b" if part_b_goal(owner_text) else "a"]
    from arti_stardew_animal_routine import animal_input_envelope
    if animal_input_envelope(owner_text or "") is not None:
        return 280
    crab = _crab_collect_alone(owner_text)
    if crab is not None:
        return crab[1]
    mines = _lawan_mine_alone(owner_text)
    # Owner 2026-10-18: an approval naming S15 lawan_mine alone may use its own (raised) input envelope.
    return mines[1] if mines is not None else INPUT_APPROVAL_MAX


def _crab_collect_alone(owner_text):
    """Owner's linked S24 day-2 witness only; other plans retain the ordinary approval cap."""
    import arti_stardew_skills as skills
    text = re.sub(r"^.*?\b(?:main mandiri|main sendiri)\s*:\s*", "", owner_text or "", flags=re.I)
    plan = skills.parse_skill_goal(text)
    if plan and plan.calls == (skills.SkillCall("crab_pot", "ambil"),):
        return skills.CRAB_POT_COLLECT_COSTS
    # W1 gap B4 (CT 2026-10-19, flagged for owner veto): the owner-named S28 delivery alone.
    if plan and len(plan.calls) == 1 and plan.calls[0].name == "surat_quest" and plan.calls[0].arg:
        return skills.QUEST_DELIVERY_COSTS
    return None


def _lawan_mine_alone(owner_text):
    import arti_stardew_skills as skills
    text = re.sub(r"^.*?\b(?:main mandiri|main sendiri)\s*:\s*", "", owner_text or "", flags=re.I)
    plan = skills.parse_skill_goal(text)
    if plan and len(plan.calls) == 1 and plan.calls[0].name == "lawan_mine":
        return skills.skill_costs(plan.calls[0])
    # W1 gap B5 (owner 2026-10-19): the separate Farm -> Mine route witness names its own envelope.
    if plan and len(plan.calls) == 1 and skills.skill_costs(plan.calls[0]) == skills.MINE_ROUTE_COSTS:
        return skills.MINE_ROUTE_COSTS
    return None


def seconds_approval_max(owner_text):
    """Standalone S15 gets 1200s; existing sleep/animal plans retain 900s."""
    if part_a_goal(owner_text) or part_b_goal(owner_text) or part_c_goal(owner_text):
        return 5400
    if input_approval_max(owner_text) == 280:
        return 900
    mines = _lawan_mine_alone(owner_text)
    if mines is not None:
        return mines[2]  # owner 2026-10-18: S15 alone 1200 s
    import arti_stardew_skills as skills
    text = re.sub(r"^.*?\b(?:main mandiri|main sendiri)\s*:\s*", "", owner_text or "", flags=re.I)
    plan = skills.parse_skill_goal(text)
    return 900 if plan and any(skills.skill_costs(c)[2] == 900 for c in plan.calls) else 600


def steps_approval_max(owner_text):
    """Only an approval naming a registered skill may use its decision envelope."""
    if part_a_goal(owner_text) or part_b_goal(owner_text) or part_c_goal(owner_text):
        return 4096
    crab = _crab_collect_alone(owner_text)
    if crab is not None:
        return crab[0]
    mines = _lawan_mine_alone(owner_text)
    return mines[0] if mines is not None else 128


def validate(mode, grant, actual, profile, session, now):
    """Local approval is owner-controlled input, never a model/CLI confirmation."""
    if mode not in MODES or mode == "dry-run":
        raise ValueError("execution_mode_required")
    if not isinstance(grant, dict) or grant.get("envelope") != ENVELOPE:
        raise ValueError("owner_envelope_missing")
    if grant.get("status") != "APPROVED" or not grant.get("owner_resolution_url"):
        raise ValueError("owner_resolution_missing")
    if mode not in grant.get("modes", []):
        raise ValueError("mode_not_granted")
    if actual.get("dirty") or grant.get("identity") != actual:
        raise ValueError("artifact_mismatch")
    if grant.get("profile") != profile or grant.get("session") != session:
        raise ValueError("profile_or_session_mismatch")
    if live7_goal(grant.get("owner_text")):
        if (not live7_approval(grant) or mode != "combined"
                or grant.get("limits") != dict.fromkeys(("provider", "input", "seconds", "steps"))
                or grant.get("expires_at") is not None
                or type(grant.get("issued_at")) not in (int, float)
                or not math.isfinite(grant["issued_at"]) or now < grant["issued_at"]
                or not actual.get("dll") or grant.get("save_resolved") is not True):
            raise ValueError("live7_automatic_approval_required")
        return
    for key in ("issued_at", "expires_at"):
        value = grant.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid_approval_time")
    if not grant["issued_at"] <= now < grant["expires_at"]:
        raise ValueError("approval_expired")
    if mode != "model-no-game" and (not actual.get("dll") or grant.get("save_resolved") is not True):
        raise ValueError("native_save_or_artifact_unresolved")
    limits = grant.get("limits", {})
    if part_a_goal(grant.get("owner_text")) or part_b_goal(grant.get("owner_text")) or part_c_goal(grant.get("owner_text")):
        from arti_stardew_part_a import limits_for
        part = "c" if part_c_goal(grant.get("owner_text")) else "b" if part_b_goal(grant.get("owner_text")) else "a"
        if not campaign_approval(grant) or limits != limits_for(part) or grant["expires_at"]-now < 1:
            raise ValueError("part_c_explicit_approval_required" if part_c_goal(grant.get("owner_text")) else
                             "part_b_explicit_approval_required" if part_b_goal(grant.get("owner_text"))
                             else "part_a_explicit_approval_required")
    elif grant.get("campaign"):
        raise ValueError("campaign_goal_mismatch")
    # FIXB24 (live batch 21: a 134-input combat + exit approval was impossible): the approved input may be raised by the
    # operator's explicit `prep_trial.py approval --inputs N` (<= INPUT_APPROVAL_MAX; the owner default stays 48).
    for key, maximum in (("provider", 240), ("input", input_approval_max(grant.get("owner_text"))), ("seconds", seconds_approval_max(grant.get("owner_text"))), ("steps", steps_approval_max(grant.get("owner_text")))):
        if type(limits.get(key)) is not int or not 0 < limits[key] <= maximum:
            raise ValueError("invalid_trial_limit:" + key)
    if limits["input"] > INPUT_APPROVAL_DEFAULT and not (isinstance(grant.get("input_decision"), str)
                                                         and grant["input_decision"].strip()):
        # FIXB24 review: an approved input above the owner default is an explicit, recorded operator decision.
        raise ValueError("input_decision_missing")
    if not isinstance(grant.get("owner_text"), str) or not grant["owner_text"].strip():
        raise ValueError("owner_objective_missing")


class TrialBudget:
    """Lower admission caps in the EXISTING journal; no replay/recovery execution.

    A trial may start only once in its approved journal. Interrupted reservations
    remain consumed. Rotating run IDs or goals cannot recover budget.
    """
    def __init__(self, journal, grant, *, clock=time.time):
        self.path = Path(journal) / "g1_journal.jsonl"
        self.grant, self.clock = grant, clock
        from arti_stardew_source_tags import grant_tags
        self.tags = grant_tags(grant)  # Live7 WP4: domain/cycle/day_trial on every trial journal line
        self.lock = threading.RLock()
        self.used = {"input": 0, "provider": 0}
        self.decisions = 0
        self.started = clock()
        self.stopped = False
        self._fresh_startup_bytes = None
        self._fresh_startup_path = None
        self._fresh_startup_identity = None
        self._startup_consumed = False

    def stop(self):
        """Irreversible for this journal; never refund outstanding reservations."""
        with self.lock:
            self.stopped = True

    def _append(self, event):
        from arti_stardew_source_tags import tagged
        event = tagged(event, self.tags)
        with self.path.open("a", encoding="utf8") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def begin(self):
        # Journal identity is pinned in approval; callers cannot choose a new run path.
        if str(self.path.parent.resolve()) != self.grant.get("journal_path"):
            raise ValueError("journal_identity_mismatch")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            raise ValueError("existing_journal_requires_reconciliation")
        # Exclusive reservation prevents a second executor/process from starting.
        initial = (json.dumps({"ev": "trial_started", "at": self.started,
                               "envelope": ENVELOPE, **self.tags}) + "\n").encode("utf8")
        with self.path.open("xb") as f:
            f.write(initial)
            f.flush(); os.fsync(f.fileno())
            stat = os.fstat(f.fileno())
            self._fresh_startup_identity = (stat.st_dev, stat.st_ino)
        self._fresh_startup_path = self.path.resolve()
        self._fresh_startup_bytes = initial

    def admit_runtime_startup(self, journal_path):
        """Consume only this instance's untouched exclusive startup reservation.

        This is never reconstructed from disk. Even a failed claim is consumed;
        prior journals, prior spend and changed/copied files cannot enable READY.
        """
        with self.lock:
            if self._startup_consumed:
                raise ValueError("fresh_trial_startup_consumed")
            self._startup_consumed = True
            if (self._fresh_startup_bytes is None or self.stopped
                    or any(self.used.values()) or self.decisions
                    or Path(journal_path).resolve() != self._fresh_startup_path
                    or self.path.resolve() != self._fresh_startup_path):
                raise ValueError("fresh_trial_startup_unavailable")
            try:
                stat = self.path.stat()
                valid = ((stat.st_dev,stat.st_ino) == self._fresh_startup_identity
                         and self.path.read_bytes() == self._fresh_startup_bytes)
            except OSError as exc:
                raise ValueError("fresh_trial_startup_unavailable") from exc
            if not valid:
                raise ValueError("fresh_trial_startup_journal_changed")
            self.check_time()
            # Fsync the consumed claim before runtime opens receiver/worker.
            self._append({"ev":"trial_runtime_startup_claimed","at":self.clock()})

    def check_time(self):
        now = self.clock()
        if live7_approval(self.grant):
            if now < self.started:
                raise ValueError("trial_clock_reversed")
            return
        if now < self.started or now >= min(self.grant["expires_at"], self.started + self.grant["limits"]["seconds"]):
            raise ValueError("trial_deadline")

    @property
    def deadline_unix_ms(self):
        if live7_approval(self.grant):
            return None
        return int(min(self.grant["expires_at"], self.started + self.grant["limits"]["seconds"]) * 1000)

    def admit(self, kind):
        with self.lock:
            if self.stopped:
                raise ValueError("trial_stopped")
            self.check_time()
            if kind == "decision":
                if not live7_approval(self.grant) and self.decisions >= self.grant["limits"]["steps"]:
                    raise ValueError("trial_decision_limit")
                self._append({"ev": "trial_decision", "at": self.clock(), "index": self.decisions + 1})
                self.decisions += 1
                return
            if not live7_approval(self.grant) and self.used[kind] >= self.grant["limits"][kind]:
                raise ValueError("trial_" + kind + "_limit")
            # Reserve before effect. A later exception is never refunded/replayed.
            self._append({"ev": "trial_reservation", "kind": kind, "at": self.clock(),
                          "index": self.used[kind] + 1})
            self.used[kind] += 1


class ObservedDriver:
    """Mechanics-only decisions from the same current request/catalog, no fixture plan."""
    def complete(self, request):
        snap = request.snapshot
        mem = snap.working_memory
        choice = {"kind": "inspect", "source": "state", "question": "Current objective facts", "scope": None}
        activity = None
        progress = asdict(mem.gather_progress) if mem.gather_progress else None
        if mem.last_action and (not mem.last_action.cleanup_confirmed or mem.last_action.world_effect_status == "uncertain"):
            choice = {"kind": "ask_owner"}
        elif mem.last_inspect_status == "current":
            episode=mem.activity_episode or {}
            criterion=episode.get("criterion",{})
            storing=criterion.get("store",False) if isinstance(criterion,dict) else ("simpan" in mem.goal_text or "store" in mem.goal_text)
            finish={"kind":"finish","scope":"activity" if episode.get("criterion_available") else "goal"}
            if snap.exit_target:
                choice=self.act("container_exit",snap.exit_target)
            elif mem.transfer_progress and mem.transfer_progress.get("transferred", 0) >= mem.transfer_progress.get("requested", 1):
                choice = finish
            elif progress and progress.get("remaining") == 0:
                if snap.transfer_targets:
                    choice = self.act("container_transfer", snap.transfer_targets[0])
                elif snap.world_interaction_target:
                    choice = self.act("world_interact", snap.world_interaction_target)
                elif storing:
                    choice = self.approach(snap, "chest")
                else:
                    choice = finish
            elif progress:
                tool = "(T)Axe" if progress["qualified_item_id"] == "(O)388" else "(T)Pickaxe"
                selection = next((x for x in snap.selectable_items if x.get("qualified_item_id") == tool), None)
                targets = [t for t in snap.approach_targets if ("tree" if tool=="(T)Axe" else "stone") in t["ref"].lower()]
                reached = any(abs(t["tile"][0]-snap.motor.tile[0])+abs(t["tile"][1]-snap.motor.tile[1])==1
                    and ((0,-1),(1,0),(0,1),(-1,0))[(snap.equipped_tool or {}).get("facing",0)] ==
                        (t["tile"][0]-snap.motor.tile[0],t["tile"][1]-snap.motor.tile[1]) for t in targets)
                if progress.get("status")=="PENDING_PICKUP":
                    drop = next((t for t in snap.approach_targets
                        if t.get("reach") == "DIRECT_TARGET" and t.get("qualified_item_id") == progress["qualified_item_id"]
                        and t.get("quality") == progress["quality"] and tuple(t["tile"]) != snap.motor.tile), None)
                    choice = (self.act("approach_target", {}, drop["ref"]) if drop else
                        {"kind":"inspect","source":"state","question":"Observe pending pickup","scope":None})
                elif targets and not reached:
                    choice = self.approach(snap,"tree" if tool=="(T)Axe" else "stone")
                elif snap.equipped_tool and snap.equipped_tool.get("qualified_item_id") == tool and snap.use_tool_available:
                    choice = self.act("world_key.use_tool", dict(snap.equipped_tool, ticks=3))
                elif selection and snap.select_item_available:
                    choice = self.act("select_item", selection)
                else:
                    choice = self.approach(snap, "tree" if tool == "(T)Axe" else "stone")
            else:
                choice = {"kind": "ask_owner"}
        if choice["kind"] == "act":
            activity = {"activity": "bounded owner objective", "purpose": "mechanics trial",
                        "target": choice.get("target_ref"), "next_step": choice["operation"]}
        return json.dumps(dict(v=1, request_nonce=request.request_nonce, goal_id=request.goal_id,
            goal_revision=request.goal_revision, context_ref=request.context_ref, speech="",
            choice=choice, activity=activity))

    @staticmethod
    def act(op, args, target=None):
        return dict(kind="act", operation=op, target_ref=target, args=dict(args), evidence_refs=[])

    def approach(self, snap, family):
        for target in snap.approach_targets:
            if family.casefold() in json.dumps(target).casefold() or family == "chest" and "(BC)130" in target["ref"]:
                return self.act("approach_target", {}, target["ref"])
        return {"kind": "ask_owner"}


class SimulatedCommands:
    """In-memory command adapter, never calls the native module's IO functions.

    Declared synthetic mechanics: a finite two-cycle tree, visible drop, then
    adjacent pickup on a later sample. Only current presented facts enter stores.
    Selection and the accepted chest deposit family use declared synthetic receiver transitions.
    Generic menu exit/day families remain unavailable; no real game physics are inferred.
    """
    simulated = True

    def __init__(self, folder, *, start_tile=(3,8), selected_slot=0, with_container=False):
        if (not isinstance(start_tile,(list,tuple)) or len(start_tile)!=2
                or any(type(v) is not int or not 0<=v<=4096 for v in start_tile)
                or type(selected_slot) is not int or selected_slot not in (0,2)
                or type(with_container) is not bool):
            raise ValueError("invalid_simulated_setup")
        from arti_stardew import StardewStateStore
        from arti_stardew_observation import RichObservationStore
        self.folder = Path(folder)
        self._sensor_stop = threading.Event()
        self._sensor_thread = None
        self._sample_lock = threading.RLock()
        self.state, self.observations = StardewStateStore(), RichObservationStore()
        self.seq = 1; self.cycles = 0; self.count = 0; self.drop_at = None
        self._world_ticks = 0
        self.date = dict(season="spring",day=1,year=1)
        self.tile = list(start_tile); self.facing = 2; self.last_use = None
        self.selected_slot=selected_slot; self.with_container=with_container
        self.selection_supported=selected_slot!=0
        self.menu=None;self.chest_count=10;self.generation=4;self.ui_revision=1
        # WS2 day-start fixture (explicit, synthetic): a staged end-of-night menu stack.
        self.night=[];self.night_staged=False;self.night_seq=0;self.ui_key_calls=[];self.ui_key_fault=None
        self.professions=set();self.save_ticks=3;self.native_progression_ticks=2
        self.raw = dict(protocol=1, session_id="simulation-command", runtime_instance_id="simulation-runtime",
            player_id=-1, player_name="SimulatedActor", screen_id=0, location_id="Farm",
            is_main_player=False, is_split_screen=False, inbox_enabled=True, world_press_enabled=True,
            use_tool_enabled=True, select_item_enabled=True, world_interaction_enabled=with_container,
            container_transfer_enabled=with_container, container_exit_enabled=with_container, ui_key_enabled=False, world_ready=True, input_busy=False, inflight_request_id=None,
            owner_id=None, generation=None, control_seq=None, lease_until_unix_ms=None,
            stop_latched=False, record_faulted=False, release_requested=False, release_acked=True)
        self.state.catat(dict(ev="hello", seq=1, player_id=-1, screen_id=0,
            player_name="SimulatedActor", is_main_player=False, is_split_screen=False))
        self.advance()

    def folder_perintah(self, ignored=None): return self.folder

    def baca_sesi(self, ignored=None, *, retry_detik=0.0):  # retry is a real-file concern; the simulation has no rename race
        from arti_stardew_command import SesiPerintah
        return SesiPerintah(session_id=self.raw["session_id"], player_id=-1, player_name="SimulatedActor",
            location_id="Farm", screen_id=0, is_main_player=False, is_split_screen=False,
            inbox_enabled=True, menu_step_enabled=False, water_once_enabled=False, world_press_enabled=True,
            world_ready=True, inflight_request_id=None, input_busy=False, mentah=dict(self.raw))

    def tulis_kontrol(self, kind, **kw):
        if kind == "acquire":
            self.raw.update(owner_id=kw["owner_id"], generation=kw["generation"], control_seq=kw["control_seq"],
                            lease_until_unix_ms=int(time.time()*1000)+1500)
        elif kind == "release":
            self.raw.update(owner_id=None, generation=None, control_seq=None, lease_until_unix_ms=None)
        elif kind == "renew":
            self.raw["lease_until_unix_ms"] = int(time.time()*1000)+1500
        else: raise ValueError("unsupported_simulated_control")

    def tulis_stop(self, **kw):
        self.raw.update(owner_id=None, generation=None, control_seq=None, lease_until_unix_ms=None,
                        stop_latched=True, release_acked=True)

    def minta_use_tool_once(self, **kw):
        from arti_stardew_command import validate_use_tool_args, HasilPerintah, SELESAI
        args={k:kw[k] for k in ("slot","qualified_item_id","tile_x","tile_y","facing","ticks")}
        validate_use_tool_args(args)
        request_id="sim-use-"+str(self.seq)
        def effect():
            if self.selected_slot != 0 or args["qualified_item_id"] != "(T)Axe" or (3-self.tile[0],9-self.tile[1]) != ((0,-1),(1,0),(0,1),(-1,0))[self.facing]:
                raise ValueError("simulated_target_not_reached")
            if self.cycles >= 2: raise ValueError("simulated_target_removed")
            self.cycles += 1
            if self.cycles==2: self.drop_at=self._world_ticks+1
            self.last_use=dict(request_id=request_id,cycle_observed=True,input_completed=True,cleanup_confirmed=True)
        kw["guarded_publish"](effect)
        self.advance()
        return HasilPerintah(SELESAI,True,request_id,"InputCompleted",mentah=dict(
            session_id=self.raw["session_id"],runtime_instance_id=self.raw["runtime_instance_id"],
            player_id=-1,screen_id=0,context_generation=self.generation,location_id="Farm",op="input_once",
            primitive="use_tool",input_args=args,request_id=request_id,accepted=True,
            press_count=args["ticks"],cleanup_confirmed=True,world_effect_status="unverified"))

    def minta_input_once(self, direction, ticks, **kw):
        from arti_stardew_command import HasilPerintah, SELESAI
        dx,dy={"up":(0,-1),"right":(1,0),"down":(0,1),"left":(-1,0)}[direction]
        def effect():
            if ticks>1:
                self.tile[0]+=dx; self.tile[1]+=dy
            self.facing={"up":0,"right":1,"down":2,"left":3}[direction]
        kw["guarded_publish"](effect)
        self.advance()
        return HasilPerintah(SELESAI,True,"sim-move-"+str(self.seq),"PressCompleted",
            mentah=dict(session_id=self.raw["session_id"],request_id="sim-move-"+str(self.seq),op="input_once",accepted=True,
                        direction=direction,press_count=ticks))

    def advance(self):
        with self._sample_lock:
            self._advance()

    def tick(self):
        """Explicit finite simulated world step, independent of reads."""
        with self._sample_lock:
            self._world_ticks += 1
            if self.drop_at is not None and self._world_ticks>self.drop_at and abs(self.tile[0]-3)+abs(self.tile[1]-9)<=1:
                self.count=5;self.drop_at=None
            if self.night and self.night[0].get("saving") is not None:
                # Synthetic save + host sync: the shipping menu closes only after this countdown.
                self.night[0]["saving"]-=1
                if self.night[0]["saving"]<=0:self._pop_night()
            elif self.night and (self.night[0]["type"]=="ShippingMenu" or self.night[0]["type"]=="LevelUpMenu"
                                 and not self.night[0].get("options")):
                # WP P1: N1's ProgressionClient (whitelist c/d) confirms these natively after its own dwell.
                top=self.night[0];top["native_ticks"]=top.get("native_ticks",0)+1
                if top["native_ticks"]>=self.native_progression_ticks:
                    if top["type"]=="ShippingMenu":top["saving"]=self.save_ticks;self.ui_revision+=1
                    else:self._pop_night()
            self.advance()  # public capture/fault seam; never bypass injected hooks

    def _advance(self):
        # Capture current facts only. Wire seq/captured_ticks remain actual
        # capture metadata; an idle capture does not apply a world effect.
        self.seq+=1
        self._publish_motor()
        inventory={"meta:inventory":dict(kind="meta",active_slot=self.selected_slot,slot_count=3),
            "slot:0":dict(kind="slot",slot=0,occupied=True,is_active=self.selected_slot==0,qualified_item_id="(T)Axe",type_token="tool",stack=1,quality=0),
            "slot:1":dict(kind="slot",slot=1,occupied=self.count>0,is_active=False,qualified_item_id="(O)388",stack=self.count,quality=0),
            "slot:2":dict(kind="slot",slot=2,occupied=True,is_active=self.selected_slot==2,qualified_item_id="(T)Hoe",type_token="tool",stack=1,quality=0)}
        targets={"ter:Farm:3:9:Tree":dict(kind="terrain",terrain_type="Tree",tile=dict(x=3,y=9),
                 wild_tree=dict(tree_type="1",growth_stage=5,tapped=False,stump=False))} if self.cycles<2 else {}
        window=dict(kind="gather_window",available=True,reason=None,location_id="Farm",captured_ticks=self.seq,
            center=dict(x=self.tile[0],y=self.tile[1]),radius=1,targets=targets,
            drops=([dict(id="sim-drop-1",qualified_item_id="(O)388",quality=0,pixel=dict(x=192.,y=576.))] if self.drop_at else []),
            drops_complete=True,using_tool=False,last_use=self.last_use)
        context=dict(runtime_instance_id=self.raw["runtime_instance_id"],context_generation=self.generation,
            session_id=self.raw["session_id"],player_id=-1,screen_id=0,ui_revision=self.ui_revision,viewport_transform_revision=1,
            location_id="Farm",mode="gameplay",is_player_free=True,can_player_move=True,
            capabilities={"world_key":dict(available=True,primitives=["use_tool","move_cardinal"])})
        context["toolbar"] = dict(active_slot=self.selected_slot,targets=[
            dict(receiver_token="toolbar",target_token="tool-"+str(slot),slot=slot,qualified_item_id=qid,stack=1,quality=0)
            for slot,qid in ((0,"(T)Axe"),(2,"(T)Hoe"))])
        context["capabilities"]["select_item"]={"available":self.selection_supported}
        scene={"gather:nearby":window,"loc:Farm":dict(kind="location",location_id="Farm")}
        if self.with_container:
            ref="obj:Farm:4:8:(BC)130"
            scene[ref]=dict(kind="object",tile=dict(x=4,y=8),qualified_item_id="(BC)130")
            context["capabilities"].update(world_interact={"available":True},container_transfer={"available":True,"single_item":True},container_exit={"available":True})
            if (tuple((4-self.tile[0],8-self.tile[1])) == ((0,-1),(1,0),(0,1),(-1,0))[self.facing] and not self.menu):
                context["world_interaction_target"]=dict(target_token="placed-chest-target",source_fact_id=ref,
                    tile_x=4,tile_y=8,actor_tile_x=self.tile[0],actor_tile_y=self.tile[1],facing=self.facing,kind="chest")
            if self.menu:
                def slot(n,count,side):
                    return dict(slot=n,side=side,bounds=[n*64,0,64,64],target_token=f"{self.menu}:{side}:{n}:{count}",
                        item=dict(qualified_item_id="(O)388",quality=0,stack=count,max_stack=999) if count else None)
                context.update(mode="menu",is_player_free=False,can_player_move=False,
                    interactive={"receiver_token":self.menu},container=dict(available=True,receiver_token=self.menu,
                        container_token="placed-chest",source_fact_id=ref,readback_token=self._readback_token(),
                        close_target=dict(kind="ordinary_chest_close",bounds=[900,100,64,64],viewport=[1280,720],target_token="close:"+self._readback_token()),
                        cursor=None,ready_to_close=True,actor_slots=[slot(0,self.count,"actor"),slot(1,0,"actor")],
                        container_slots=[slot(0,self.chest_count,"container"),slot(1,0,"container")]))
        if self.night_staged:
            context["day_transition"]=dict(showing_end_of_night_stuff=bool(self.night),end_of_night_menus_pending=max(0,len(self.night)-1))
        if self.night:
            context.update(mode="menu",is_player_free=False,can_player_move=False,
                interactive={"receiver_token":self.night[0]["receiver"],"menu_type":self.night[0]["type"]},ui_key=self._night_block())
            context["capabilities"]["ui_key"]={"implemented":True,"available":context["ui_key"]["ready"],
                "reason":context["ui_key"].get("not_ready_reason") or "ready"}
        for domain,facts in (("ui.context",context),("progress.inventory",inventory),("scene.current_location",scene)):
            self.seq += 1
            rec=self.observations.get(-1,domain)
            revision=(rec.domain_revision + int(dict(rec.facts)!=facts) if rec else 1)
            self.observations.apply(dict(ev="observation",seq=self.seq,player_id=-1,screen_id=0,
                payload=dict(session_id="simulation-observation",product=domain.split(".")[0],domain=domain,
                kind="snapshot",domain_revision=revision,provenance="LOCAL_AUTHORITATIVE",authoritative=True,
                available=True,error=None,truncation=dict(truncated=False,observed_count=len(facts),sent_count=len(facts)),
                body=dict(facts=facts))))

    def _publish_motor(self):
        self.state.catat(dict(ev="state",seq=self.seq,player_id=-1,screen_id=0,payload=dict(
            location="Farm",tile=dict(x=self.tile[0],y=self.tile[1]),pixel=dict(x=self.tile[0]*64.,y=self.tile[1]*64.),
            facing=self.facing,time_of_day=610,date=dict(self.date),energy=270.,health=100,
            is_player_free=not bool(self.menu or self.night),can_player_move=not bool(self.menu or self.night),
            active_menu=dict(type=self.night[0]["type"]) if self.night else dict(type="ItemGrabMenu") if self.menu else None,
            ui_revision=self.ui_revision)))

    def start_sensor(self, runtime_lock):
        """Finite fake-world clock + genuine motor/rich capture, no decisions.

        Capture refreshes identical snapshots at their semantic revision; changed
        facts (including gather capture ticks) remain changed, not stale grants.
        """
        if self._sensor_thread is not None:
            raise ValueError("simulation_sensor_already_running")
        # Provider/approval preparation may outlive the 500ms motor bound.
        # Starting the declared observer publishes current facts immediately;
        # this is capture only, never a simulated physics tick or input grant.
        with runtime_lock, self._sample_lock:
            self.advance()
        def sample():
            while not self._sensor_stop.wait(0.1):
                with runtime_lock, self._sample_lock:
                    self.tick()
        self._sensor_thread = threading.Thread(target=sample, name="trial-simulated-sensor", daemon=True)
        self._sensor_thread.start()

    def stop_sensor(self):
        self._sensor_stop.set()
        if self._sensor_thread is not None:
            self._sensor_thread.join(timeout=1)
        self._sensor_thread = None

    def stage_night(self, menus, *, save_ticks=3, fault=None):
        """Explicit synthetic end-of-night fixture: top-first menu stack over a new date (no game physics)."""
        with self._sample_lock:
            self.night=[dict(m) for m in menus];self.night_staged=True;self.save_ticks=save_ticks
            self.ui_key_fault=fault;self.raw["ui_key_enabled"]=True
            self.date=dict(self.date,day=self.date["day"]+1)
            if self.night:self._new_night_receiver()
            self.advance()

    def _new_night_receiver(self):
        self.night_seq+=1;self.night[0]["receiver"]=f"rcv:sim{self.night_seq}"
        self.generation+=1;self.ui_revision+=1

    def _pop_night(self):
        self.night.pop(0)
        if self.night:self._new_night_receiver()
        else:self.generation+=1;self.ui_revision+=1

    def _night_block(self):
        import hashlib
        top=self.night[0];menu=top["type"];receiver=top["receiver"]
        kind={"LevelUpMenu":"level_up_profession" if top.get("options") else "level_up",
              "ShippingMenu":"shipping_summary","DialogueBox":"dialogue","LetterViewerMenu":"letter"}.get(menu)
        ready=kind is not None and top.get("saving") is None
        block=dict(receiver_token=receiver,menu_type=menu,ready=ready,wait_only=kind is None)
        if kind is None:block["not_ready_reason"]="wait_only"
        elif not ready:block["not_ready_reason"]="saving"
        if kind is not None:
            from arti_stardew_command import UI_KEY_TABLE
            block["kind"]=kind
            block["keys"]=[dict(key=k,target_token="uk:"+hashlib.sha256(f"{receiver}|{menu}|{kind}|{k}|{top.get('page',0)}".encode()).hexdigest()[:32])
                           for k in UI_KEY_TABLE[kind][1]]
        if menu=="LevelUpMenu":
            block.update(skill=top.get("skill",2),level=top.get("level",1))
            if top.get("options"):
                block["professions"]=[dict(index=i,profession_id=p,name=f"p{p}") for i,p in enumerate(top["options"])]
        return block

    def minta_ui_key(self, **kw):
        from arti_stardew_command import validate_ui_key_args, HasilPerintah, SELESAI, TIDAK_PASTI
        args={k:kw[k] for k in ("receiver_token","menu_type","kind","key","target_token","option_index","profession_id") if k in kw}
        validate_ui_key_args(args)
        block=self.observations.get(-1,"ui.context").facts.get("ui_key") or {}
        token=next((k["target_token"] for k in block.get("keys",[]) if k["key"]==args["key"]),None)
        if (not self.night or args["receiver_token"]!=block.get("receiver_token") or token!=args["target_token"]
                or block.get("ready") is not True):
            raise ValueError("simulated_ui_key_changed")
        old=self.generation;request_id=f"sim-ui_key-{self.seq}";top=self.night[0];before=top["receiver"]
        def effect():
            self.ui_key_calls.append(dict(args))
            if self.ui_key_fault=="uncertain":return
            if top["type"]=="ShippingMenu":
                top["saving"]=self.save_ticks;self.ui_revision+=1
            else:
                if args["key"]=="choose":self.professions.add(args["profession_id"])
                self._pop_night()
        kw["guarded_publish"](effect)
        self.advance()
        receipt=dict(session_id=self.raw["session_id"],runtime_instance_id=self.raw["runtime_instance_id"],
            player_id=-1,screen_id=0,context_generation=old,location_id="Farm",op="input_once",primitive="ui_key",
            request_id=request_id,accepted=True,input_args=args,press_count=1,cleanup_confirmed=True)
        if self.ui_key_fault=="uncertain":
            return HasilPerintah(TIDAK_PASTI,False,request_id,"UiKeyUncertain",alasan="simulated_uncertain",
                                 mentah=dict(receipt,status="uncertain",outcome="UiKeyUncertain"))
        after=self.night[0]["receiver"] if self.night else None
        receipt.update(status="completed",outcome="UiKeyApplied",success=True,input_settled=True,
            receiver_transition_status="NoReceiverTransition" if after==before else "ExpectedReceiverTransition",
            ui_key_effect="shipping_confirmed" if top["type"]=="ShippingMenu" else
                "profession_chosen" if args["key"]=="choose" else "level_up_confirmed" if args["kind"]=="level_up" else "closed",
            result_receiver_token=after,result_context_generation=self.generation,world_effect_status="observed_ui_key")
        return HasilPerintah(SELESAI,True,request_id,"UiKeyApplied",mentah=receipt)

    def _readback_token(self):
        return f"{self.menu}:{self.count}:{self.chest_count}"

    def _finite(self,primitive,args,kw,effect,receiver=False):
        from arti_stardew_command import HasilPerintah,SELESAI
        old=self.generation; request_id=f"sim-{primitive}-{self.seq}"
        kw["guarded_publish"](effect)
        self.advance()
        receipt=dict(session_id=self.raw["session_id"],runtime_instance_id=self.raw["runtime_instance_id"],
            player_id=-1,screen_id=0,context_generation=old,location_id="Farm",op="input_once",primitive=primitive,
            request_id=request_id,accepted=True,input_args=args,press_count=1,cleanup_confirmed=True)
        if receiver:receipt.update(input_settled=True,receiver_transition_status="ExpectedReceiverTransition",
            result_receiver_token=self.menu,result_context_generation=self.generation,result_readback_token=self._readback_token())
        return HasilPerintah(SELESAI,True,request_id,{"select_item":"SelectionObserved","world_interact":"ContainerOpened","container_transfer":"TransferObserved","container_exit":"MenuExited"}[primitive],mentah=receipt)

    def minta_select_item(self,**kw):
        from arti_stardew_command import validate_select_item_args
        args={k:kw[k] for k in ("receiver_token","target_token","slot","qualified_item_id","stack","quality")}
        validate_select_item_args(args)
        targets=self.observations.get(-1,"ui.context").facts["toolbar"]["targets"]
        if args not in targets or self.menu:raise ValueError("simulated_selection_changed")
        return self._finite("select_item",args,kw,lambda:setattr(self,"selected_slot",args["slot"]))

    def minta_world_interaction(self,**kw):
        from arti_stardew_command import validate_world_interaction_args
        args={k:kw[k] for k in ("target_token","source_fact_id","tile_x","tile_y","actor_tile_x","actor_tile_y","facing","kind")}
        validate_world_interaction_args(args)
        if self.menu or args!=self.observations.get(-1,"ui.context").facts.get("world_interaction_target"):
            raise ValueError("simulated_receiver_changed")
        def effect():self.menu="r0";self.generation+=1;self.ui_revision+=1
        return self._finite("world_interact",args,kw,effect,True)

    def minta_container_transfer(self,**kw):
        from arti_stardew_command import validate_container_transfer_args
        args={k:kw[k] for k in ("receiver_token","container_token","readback_token","source_fact_id","target_token", "slot","qualified_item_id","stack","quality","owner_ceiling")}
        if "quantity" in kw:args["quantity"]=kw["quantity"]
        validate_container_transfer_args(args)
        amount=args.get("quantity",args["stack"])
        if (not self.menu or args["readback_token"]!=self._readback_token()
                or args["stack"]!=self.count or amount>args["owner_ceiling"]):
            raise ValueError("simulated_transfer_changed")
        def effect():
            self.count-=amount;self.chest_count+=amount
            self.generation+=1;self.ui_revision+=1;self.menu="r"+str(self.generation)
        return self._finite("container_transfer",args,kw,effect,True)

    def minta_container_exit(self,**kw):
        from arti_stardew_command import validate_container_exit_args
        args={k:kw[k] for k in ("receiver_token","container_token","source_fact_id","readback_token","target_token")}
        validate_container_exit_args(args)
        view=self.observations.get(-1,"ui.context").facts.get("container",{})
        if (not self.menu or any(args[k]!=view.get(k) for k in args if k!="target_token")
                or args["target_token"]!=view.get("close_target",{}).get("target_token")):
            raise ValueError("simulated_exit_changed")
        def effect():self.menu=None;self.generation+=1;self.ui_revision+=1
        return self._finite("container_exit",args,kw,effect,True)

    def wait_next(self, pid, revision, timeout):
        # Compatibility helper; waiting cannot sample or advance the world.
        return self.state.wait_next(pid, revision, timeout)

    def __getattr__(self, name):
        # Hard separation: no fallback to live transport for unsupported families.
        if name.startswith("minta_"):
            def unavailable(*args, **kwargs): raise ValueError("simulated_family_not_implemented")
            return unavailable
        raise AttributeError(name)


def sanitized(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        # Private exception receipts intentionally retain message + stack, including
        # filenames. Applying the ordinary path scrub to a whole traceback erased
        # the only diagnostic for startup failures. No locals are captured.
        if {"phase", "error", "message", "traceback"} <= value.keys():
            return dict(value)
        return {k: sanitized(v) for k, v in value.items()
                if k not in {"player_name", "player_id", "speech", "owner_text", "goal_text", "clue", "journal_path", "path"}
                and not re.search(r"secret|credential|api.?key|authorization|access.?token|password",k,re.I)}
    if isinstance(value, (list, tuple)):
        return [sanitized(v) for v in value]
    if isinstance(value,str) and (re.search(r"[A-Za-z]:[\\/]|/(?:home|Users)/",value)):
        return "<private-path-omitted>"
    return value


def recoverable_gather_replan(runtime, evidence) -> bool:
    """Unpublished clean gather/ship replans use the runtime's bounded retry/rotation.

    Keep the historical helper name; gather_only excludes unrelated extras goals, not the ship lane (FIXB36).
    """
    return bool(evidence is not None and evidence.lifecycle == "not_taken" and evidence.request_id is None
                and evidence.reason == "approach_replan_required" and evidence.cleanup_confirmed
                and runtime._approach_replan_eligible(runtime.active_goal, gather_only=True))


def skill_sequence_status(evidence):
    """Keep a verified no-work eat terminal distinct from failure and consumption."""
    if evidence is not None and evidence.status == "achieved" and evidence.reason in (
            "skill_sequence_verified", "part_a_verified"):
        # A31: the Part A campaign finishes achieved only when every native readback row passed
        # (arti_stardew_part_a gaps empty); the wrapper had mislabelled it HELD.
        return "OBJECTIVE_VERIFIED"
    if evidence is not None and evidence.status == "not_applicable" and evidence.reason == "already_full":
        return "NOT_APPLICABLE"
    if evidence is not None:
        # Live7 rehearsal 1: a verified bed + next morning was reported HELD and the cycle supervisor
        # (OBJECTIVE_VERIFIED only) safety-stopped. The shared live-day contract names the terminal.
        from arti_stardew_live7 import trial_status
        live = trial_status(evidence.status, evidence.reason)
        if live is not None:
            return live
    return "HELD"


def completed_progress(result, reason, goal_active: bool, unresolved: bool) -> bool:
    """D20 (live B3W6c): a settled act that moved the actor but missed its own outcome (a move_through
    that walked one of its two tiles: applied=false, reason=outcome=PressCompleted, observed change,
    cleanup confirmed, a continuation scheduled) is progress, not a HOLD. The runner keeps observing;
    when nothing follows, its idle check still ends the trial HELD continuation_not_ready."""
    evidence = getattr(result, "action_evidence", None)
    return bool(goal_active and not unresolved and not result.applied and not result.discarded
                and (str(reason).startswith("continuation") or reason == "owner_turn")
                and evidence is not None and evidence.lifecycle == "completed"
                and evidence.cleanup_confirmed is True and evidence.request_id is not None
                and evidence.world_effect_status == "observed_change"
                and str(evidence.reason or "").startswith("outcome="))


# D-B3-4 (live V2B3G2buy-a/-b): a settled menu step that changed nothing (ui_press LT with nothing snapped, ui_nav at the
# list edge) is a no-op the runtime already re-decides from fresh facts, not a reason to end the trial. Bounded per trial.
MENU_NOOP_OUTCOMES = frozenset({"UiPressNoEffect", "UiNavNoEffect"})
MENU_NOOP_BUDGET = 6


def menu_noop(result, reason, goal_active: bool) -> bool:
    """A verified-clean menu no-op: completed, cleanup confirmed, published (request id), outcome NoEffect, nothing
    applied and nothing discarded, during an owner turn or a continuation of a live goal. Refusals (rejected /
    not_taken), uncertain lifecycles and anything with an unconfirmed cleanup are NOT no-ops (the runner holds)."""
    evidence = getattr(result, "action_evidence", None)
    return bool(goal_active and not result.applied and not result.discarded
                and (str(reason).startswith("continuation") or reason == "owner_turn")
                and evidence is not None and evidence.lifecycle == "completed" and evidence.cleanup_confirmed is True
                and evidence.request_id is not None and evidence.outcome in MENU_NOOP_OUTCOMES
                and evidence.world_effect_status in ("unchanged", "zero_effect"))


def menu_noop_admit(report: dict, result, reason, goal_active: bool) -> tuple[bool, dict | None]:
    """Count one settled menu no-op against the trial budget: (recoverable, journal note | None). The step past the
    budget is not recoverable (the runner HOLDs as before) and is journaled once as menu_noop_budget_exhausted."""
    if not menu_noop(result, reason, goal_active):
        return False, None
    report["menu_noops"] = report.get("menu_noops", 0) + 1
    if report["menu_noops"] <= MENU_NOOP_BUDGET:
        return True, None
    note = {"ev": "menu_noop_budget_exhausted", "count": report["menu_noops"], "budget": MENU_NOOP_BUDGET,
            "outcome": result.action_evidence.outcome}
    report["menu_noop_budget_exhausted"] = note
    return False, note


RUNNER_TIMEOUT_S = 700.0      # prep_trial's subprocess timeout
OVERNIGHT_MARGIN_S = 45.0     # cleanup + report after the wait


def overnight_budget(wait_s: float, *, grant_remaining_s: float | None, elapsed_s: float) -> float:
    """FIXB17 review: the overnight wait never outlives the grant or the runner's 700 s subprocess timeout."""
    caps = [wait_s, RUNNER_TIMEOUT_S - elapsed_s - OVERNIGHT_MARGIN_S]
    if grant_remaining_s is not None:
        caps.append(grant_remaining_s)
    return max(0.0, min(caps))


OVERNIGHT_DAY_CHANGED_GRACE_S = 20.0  # FIXB31 D-SOAK-11: a new day, no menu, ARTI free for this long ends the wait
_SEASON_ORDER = ("spring", "summer", "fall", "winter")


def _date_key(date) -> tuple | None:
    """(year, season, day) -> a sortable key, or None."""
    try:
        year, season, day = date
        return (int(year), _SEASON_ORDER.index(season), int(day))
    except (TypeError, ValueError):
        return None


def overnight_pose(runtime) -> dict | None:
    """FIXB31 D-SOAK-11: the bound farmhand's date / menu / free state from the runtime's own motor store (read-only), plus
    the goal's date basis (the day the goal ran on); None when unreadable."""
    try:
        _, pid = runtime._binding_view()
        motor = runtime._state.latest(pid) if pid is not None else None
        if motor is None:
            return None
        goal = runtime.active_goal
        return {"day": _date_key(runtime._date_tuple(motor)), "menu": motor.active_menu,
                "free": motor.is_player_free is True,
                "basis": _date_key(getattr(goal, "date_basis", None)) if goal is not None else None}
    except Exception:  # noqa: BLE001 - a probe never raises
        return None


def overnight_wait(runtime, *, wait_s: float = 300.0, poll_s: float = 0.25, clock=time.monotonic, sleep=time.sleep,
                   stop_event=None, pose=None, day_changed_grace_s: float = OVERNIGHT_DAY_CHANGED_GRACE_S) -> dict:
    """FIXB17 (MS11 / MS13 / D1: the day-start rule never ran live inside a trial): after the goal ended, keep the runtime
    (and its worker's day-start rule) alive until a day-start episode was seen and cleared, or ``wait_s`` passes. The
    rule journals its own steps (day_start_detected / day_start_action / day_start_cleared / day_start_blocked).
    FIXB31 D-SOAK-11 (V2SOAK1A3 t1: the bed goal slept, the morning came with no farmhand menu, and the wait ran its full
    300 s, ~5 real minutes idle per night): a farmhand ShippingMenu / LevelUp ends it through the day-start path as before
    (seen -> cleared); with no day-start menu, the day changed (vs the goal's date basis, else the first read) and ARTI
    free with no menu for ``day_changed_grace_s`` also ends it (status ``day_changed``)."""
    probe = pose if pose is not None else (lambda: overnight_pose(runtime))
    started, seen, ended = clock(), False, "deadline"
    day0, changed_since = None, None
    while clock() - started < wait_s:
        if stop_event is not None and stop_event.is_set():
            ended = "stopped"  # FIXB17 review: STOP ends the wait at once
            break
        active = bool(getattr(runtime, "_day_start_active", False)) or runtime.day_start_pending()
        if active:
            seen = True
        elif seen:
            ended = "cleared"
            break
        else:
            p = probe() or {}
            day = p.get("day")
            if day0 is None:
                day0 = p.get("basis") or day
            if day is not None and day0 is not None and day > day0 and not p.get("menu") and p.get("free"):
                changed_since = clock() if changed_since is None else changed_since
                if clock() - changed_since >= day_changed_grace_s:
                    ended = "day_changed"
                    break
            else:
                changed_since = None  # a menu / not free / same day restarts the grace
        sleep(poll_s)
    state = runtime._day_start_state
    status = ("stopped" if ended == "stopped" else "blocked" if state.blocked
              else "done" if ended == "cleared" and state.finished else "deadline" if ended == "deadline"
              else "day_changed" if ended == "day_changed"
              else "active" if state.active else "none")
    return {"seen": seen, "waited_s": round(clock() - started, 1), "wait_cap_s": round(wait_s, 1), "actions": state.actions,
            "status": status, "reason": state.blocked or state.finished, "menus": list(state.menus)}


def refusal_text(needed: int, approved: int, objective: str = "combat") -> str:
    if objective == "animal routine":
        return (f"envelope_exceeds_approval: the animal routine needs {needed} inputs > approved {approved}; "
                "obtain a new owner/CT-issued pinned animal approval; --inputs/--inputs-ok cannot raise it")
    """FIXB23 review: the combat envelope refusal names the exact rerun commands."""
    return (f"envelope_exceeds_approval: the {objective} objective needs {needed} inputs > approved {approved}; rerun with "
            f'M1_RUN_ARGS="--inputs-ok" bash scripts/m1_operator/run_manual.sh ... '
            f"(or: python scripts/m1_operator/prep_trial.py run --inputs-ok; --inputs {needed} for an explicit number)")


def _objective_input_envelope(grant, budget, runtime, report, *, inputs_ok=False, inputs=None):
    """FIXB23 D-B20-2 (live V2B20ACC11b: the combat + exit trial hit the approved 48 inputs at the exit leg): a combat goal
    sizes its trial input envelope like a skill plan (arti_stardew_combat.combat_input_envelope, kill 1 = 134). Above the
    approved input the trial refuses with the needed number unless the operator opts in (--inputs-ok: the computed
    envelope) or sets it explicitly (--inputs N, 1..COMBAT_INPUT_ENVELOPE_MAX, any trial). Journaled
    (`trial_input_envelope`); the approval file itself is never edited."""
    import arti_stardew_combat as _combat_mod
    if live7_approval(grant):
        if inputs is not None or inputs_ok:
            raise ValueError("live7_has_no_numeric_caps")
        return grant
    approved = grant["limits"]["input"]
    if getattr(runtime, "_skills_enabled", False):
        # FIXB23 review: a --skills trial keeps its own envelope (--skills-envelope-ok); --inputs never overrides it.
        if inputs is not None or inputs_ok:
            budget._append({"ev": "trial_input_envelope_ignored", "reason": "skills_envelope",
                            "inputs": inputs, "inputs_ok": bool(inputs_ok), "input": approved})
            report["trial_input_envelope_ignored"] = "skills_envelope"
        return grant
    needed = _combat_mod.combat_input_envelope(grant.get("owner_text") or "")
    from arti_stardew_animal_routine import animal_input_envelope
    animal_envelope = animal_input_envelope(grant.get("owner_text") or "")
    if needed is None:
        needed = animal_envelope
    if inputs is not None:
        if type(inputs) is not int or not 1 <= inputs <= input_approval_max(grant.get("owner_text")):
            raise ValueError("inputs_out_of_range")
        chosen, source = inputs, "operator_inputs"
    elif needed is not None and needed > approved:
        if not inputs_ok:
            report["input_envelope_needed"] = needed
            print(refusal_text(needed, approved, "animal routine" if animal_envelope is not None else "combat"), file=sys.stderr)
            raise ValueError("envelope_exceeds_approval")
        chosen, source = needed, "inputs_ok"
    else:
        return grant
    if animal_envelope is not None and chosen > approved:
        report["input_envelope_needed"] = chosen
        raise ValueError("animal_envelope_requires_pinned_approval")
    if chosen == approved:
        return grant
    grant = dict(grant, limits=dict(grant["limits"], input=chosen))
    budget.grant = grant
    runtime._action_session_limit = chosen  # the runtime's own cap was sized from the approved input
    budget._append({"ev": "trial_input_envelope", "input": chosen, "approved": approved, "needed": needed,
                    "source": source, "max": input_approval_max(grant.get("owner_text"))})
    report["trial_input_envelope"] = {"input": chosen, "approved": approved, "needed": needed, "source": source}
    return grant


def _animal_approval_guard(runtime, grant, budget, session):
    """Runtime-private capability, installed ONLY after run_trial.validate succeeds.

    Freeze the approved scope/limit before optional sizing; generic config/session
    integers and --inputs cannot mint this capability. Bind its first original
    owner goal and native epoch, then refuse reuse by replacement goals/sessions.
    """
    if live7_approval(grant):
        return lambda goal: None  # Live7 uses its native skill gates, never an ordinary S3 cap grant.
    from arti_stardew_animal_routine import animal_input_envelope
    text = re.sub(r"^(?:arti[\s,:]+)?(?:main mandiri|main sendiri|tujuan baru)(?:\s*[:,-]\s*|\s+)",
                  "", grant["owner_text"].strip(), flags=re.I)
    cap, expires = grant["limits"]["input"], grant["expires_at"]
    campaign = part_a_approval(grant)
    supported = campaign or animal_input_envelope(grant["owner_text"]) is not None
    if campaign:
        cap = 280  # existing S3 ceiling, never 900 per animal routine
    explicit = isinstance(grant.get("input_decision"), str) and bool(grant["input_decision"].strip())
    bound = None
    def approved(goal):
        nonlocal bound
        epoch = getattr(goal, "play_identity", None)
        # A registered S3 skill expands the approved Indonesian goal to the
        # canonical animal objective. Bind only its actual child, never an alias
        # supplied by an unrelated replacement goal.
        from arti_stardew_skills import ANIMAL_GOAL, objective_text, SkillCall
        skill_session = getattr(runtime, "_skill_session", None)
        run = getattr(skill_session, "current", None)
        skill_child = (getattr(runtime, "_skills_enabled", False) and skill_session is not None
            and skill_session.status == "active" and run is not None and run.call.name == "rawat_hewan"
            and run.goal_id == getattr(goal, "goal_id", None)
            and text.rstrip(".").casefold() == ANIMAL_GOAL.rstrip(".").casefold()
            and skill_session.goal_text.rstrip(".").casefold() == text.rstrip(".").casefold()
            and goal is not None and goal.text == objective_text(SkillCall("rawat_hewan")))
        if campaign:
            skill_child = (getattr(runtime, "_skills_enabled", False) and skill_session is not None
                and skill_session.status == "active" and skill_session.mode == "part_a"
                and part_a_goal(skill_session.goal_text) and run is not None and run.call.name == "rawat_hewan"
                and run.goal_id == getattr(goal, "goal_id", None) and goal is not None
                and goal.text == objective_text(SkillCall("rawat_hewan")))
        if (not supported or type(cap) is not int or not 0 < cap <= 280
                or cap > INPUT_APPROVAL_DEFAULT and not explicit
                or budget.stopped or budget.clock() >= expires or runtime._trial_admission != budget.admit
                or goal is None or not (goal.text.strip().casefold() == text.casefold() or skill_child)
                or not epoch or epoch[1] != session):
            return None
        if bound is None:
            bound = (skill_session.session_id, epoch) if campaign else (goal, epoch)
        if campaign:
            return cap if skill_child and bound == (skill_session.session_id, epoch) else None
        return cap if bound[0] is goal and bound[1] == epoch else None
    return approved


LIVE7_DAY_START_CLEARED = "LIVE7_DAY_START_CLEARED"  # a lost night's end-of-night menu cleared by the day-start rule
LIVE7_DAY_START_IDLE = "LIVE7_DAY_START_IDLE"  # the day-start-only pass found no end-of-night menu to clear
DAY_START_ONLY_SETTLE_S = 10.0  # a fresh runtime may need a moment before ui.context shows the night menu


def live7_native_night(report, runtime, *, day_start_only=False):
    """Live7 risk #1/#3: the native night owns the farmhand, so no cleanup presses and no free/menu gate.

    Only (a) this run's own verified 02:00 pass-out terminal (``_live7_guard_day`` read a native time
    >= 2600 on the campaign date, or the next date) re-read here from the latest native motor read, or
    (b) a day-start-only pass on a lost night. Returns the readback dict, else None (ordinary gate).
    The re-read may be stale (the night fade can pause motor publication); a stale read still proves the
    clock reached 02:00, so it is accepted and recorded as ``fresh: false`` (review F5), never upgraded.
    """
    from arti_stardew_live7 import DAY_LOST, PASSED_OUT_REASON
    if day_start_only:
        return dict(basis="live7_day_start_pass", status=report.get("status"))
    if report.get("status") != DAY_LOST or report.get("reason") != PASSED_OUT_REASON:
        return None
    session = getattr(runtime, "_skill_session", None)
    campaign = getattr(session, "campaign", None)
    if getattr(session, "reason", None) != PASSED_OUT_REASON or getattr(campaign, "date", None) is None:
        return None
    from arti_stardew_part_a import next_date
    start = tuple(campaign.date)
    _, pid = runtime._binding_view()
    motor = runtime._state.latest(pid) if pid is not None else None
    date = runtime._date_tuple(motor) if motor is not None else None
    now = getattr(motor, "time_of_day", None)
    if date is None or type(now) is not int:
        return None
    if date == start and now >= 2600:
        basis = "native_passout_time"
    elif date == next_date(start):
        basis = "native_rollover"
    else:
        return None
    try:
        fresh = not runtime._state.is_stale(pid)
    except Exception:
        fresh = None
    return dict(basis=basis, date=list(date), time_of_day=now, campaign_date=list(start), fresh=fresh)


# Review F2: at 02:00 the guard's own _cancel_action releases asynchronously during the pass-out fade, so one
# input-released read may land mid-release (session.json mid-rename, a pad tick outstanding). Re-read only.
NIGHT_RELEASE_REREAD_S = 5.0
NIGHT_RELEASE_STEP_S = 0.25


def night_input_released(runtime, *, sleep=time.sleep, clock=time.monotonic):
    """Bounded read-only re-poll of skill_post_row_state for input release. No presses, no cancel.
    Returns (last post_row_state, reads). A state that never releases stays unreleased (HELD)."""
    post, reads = runtime.skill_post_row_state(), 1
    deadline = clock() + NIGHT_RELEASE_REREAD_S
    while not post["input_released"] and clock() < deadline:
        sleep(NIGHT_RELEASE_STEP_S)
        post, reads = runtime.skill_post_row_state(), reads + 1
    return post, reads


def live7_recovery_active(runtime, *, live7=False, day_start_only=False, reason=None):
    """The runtime, rather than a wrapper HOLD, owns a live recovery decision."""
    from arti_stardew_live7 import safety_reason
    return bool(live7 and not day_start_only and getattr(runtime, "_live7_active", lambda: False)()
                and runtime.status in ("READY", "PAUSED_RECOVERY")
                and not runtime._worker_stop.is_set() and not getattr(runtime, "_owner_play_stopped", False)
                and not safety_reason(reason) and reason not in ("owner_stop", "owner_play_stopped", "worker_stopped"))


def skill_terminal_gate(report, runtime, *, live7=False, day_start_only=False, sleep=time.sleep,
                        clock=time.monotonic):
    """Post-run skill cleanup + HELD gate. A real non-free state / open menu after published input still
    holds; only a verified native night (live7_native_night) is judged by input release alone, re-read
    read-only for at most NIGHT_RELEASE_REREAD_S before it holds."""
    night = live7_native_night(report, runtime, day_start_only=day_start_only) if live7 else None
    if night is not None:
        post, reads = night_input_released(runtime, sleep=sleep, clock=clock)  # read-only; no presses at night
        report["post_row_state"] = post
        report["native_night"] = dict(night, release_reads=reads)
        if not post["input_released"]:
            report["terminal_outcome"] = {"status": report["status"], "reason": report.get("reason")}
            report.update(status="HELD", reason="terminal_cleanup_unconfirmed")
        return report
    report["post_row_state"] = runtime.skill_terminal_cleanup()
    post = report["post_row_state"]
    if (runtime.active_goal is not None or getattr(runtime, "_physical_action_publications", 0) > 0) and (
            post["menu"] is not None or post["free"] is not True or not post["input_released"]):
        report["terminal_outcome"] = {"status": report["status"], "reason": report.get("reason")}
        report.update(status="HELD", reason="terminal_cleanup_unconfirmed")
    return report


REFLECTION_JOIN_S = 150.0  # fix54H: covers complete_reflection's 120 s timeout plus lane setup
REFLECTION_STOP_JOIN_S = 5.0


def stop_trial_runtime(runtime, *, live7=False):
    # STOP may beat the worker to the next boundary. Record the core's exact
    # SAFETY_STOP reason before generic shutdown ends/revokes the session.
    try:
        if live7:
            runtime.live7_stop_file_check()
    finally:
        runtime.stop()


def run_trial(*, mode="dry-run", grant=None, actual=None, profile=None, session=None,
              journal=None, factory=None, clock=time.time, stop_event=None,
              native_provenance="injected", skills_envelope_ok=False, overnight=False, overnight_wait_s=300.0,
              inputs_ok=False, inputs=None, day_start_only=False):
    from arti_stardew_diagnostics import exception_record
    run_started = time.monotonic()  # FIXB18: the runner subprocess starts the trial at once (its 700 s timeout clock)
    report = {"schema": "stardew_m1_trial.v1", "mode": mode, "identity": actual,
              "profile": profile, "status": "NOT_RUN", "steps": [], "limits": None,
              "provenance": {"model": "NOT_RUN", "native": "NOT_RUN", "mic": "NOT_RUN"},
              "host_paused": False, "live_verified": False}
    if mode == "dry-run":
        report["reason"] = "explicit_owner_approval_required"
        return report
    runtime = None
    live7 = False
    monitor_done = threading.Event()
    deadline_reached = threading.Event()
    monitor = None
    budget = None
    trial_provider = None
    said_lines = None
    results = queue.Queue()
    try:
        if day_start_only and not live7_approval(grant or {}):
            raise ValueError("day_start_only_requires_live7")  # before any journal/runtime effect
        validate(mode, grant, actual, profile, session, clock())
        # Preserve the approved ceiling before optional operator/skill sizing.
        # An animal envelope can never exceed the pinned approval itself.
        animal_approved = dict(grant, limits=dict(grant["limits"]))
        budget = TrialBudget(journal, grant, clock=clock)
        # Validate/configure the adapter before journal reservation; factory is
        # an explicit embedding dependency. CLI uses only the built-in factory.
        if factory is None:
            raise ValueError("runtime_adapter_missing")
        budget.begin()
        live7 = live7_approval(grant)
        if not live7 and (grant["limits"]["input"] > INPUT_APPROVAL_DEFAULT or grant.get("input_decision")):
            budget._append({"ev": "trial_approval_input", "input": grant["limits"]["input"],
                            "decision": grant.get("input_decision")})  # FIXB24 review
        runtime = factory(mode, budget.admit)
        if hasattr(runtime, "_source_tags"):
            runtime._source_tags = dict(budget.tags)  # Live7 WP4: the approval's tags label every runtime line
        if (campaign_approval(grant) or live7) and not getattr(runtime, "_skills_enabled", False):
            raise ValueError("part_a_requires_skills")
        if grant.get("input_decision"):
            # FIXB26 D-B23-2: the runtime's own action cap after the approval's decision (journaled with the approval)
            budget._append({"ev": "trial_session_cap", "input": grant["limits"]["input"],
                            "session_cap": getattr(runtime, "_action_session_limit", None)})
        if getattr(runtime, "_skills_enabled", False) and not live7:
            # D-B4-4 (live V2B4SKL2a: tebang 31 + pungut 12 + simpan > the approved 48 inputs): a --skills trial's input
            # envelope = max(approved input, sum of the planned skills' input caps) <= SKILLS_INPUT_ENVELOPE_MAX; every
            # skill keeps its own cap. Journaled; the operator never edits the approval for it.
            import arti_stardew_skills as _skills_mod
            envelope = _skills_mod.plan_input_envelope(grant.get("owner_text") or "")
            if envelope is not None and envelope > grant["limits"]["input"] and not skills_envelope_ok:
                # FIXB4 review (CT decision, owner confirmation pending): raising the approved input is opt-in.
                report["skills_envelope_needed"] = envelope
                print(f"envelope_exceeds_approval: the skill plan needs {envelope} inputs > approved "
                      f"{grant['limits']['input']}; rerun with --skills-envelope-ok", file=sys.stderr)
                raise ValueError("envelope_exceeds_approval")
            if envelope is not None and envelope > grant["limits"]["input"]:
                grant = dict(grant, limits=dict(grant["limits"], input=envelope))
                budget.grant = grant
                runtime._action_session_limit = envelope  # the runtime's own cap was sized from the approved input
                budget._append({"ev": "skills_input_envelope", "input": envelope,
                                "max": _skills_mod.SKILLS_INPUT_ENVELOPE_MAX})
                report["skills_input_envelope"] = envelope
        grant = _objective_input_envelope(grant, budget, runtime, report, inputs_ok=inputs_ok, inputs=inputs)
        from arti_stardew_runtime import ProductionDecisionProvider
        import arti_stardew_command as native_commands
        # Positive supported boundaries, not a marker which a transparent
        # native proxy can accidentally omit. Unknown/subclass adapters reject.
        simulated = type(runtime._commands) is SimulatedCommands
        native = runtime._commands is native_commands
        if not simulated and not native:
            raise ValueError("transport_provenance_unrecognized")
        if mode == "native-no-network" and isinstance(runtime.decision_provider, ProductionDecisionProvider):
            raise ValueError("native_mode_provider_forbidden")
        if mode != "model-no-game" and simulated:
            raise ValueError("live_fixture_mixing_forbidden")
        if mode != "native-no-network" and not isinstance(runtime.decision_provider, ProductionDecisionProvider):
            raise ValueError("production_provider_required")
        if mode == "model-no-game" and (runtime._start_listener or not simulated):
            raise ValueError("model_mode_native_forbidden")
        if not runtime._continuation_enabled or not runtime._start_worker:
            raise ValueError("autonomous_runtime_required")
        runtime._on_generation_result = lambda request, result: results.put((request.reason, result))
        runtime._trial_admission = budget.admit
        runtime._trial_deadline_unix_ms = budget.deadline_unix_ms
        # Read-only remaining approval; S15 reserves exit before trial_decision_limit.
        runtime._trial_budget_remaining = (lambda: dict.fromkeys(("decisions", "inputs", "seconds"), float("inf"))) if live7 else lambda: dict(
            decisions=budget.grant["limits"]["steps"]-budget.decisions,
            inputs=budget.grant["limits"]["input"]-budget.used["input"],
            seconds=budget.deadline_unix_ms/1000-budget.clock())
        # Apply the lower cap even to an embedded runtime; never raise an existing cap.
        limit = runtime._action_session_limit
        runtime._action_session_limit = None if live7 else (min(limit, grant["limits"]["input"]) if limit is not None else grant["limits"]["input"])
        runtime._animal_trial_approval = _animal_approval_guard(runtime, animal_approved, budget, session)
        if isinstance(runtime.decision_provider, ProductionDecisionProvider):
            runtime.decision_provider.physical_start_guard = budget.admit
            # validate() accepted the exact approved owner_text, identity, session and limits.
            # No later owner input enters this trial. This fact changes model guidance only.
            trial_provider = runtime.decision_provider
            trial_provider.trial_owner_authorized = True
            trial_provider.enable_trial_archive(runtime._journal_dir,
                enabled=runtime._config.get("stardew_trial_provider_archive_enabled", True) is True,
                config=runtime._config, tags=budget.tags)
            budget._append({"ev": "trial_owner_authorized", "source": "validated_approval_owner_text"})
        # Runtime sees its own trial reservation. No recovery is executed: start
        # before reservation would make a second listener possible on failure.
        if simulated:
            runtime._commands.start_sensor(runtime._lock)
        runtime.start()
        def supervise():
            while not monitor_done.wait(0.02):
                try: budget.check_time()
                except ValueError:
                    deadline_reached.set()
                    runtime.stop(); return
                if stop_event is not None and stop_event.is_set():
                    stop_trial_runtime(runtime, live7=live7); return
        monitor = threading.Thread(target=supervise, name="stardew-trial-stop", daemon=True)
        monitor.start()
        if live7 and runtime._config.get("rig_scripted_said_lines"):
            # ADR 229 part 5: the rig-only scripted said-lines instrument (free play; absent key = nothing).
            from scripts.free_play.said_lines import maybe_start
            said_lines = maybe_start(runtime, runtime._config)
        report["limits"] = grant["limits"]
        report["provenance"] = {"model": "deterministic" if mode == "native-no-network" else
                                getattr(runtime.decision_provider, "transport_provenance", "injected"),
                                "native": "simulated" if simulated else native_provenance, "mic": "NOT_RUN",
                                "owner_goal": "approved_initial_text", "later_owner_input": "NONE"}
        report.update(owner_directed_decisions=0, autonomous_decisions=0,
                      injected_mic_events=0, actual_mic_events=0, execution="existing_runtime_worker")
        # Native readiness: the mod connects to the receiver only after runtime.start(), so the first owner turn
        # must not be snapshotted before the farmhand identity and motor state exist (2026-10-06 attempt 2 held with
        # inspect mismatch "request_binding_changed", expected_player=None). Bounded wait, counted against the trial
        # clock; no provider or input reservation is spent while waiting. Simulated modes are bound at start.
        ready_wait = 0.0
        if not simulated:
            wait_started = clock()
            while clock() - wait_started < 45.0 and not (stop_event is not None and stop_event.is_set()):
                binding, pid = runtime._binding_view()
                if binding.status == "bound" and pid is not None and runtime._state.latest(pid) is not None:
                    break
                time.sleep(0.25)
            ready_wait = clock() - wait_started
            report["native_readiness_wait_seconds"] = round(ready_wait, 2)
            binding, pid = runtime._binding_view()
            if not (binding.status == "bound" and pid is not None and runtime._state.latest(pid) is not None):
                report.update(status="HELD", reason="native_binding_not_ready")
        # WS2 pre-phase: end-of-night screens (level-up, shipping, mail) are cleared by the runtime's
        # deterministic day-start rule before the owner turn; counted against the trial clock, no provider.
        stopped = lambda: stop_event is not None and stop_event.is_set()
        pending = report["status"] == "NOT_RUN" and not stopped() and runtime.day_start_pending()
        if day_start_only and report["status"] == "NOT_RUN" and not pending:
            settle_started = time.monotonic()  # bounded read-only settle for the first ui.context
            while not pending and not stopped() and time.monotonic() - settle_started < DAY_START_ONLY_SETTLE_S:
                time.sleep(0.25)
                pending = runtime.day_start_pending()
        if pending and not stopped():
            day_start = runtime.run_day_start_handler()
            report["day_start"] = day_start
            if day_start["status"] != "done":
                cause = "day_start_blocked:" + str(day_start["reason"])
                if live7 and not day_start_only:
                    report["day_start_recovery_pending"] = cause
                else:
                    report.update(status="HELD", reason=cause)
        if report["status"] != "NOT_RUN":
            pass
        elif stopped():
            report["status"] = "OWNER_STOPPED"
        elif day_start_only:
            # Live7 risk #3: a lost night's pass is the existing day-start rule only; never an owner turn/model.
            report.update(status=LIVE7_DAY_START_CLEARED if "day_start" in report else LIVE7_DAY_START_IDLE,
                          reason=report["day_start"].get("reason") if "day_start" in report else "day_start_not_pending")
        else:
            initial = runtime.decide_owner_turn(text=grant["owner_text"], trigger_type="ptt", viewer_name=None)
            if live7 and initial.admitted and hasattr(runtime, "live7_start_pending_reflection"):
                # fix54H: last night's reflection runs now, beside the day (its own provider lane), not before it.
                try:
                    runtime.live7_start_pending_reflection()
                except Exception as exc:  # noqa: BLE001 - a reflection never blocks the day
                    budget._append({"ev": "mind_reflection_failed", "code": type(exc).__name__, "deferred": True,
                                    "alert": True})
            if (initial.admitted and not stopped() and report.get("day_start_recovery_pending")
                    and live7_recovery_active(runtime, live7=live7, day_start_only=day_start_only)):
                runtime._live7_uncertain_ui(runtime._skill_session, report["day_start_recovery_pending"])
            if stop_event is not None and stop_event.is_set():
                report["status"] = "OWNER_STOPPED"
            elif not initial.admitted:
                report.update(status="HELD", reason=initial.reason or "initial_turn_not_admitted")
                budget._append({"ev": "trial_initial_turn_refused", "reason": report["reason"]})
            # This loop observes results. ONLY the existing runtime worker drives
            # subsequent decisions; no synthetic owner input or event is emitted.
            while report["status"] == "NOT_RUN":
                budget.check_time()
                if stop_event is not None and stop_event.is_set():
                    report["status"] = "OWNER_STOPPED"; break
                try:
                    reason, result = results.get(timeout=0.05)
                except queue.Empty:
                    with runtime._lock:
                        goal = runtime.active_goal
                        # WS2: a day-start handler waiting for the host save/sync is work, not an idle runner.
                        busy = (runtime._action_busy or runtime._inflight_generation is not None or runtime._results_pending
                                or bool(getattr(runtime, "_skill_emits", ()))
                                or getattr(runtime, "_skill_delivery_pending", 0)
                                or runtime.festival_lifecycle_due()
                                or runtime._day_start_active
                                # WS3b: asleep in the ReadyCheckDialog waiting for the host is work too.
                                or runtime._routine_holds_continuation()
                                # WS2/D33: a provable store finish the worker records next (no HOLD in the gap).
                                or runtime._store_rule_pending()
                                # WS1 D35: an observed tool effect awaits the worker's rule finish.
                                or runtime.tool_work_finish_due()
                                # Live D-B1c-1: the worker's menu_rule closes the open objective menu / finishes.
                                or runtime.auto_act_due()
                                # WS-G4: a verified catch awaits the worker's fishing_rule finish.
                                or runtime.fishing_finish_due()
                                # WS-G5: a verified kill / charge (or a heal hand-off) awaits the worker.
                                or runtime.combat_finish_due()
                                # D-B3-2: a verified economy effect awaits the worker's economy_rule (close + finish).
                                or getattr(runtime, "economy_finish_due", lambda: False)()
                                # FIXB26 D-B23-3: a take blocked bag_full awaits the worker's close + partial finish.
                                or getattr(runtime, "economy_take_blocked_due", lambda: False)()
                                # FIXB27 review: a failed sleep home leg awaits the worker's partial finish.
                                or getattr(runtime, "sleep_home_failed_due", lambda: False)()
                                # FIXB11 D-B8-3: a verified extras effect awaits the worker's extras_rule finish.
                                or getattr(runtime, "extras_finish_due", lambda: False)()
                                # FIXB18 D-B15a-2: a verified eat (or an already-full start) awaits routine_eat_rule.
                                or getattr(runtime, "routine_eat_finish_due", lambda: False)()
                                # FIXB22 D-B19-2: a not-taken post-finish store close awaits its bounded retry.
                                or getattr(runtime, "store_close_retry_due", lambda: False)()
                                # Skills tahap 1: an active skill session ends its own stalled skill and reports.
                                or getattr(runtime, "skill_boundary_due", lambda: False)())
                        if runtime._day_start_state.blocked is not None:
                            if live7_recovery_active(runtime, live7=live7, day_start_only=day_start_only):
                                runtime._live7_uncertain_ui(runtime._skill_session,
                                    "day_start_blocked:" + runtime._day_start_state.blocked)
                            else:
                                report.update(status="HELD", reason="day_start_blocked:" + runtime._day_start_state.blocked)
                                continue
                        waiting = goal and (goal.question_basis == "optional" or goal.no_progress_until is not None
                                            or runtime._owner_voice_busy)
                        pending = runtime._pending_continuation is not None or runtime._pending_event_reason is not None
                        if not busy and not waiting and not pending and results.empty():
                            report.update(status="HELD", reason=runtime.continuation_hold_reason())
                            report["continuation_hold"] = {
                                "runtime_status": runtime.status,
                                "budget_available": runtime._continuation_budget_available(),
                                "action_cancelled": runtime._action_cancel.is_set(),
                                "unresolved_input": runtime._gather_mutation_unresolved,
                                "last_generation_reason": runtime._logical_request.reason if runtime._logical_request else None,
                                "last_inspect_status": goal.last_inspect_status if goal else None,
                                "motor_stale": runtime._state.is_stale(goal.last_binding_player_id) if goal else None,
                                "owner_decisions": goal.owner_decisions if goal else None,
                                "autonomous_decisions": goal.continuation_decisions if goal else None,
                                "question_basis": goal.question_basis if goal else None,
                                "play_blocker": goal.play_blocker if goal else None,
                                "no_progress_blocked": goal.no_progress_blocked if goal else None,
                                "approach": goal.approach_progress if goal else None,
                            }
                    continue
                report["steps"].append(dict(sanitized(result), generation_reason=reason))
                report["owner_directed_decisions" if reason == "owner_turn" else "autonomous_decisions"] += 1
                if getattr(runtime, "_skills_enabled", False) and runtime.skill_report() is not None:
                    # Skills tahap 1: one owner goal = a skill sequence. A skill's own finish is a step, not the
                    # objective; only the session's verified sequence finish is OBJECTIVE_VERIFIED. Stalls inside a
                    # skill are ended by the runtime's skill boundary (never HOLD here while the session is active).
                    report["skills_mode"] = True
                    if result.reason == "trial_decision_limit":
                        report["status"] = "STEP_LIMIT"; break
                    if result.reason == "skill_sequence_finish":
                        report["status"] = skill_sequence_status(result.finish_evidence)
                        if report["status"] != "OBJECTIVE_VERIFIED":  # Live7 terminals keep their reason
                            report["reason"] = (result.finish_evidence.reason if result.finish_evidence is not None
                                                else "skill_sequence_unverified")
                        break
                    if result.reason == "owner_play_stopped":
                        report["status"] = "HELD"; break
                    continue
                # Runtime rule finishes (WS3b routine_sleep_finish, WS1 tool_work_finish) arrive here with
                # reason "finish" and count exactly like a model goal finish.
                # FIXB40 A8 run-3: the routine may honestly decline food at full vitals.
                # That terminal is not evidence that the trial's eat primitive ran.
                if result.finish_evidence and result.finish_evidence.reason in (
                        "original_objective_aborted_curfew", "already_full"):
                    report.update(status="HELD", reason=result.finish_evidence.reason)
                    break
                if result.finish_evidence and result.finish_evidence.reason == "observed_eat_effect":
                    from arti_stardew_interact_b import eat_consumption_observed
                    if not eat_consumption_observed(result.finish_evidence.observed):
                        report.update(status="HELD", reason="eat_consumption_not_observed")
                        break
                if result.finish_evidence and result.finish_evidence.status == "achieved" and (
                        result.reason != "activity_finish"
                        # D2: a tool-work goal is one effect; an achieved activity finish is the objective
                        # (the runtime now routes it as a goal finish; kept for older runtimes' journals).
                        or getattr(runtime.active_goal, "tool_work", None) is not None):
                    report["status"] = "OBJECTIVE_VERIFIED"; break
                if result.reason == "trial_decision_limit":
                    report["status"] = "STEP_LIMIT"; break
                goal = runtime.active_goal
                optional = (result.reason == "ask_owner" and result.applied
                            and result.question_basis == "optional")
                with runtime._lock:
                    # "superseded": a continuation raced the previous result's acceptance and published
                    # nothing (live attempt 10z5); same rule as a context change.
                    replanning = (result.discarded and result.reason in {"continuation_context_changed", "superseded"}
                                  and (runtime._pending_continuation is not None
                                       or runtime._inflight_generation is not None or not results.empty()
                                       or goal and goal.no_progress_until is not None
                                       # the runtime's deferred re-inspect runs once this delivery
                                       # finishes (live attempt 10z18 held in that window)
                                       or runtime._results_pending > 0
                                       or runtime._trial_deferred_resume is not None
                                       # WS2: the day-start rule resumes with day_start_cleared
                                       or runtime._day_start_active))
                # A play decision refused only because no current inspect preceded it (play_inspect_required) is
                # not terminal: the runtime keeps the goal and its continuation worker can inspect next. Holding
                # here ended live attempt 5 (2026-10-06 04:58Z) after the model's first, otherwise valid, act.
                evidence = result.action_evidence
                noop_action = (evidence is not None and evidence.lifecycle == "not_taken"
                               and evidence.request_id is None and runtime.active_goal is not None
                               and ((evidence.reason in {"approach_arrived_and_facing", "approach_publication_context_changed",
                                                         "approach_partial", "approach_blocked"} and evidence.cleanup_confirmed)
                                    or recoverable_gather_replan(runtime, evidence)
                                    # refused before any lease (live attempt 10z)
                                    or (evidence.reason == "invalid_approach_target" and evidence.before is None)))
                # Likewise a no-op approach (actor already adjacent to and facing the target; nothing published)
                # is not terminal: the runtime schedules the next decision. Holding here ended live attempt 9f.
                # One malformed provider output (semantic_mismatch, choice_unavailable) during a
                # continuation is refreshed by the runtime the same way; the goal is intact.
                from arti_stardew_runtime import _REFRESHED_CODEC_ERRORS
                malformed = ((result.reason in {"semantic_mismatch", "choice_unavailable"}
                              or result.reason in _REFRESHED_CODEC_ERRORS)
                             and (str(reason).startswith("continuation") or reason == "owner_turn")
                             and runtime.active_goal is not None)  # live 10v: missing_field
                # A failed continuation precheck publishes nothing; the runtime refreshes it the same
                # way (live attempt 10b ended on one). Discarded, but the goal is intact.
                precheck_refreshed = (result.discarded and result.reason == "continuation_precheck_failed"
                                      and str(reason).startswith("continuation") and runtime.active_goal is not None)
                # A play refusal is a pre-publication gate (nothing written); the runtime refreshes it
                # the same way as play_inspect_required (live attempt 10j: use_tool chosen one step early).
                play_refused = (result.reason == "play_inspect_required"
                                or (result.reason in {"store_act_offered_inspect_redundant",
                                                      "tool_act_offered_inspect_redundant"}
                                    and runtime.active_goal is not None)  # rule layer, live 10z51 / batch 1
                                or (result.reason == "play_activity_required"
                                    and runtime.active_goal is not None)  # live 10m, 10z4
                                or (str(result.reason or "").startswith(("gather_", "tool_work_"))
                                    and runtime.active_goal is not None
                                    and str(reason).startswith("continuation")))
                # A provider timeout the runtime chose to refresh (bounded there; live attempt 10y).
                with runtime._lock:
                    provider_refreshed = (result.reason in {"provider_error", "mismatch"}
                                          and runtime.active_goal is not None
                                          and (runtime._pending_continuation is not None
                                               or runtime._trial_deferred_resume is not None
                                               or runtime._results_pending > 0))  # live 10y, 10z25
                # D7 (live B2W5b): an inspect continuation discarded as "superseded" with nothing pending
                # ended the trial HELD (reason None). Recover it once per trial through the runtime's own
                # read-only refresh (fresh inspect + one continuation); a second one still holds.
                superseded_recovered = False
                if (result.discarded and result.reason == "superseded" and not replanning
                        and runtime.active_goal is not None and not report.get("superseded_recovered")):
                    superseded_recovered = runtime.recover_superseded_inspect()
                    report["superseded_recovered"] = superseded_recovered
                with runtime._lock:
                    progressed = completed_progress(result, reason, runtime.active_goal is not None,
                                                    bool(runtime._gather_mutation_unresolved))
                ui_noop, budget_note = menu_noop_admit(report, result, reason, runtime.active_goal is not None)
                if budget_note is not None:
                    with runtime._lock:
                        runtime._journal(budget_note)
                refused_recoverable = ((not result.applied and not result.discarded
                                        and (play_refused or noop_action or malformed or provider_refreshed
                                             or progressed or ui_noop))
                                       or precheck_refreshed or superseded_recovered)
                if refused_recoverable:
                    report.setdefault("recoverable_refusals", []).append(result.reason)
                live_recovery = live7_recovery_active(runtime, live7=live7,
                    day_start_only=day_start_only, reason=result.reason)
                if not optional and not replanning and not refused_recoverable and not live_recovery and (
                        result.reason in {"ask_owner", "provider_error", "provider_off", "owner_play_stopped"} or not result.applied):
                    report["status"] = "HELD"
                    report["reason"] = (runtime.continuation_hold_reason() if result.reason == "ask_owner"
                                        else (result.action_evidence.reason if result.action_evidence else result.reason))
                    break
                if live_recovery and not result.applied and not refused_recoverable:
                    runtime._live7_uncertain_ui(runtime._skill_session, str(result.reason or "trial_readback_unconfirmed"))
    except KeyboardInterrupt:
        report.update(status="OWNER_STOPPED",reason="keyboard_interrupt")
    except Exception as exc:
        report.update(status="FAILED", reason=type(exc).__name__)
        report.setdefault("exceptions", []).append(exception_record(exc, phase="trial"))
        # Preserve stable reason codes as well as the full private diagnostic.
        if isinstance(exc, ValueError) and str(exc).replace("_", "").replace(":", "").isalnum():
            report["reason"] = str(exc)
    finally:
        monitor_done.set()
        if monitor is not None: monitor.join(timeout=0.1)
        if said_lines is not None:
            said_lines.stop()
        if budget:
            try: budget.check_time()
            except ValueError: deadline_reached.set()
        if deadline_reached.is_set():
            report.update(status="TRIAL_DEADLINE", reason="trial_deadline")
        elif stop_event is not None and stop_event.is_set() and runtime is not None:
            report.update(status="OWNER_STOPPED", reason="owner_stop")
        if runtime is not None and overnight and not (stop_event is not None and stop_event.is_set()):
            grant_left = None
            if budget is not None:
                try:
                    grant_left = min(grant["expires_at"], budget.started + grant["limits"]["seconds"]) - clock()
                except (KeyError, TypeError):
                    grant_left = 0.0
            cap = overnight_budget(overnight_wait_s, grant_remaining_s=grant_left,
                                   elapsed_s=time.monotonic() - run_started)
            report["overnight"] = overnight_wait(runtime, wait_s=cap, stop_event=stop_event)
        if runtime is not None:
            # Native cancellation/cleanup does not wait on a provider return.
            if getattr(runtime, "_skills_enabled", False):
                skill_terminal_gate(report, runtime, live7=live7, day_start_only=day_start_only)
            goal = runtime.active_goal
            report["final_goal"] = sanitized({"gather": asdict(goal.gather_progress) if goal and goal.gather_progress else None,
                "tool_work": asdict(goal.tool_work) if goal and getattr(goal, "tool_work", None) else None,
                "transfer": goal.transfer_progress if goal else None, "last_action": goal.last_action if goal else None,
                "episodes":goal.episode_outcomes if goal else (),"criterion_id":goal.episode_criterion_id if goal else None,
                "store_close":goal.store_close if goal else None})
            day_state = runtime._day_start_state
            if day_state.started_at is not None and "day_start" not in report:
                report["day_start"] = {"actions": day_state.actions, "status": "blocked" if day_state.blocked else
                                       "done" if day_state.finished else "active", "reason": day_state.blocked or day_state.finished,
                                       "menus": list(day_state.menus)}
            ending = deadline_reached.is_set() or (stop_event is not None and stop_event.is_set())
            if live7 and hasattr(runtime, "live7_join_pending_reflection"):
                # A morning reflection still in flight finishes before shutdown closes its lane (a short day only);
                # a STOP waits briefly and an unfinished one stays pending for the next runner.
                if not runtime.live7_join_pending_reflection(REFLECTION_STOP_JOIN_S if ending else REFLECTION_JOIN_S):
                    report["mind_reflection_pending"] = "in_flight_at_exit"
            if (live7 and hasattr(runtime, "live7_bedtime_reflection") and not ending):
                # Mind slice 1: one bedtime reflection per verified sleep (she is asleep; it sends no game input).
                # A failure is journaled (mind_reflection_failed -> watcher alert); it never changes the day status.
                # fix54H: its facts are collected now; the provider call runs in the next runner (defer=True).
                try:
                    mind = runtime.live7_bedtime_reflection(defer=True)
                    if mind is not None:
                        report["mind_reflection"] = {k: mind.get(k) for k in ("ev", "code", "new_goals", "dropped", "pending")
                                                     if k in mind}
                except Exception as exc:  # noqa: BLE001
                    report["mind_reflection"] = {"ev": "mind_reflection_failed", "code": type(exc).__name__}
            try: stop_trial_runtime(runtime, live7=live7)
            except Exception as exc:
                report["cleanup"] = "UNCONFIRMED"
                report.setdefault("exceptions", []).append(exception_record(exc, phase="cleanup"))
            runtime._animal_trial_approval = None
            runtime._trial_budget_remaining = None
            if trial_provider is not None:
                trial_provider.trial_owner_authorized = False
            if type(runtime._commands) is SimulatedCommands:
                runtime._commands.stop_sensor()
            report["physical_inputs"] = runtime.physical_action_publications
            skill_report = runtime.skill_report() if hasattr(runtime, "skill_report") else None
            if skill_report is not None:
                report["skills"] = sanitized(skill_report)  # per-skill outcomes (Skills tahap 1)
            provider = runtime.decision_provider
            report["physical_requests"] = getattr(provider, "physical_requests", 0)
            report["physical_lanes"] = getattr(provider, "lane_owners", {})
        if budget:
            report["reserved"] = dict(budget.used)
            report["elapsed_seconds"] = max(0, clock() - budget.started)
    return report


def chest_memory_file(journal_path) -> Path:
    """FIXB13 D-B11-2: <run dir>/state/chest_contents.json (the journal dir is <run dir>/journal-attempt...)."""
    return Path(journal_path).resolve().parent / "state" / "chest_contents.json"


def trial_runtime_config(cfg, grant, *, skills=False, live7_stop_file=None, live7_help_dir=None):
    """The runtime config a trial runs with: the reviewed config, the grant's input cap, and the --skills flag
    (Skills tahap 1: the same primitives, gates and budgets; only the chooser between skills changes)."""
    if live7_approval(grant):
        out = dict(cfg, stardew_skills_enabled=True,
                   live7_moments_enabled=cfg.get("live7_moments_enabled") is True)
        for key in ("stardew_actions_per_session", "stardew_provider_requests_per_session"):
            out.pop(key, None)
        out["stardew_live7_resume"] = grant.get("live7_resume", {})
        progress_at = os.environ.get("ARTI_LIVE7_PROGRESS_AT")
        if progress_at:
            out["stardew_live7_progress_at"] = float(progress_at)
        out["stardew_live7_cycle_id"] = grant.get("cycle_id")
        out["stardew_live7_scope"] = grant.get("native_scope", {"session_id": grant.get("session")})
        if live7_stop_file is not None:
            out["stardew_live7_stop_file"] = str(Path(live7_stop_file).resolve())
        if live7_help_dir is not None:
            out["stardew_live7_help_dir"] = str(Path(live7_help_dir).resolve())
        return out
    configured_limit = cfg.get("stardew_actions_per_session", grant["limits"]["input"])
    approved = grant["limits"]["input"]
    if (isinstance(grant.get("input_decision"), str) and grant["input_decision"].strip()
            and type(approved) is int and INPUT_APPROVAL_DEFAULT < approved <= input_approval_max(grant.get("owner_text"))):
        # FIXB26 D-B23-2 (live V2B23ACC11b: `M1_INPUTS=134` approved, the trial config's 48 stayed the runtime's action cap ->
        # `action_session_limit` at 48 in the exit leg): the operator's recorded input decision IS the session cap too.
        configured_limit = max(configured_limit, approved)
    out = dict(cfg, stardew_actions_per_session=min(configured_limit, approved))
    if skills:
        out["stardew_skills_enabled"] = True
        if campaign_approval(grant):
            out.update(stardew_skill_session_max_model=24, stardew_skill_session_max_skills=24,
                       stardew_continuation_decisions=128, stardew_continuation_wall_seconds=5400)
    return out


class StopFile:
    def __init__(self, path, scope=None):
        self.paths = [Path(p) for p in path] if isinstance(path, (list, tuple)) else [Path(path)]
        self.scope = scope or {}

    def is_set(self):
        from arti_stardew_stop import pending
        return any(pending(p, self.scope) for p in self.paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="dry-run")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--config", type=Path, help="Explicit reviewed trial config; never auto-load private config")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--stop-file", type=Path, action="append", help="cooperative STOP checked by the existing trial monitor; repeatable")
    parser.add_argument("--live7-stop-file", type=Path, help="run's LIVE7_STOP path for the core safety hook")
    parser.add_argument("--live7-help-dir", type=Path, help="run folder for the optional arti.help/1 file relay")
    parser.add_argument("--live7-day-start-only", action="store_true",
                        help="Live7 lost night: run only the existing day-start rule on an end-of-night menu; no owner turn")
    parser.add_argument("--skills", action="store_true",
                        help="Skills tahap 1: the owner goal runs as a skill sequence (stardew_skills_enabled)")
    parser.add_argument("--overnight", action="store_true",
                        help="FIXB17: after the goal, keep the runtime alive until the day-start rule cleared a night (<= 300 s)")
    parser.add_argument("--skills-envelope-ok", action="store_true",
                        help="allow a --skills trial to raise the approved input to the plan's envelope (max 160)")
    parser.add_argument("--inputs-ok", action="store_true",
                        help="FIXB23: allow a combat goal to raise the approved input to its envelope (kill 1 = 134, max 160)")
    parser.add_argument("--inputs", type=int, default=None, metavar="N",
                        help="operator input envelope: 1..160, or 1..280 for the animal routine; defaults unchanged")
    args = parser.parse_args(argv)
    actual = identity(artifact=args.artifact)
    if args.mode == "dry-run":
        report = run_trial(actual=actual)
    else:
        # Reading approval is not constructing a provider or opening a listener.
        grant = json.loads(args.approval.read_text()) if args.approval else None
        config_bytes = args.config.read_bytes() if args.config else b"{}"
        cfg = json.loads(config_bytes)
        profile = {k: cfg.get(k) for k in ("stardew_decision_provider_kind", "stardew_decision_model", "stardew_decision_effort")}
        def factory(mode, admit):
            from arti_stardew_runtime import StardewGoalRuntime, construct_stardew_decision_provider
            if hashlib.sha256(config_bytes).hexdigest() != grant.get("config_sha256"):
                raise ValueError("config_mismatch")
            # Both runtime and provider consume the SAME explicitly reviewed
            # continuation gate. Do not silently enable it after provider creation.
            if cfg.get("stardew_runtime_enabled") is not True or cfg.get("stardew_continuation_enabled") is not True:
                raise ValueError("autonomous_trial_config_required")
            extra = {}
            if mode == "model-no-game":
                sim = SimulatedCommands(Path(grant["journal_path"]) / "unused-command-path",
                    **cfg.get("stardew_trial_simulation",{}))
                extra = dict(state_store=sim.state, observation_store=sim.observations,
                             command_io=sim, start_listener=False)
            runtime_cfg = trial_runtime_config(cfg, grant, skills=args.skills, live7_stop_file=args.live7_stop_file,
                                               live7_help_dir=args.live7_help_dir)
            bench = cfg.get("rig_bench_trial")
            if bench is not None:
                from scripts.free_play.bench_choices import NoModelProvider, TrialChoices
                if not live7_approval(grant) or not args.skills or mode != "combined":
                    raise ValueError("bench requires a native Live7 trial")
                driver = TrialChoices(bench)
            provider = (NoModelProvider() if bench and bench["mode"] == "buttons" else
                        ObservedDriver() if mode == "native-no-network" else
                        construct_stardew_decision_provider(runtime_cfg))
            if provider is None:
                raise ValueError("provider_unavailable")
            if mode != "native-no-network": provider.physical_start_guard = admit
            # Live 2026-10-07 (M1 attempt 10z2): the runtime's default continuation ceiling (24 decisions)
            # ended a chop at 24 autonomous decisions although the trial config says 64. A trial passes
            # its configured continuation limits explicitly, never above the owner-approved grant
            # (provider requests, seconds); the runtime still takes the minimum with the config.
            continuation_limits = (128, 1800, 8) if live7_approval(grant) else (
                (128 if args.skills and campaign_approval(grant) else
                 min(int(cfg.get("stardew_continuation_decisions", 24)), int(grant["limits"]["provider"]))),
                min(float(cfg.get("stardew_continuation_wall_seconds", 1800)), float(grant["limits"]["seconds"])),
                int(cfg.get("stardew_continuation_per_minute", 8)))
            if mode != "model-no-game" and not runtime_cfg.get("stardew_chest_memory_file"):
                # FIXB13 D-B11-2: chest contents survive the per-trial process in the operator run dir.
                runtime_cfg["stardew_chest_memory_file"] = str(chest_memory_file(grant["journal_path"]))
            runtime = StardewGoalRuntime(config=runtime_cfg,
                provider=provider, trial_admission=admit, journal_dir=grant["journal_path"], start_worker=True,
                continuation_limits=continuation_limits, **extra)
            if bench:
                driver.attach(runtime, provider)
            return runtime
        session = "simulation-command"
        if args.mode != "model-no-game":
            # Read actual published identity, never infer it from the approval itself.
            from arti_stardew_command import baca_sesi
            session = baca_sesi(cfg.get("stardew_command_dir")).session_id if cfg.get("stardew_command_dir") else None
        report = run_trial(mode=args.mode, grant=grant, actual=actual, profile=profile,
                           session=session,
                           journal=grant.get("journal_path") if grant else None, factory=factory,
                           native_provenance="configured_native_transport",
                           skills_envelope_ok=args.skills_envelope_ok, overnight=args.overnight,
                           inputs_ok=args.inputs_ok, inputs=args.inputs,
                           day_start_only=args.live7_day_start_only,
                           stop_event=StopFile((args.stop_file or []) + ([args.live7_stop_file] if args.live7_stop_file else []),
                                               dict((grant or {}).get("native_scope") or {}, cycle_id=(grant or {}).get("cycle_id")))
                                      if args.stop_file or args.live7_stop_file else None)
    payload = json.dumps(sanitized(report), indent=2, ensure_ascii=False)
    if args.report:
        args.report.write_text(payload + "\n", encoding="utf8")
        # DISK-A: Live7 keeps stdout.txt beside report.json; readers take the report from the file, so
        # stdout carries a pointer instead of a second copy of the whole report (3-7 MB per game day).
        print(json.dumps(dict(report=str(args.report), status=report.get("status"), reason=report.get("reason"),
                              report_bytes=len(payload)), ensure_ascii=False))
    else:
        print(payload)
    # LIVE7_DAY_VERIFIED: verified bed + next morning (arti_stardew_live7 day contract); a lost day stays 2.
    return 0 if report["status"] in {"NOT_RUN", "OBJECTIVE_VERIFIED", "NOT_APPLICABLE", "LIVE7_DAY_VERIFIED",
                                     LIVE7_DAY_START_CLEARED, LIVE7_DAY_START_IDLE} else 2


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
