"""soak.py: A4, a 60-minute background soak, chained M1 trials (each <= 600 s, its own approval and journal).

  soak.py run --label soak1 [--minutes 60] [--host-idle] [--dismiss-levelup] [--src SHA7] [--dll8 HEX8]
              [--park-dx 4 --park-dy 3] [--stage auto|always|never] [--host-shot-every 60] [--dry-run]
              [--mode auto|pad|v1] [--owner-playing] [--goals wood,tools,chest,shop,fish,combat] [--owner-wait 900]
  soak.py report DIR        rebuild the receipt from DIR/soak.jsonl (writes a new SOAK_RECEIPT-rebuilt-<utc>.md)

Per trial k (label <label>-t<k>), between trials only (the port and the inbox are free then):
  pose (game time, day, location, menu, energy) -> rollover wait if time >= 22:00 or < 180 real s to 02:00 or
  energy < 30 -> Cabin exit walk (run_attempt.sh DRY) -> layout (find_layout.py --plan-only + walk_to.py, only when
  the previous trial had no inputs / no target, or --stage always) -> cursor park (fg farmhand, move, fg host) ->
  `prep_trial.py approval --seconds S` with S = min(600, real seconds to 02:00 - 60) and the exact M1 goal text ->
  `prep_trial.py run` (blocks <= 700 s) -> post-checks -> preserve-move run/journal (retry on PermissionError, never
  delete). The approval envelope is never widened: S <= 600 and every other limit is prep_trial's.
Monitors during a trial use files + user32 only (no port, no inbox): every 2 s foreground / IsWindow / IsHungAppWindow
/ IsIconic / pids / farmhand-exit.json; every 5 s session.json, new SMAPI log bytes, journal line count; every 60 s
SMAPI working set and a HOST PrintWindow (read-only). Between trials: stuck input (two poses 3 s apart, session flags,
every background run has TERMINAL containment=RELEASE_VERIFIED), crash, leak into the host (ui-op-log audit,
foreground timeline, host frame check with --host-idle), menu blocks (LevelUp OK only with --dismiss-levelup and only
when the banner AND its OK button are on screen; a profession choice or any other menu stops the soak).

Host gone during a trial: the trial cannot be aborted (no kill); the soak waits for the runner's own limit, sends
nothing more, preserves the journal and stops (exit 3). Any unexpected exception also stops with exit 3.
Stop: wall >= --minutes (the running trial finishes), crash, stuck input, leak, unknown menu, 3 consecutive trials with
physical_inputs == 0, or the stop file RUN/SOAK_STOP (the CT creates it; checked between trials and while waiting).
Output RUN/soak-<label>-<utc>/: soak.jsonl (every event), SOAK_RECEIPT.md, receipt.json, trial stdout/report copies,
smapi-slice.txt, host shots. Exit: 0 PASS | 1 finished, not PASS | 2 usage/precondition | 3 stopped by a monitor |
4 stop file. PASS criteria are the WS5 brief's 2.5; working-set growth is reported, not gated (HYPOTHESIS).

Primitives v2 (--mode pad, or auto with GamepadInputEnabled in the installed mod config): the soak NEVER sends input or
foregrounds anything (a guard turns any ui_op fg/click/move/key/hold/type/pos/close into a stop): no cursor park, no
tool select by key (the runtime has select_slot), no Cabin walk, no staging walk, no LevelUp click (a farmhand menu is
`waiting_for_owner` until it clears, <= --owner-wait s, else stop 3). `pad_watch.py preflight` before the soak and before
every trial; `pad_watch.py watch` runs beside the soak (RUN/soak-*/pad-watch.jsonl) and any violation (unblocked OS
warp, pad held after a terminal, ForceOn on a focused farmhand) stops it at the next boundary. The farmhand in front at
ANY time is a leak (not only during a trial). The ui-op-log audit expects no input op at all.
--owner-playing (alias --owner-plays; PLAN §5 V2SOAK1): pad mode while the owner actively plays the HOST: no HOST frame
check (refused with --host-idle), the owner's foreground app is logged by exe name only (never a title), the night
(and any HOST-side ReadyCheck) is `waiting_for_owner` (the soak never clicks the HOST), goals rotate through --goals
(combat only when the game time is 09:00-18:00, PLAN §5.1 step 11). Receipt criteria add pad_watch_0 and fh_never_front.

FIXB25 (V2SOAK1 prep, D-SOAK-1..8): pad mode issues goals until 24:00, then ARTI's own bed goal (`tidur`, prep_trial run
--overnight) once per game day (`soak_night`; a night without ARTI in bed is a counted pass-out, criterion no_pass_out); a
farmhand ShippingMenu / LevelUpMenu starts a `prep_trial run --day-start` trial (ARTI's day-start rule clears it,
`soak_day_start`, <= 2 per day, then waiting_for_owner); --goal-inputs KIND=N / --inputs-ok approve a goal kind's input
(combat 134, the FIXB24 approval marker) and a runner refusal before any input leaves the rotation (`goal_refused`, never a
zero-input trial); menu goals `journal` / `menu`; tagged HOST pos / close rows listed (excluded only on the HOST hwnd); the
markdown receipt shows every HOST-driver row; 0 trials = INCONCLUSIVE and PASS needs >= --min-verified (6) verified goals;
the ui-op rows after the last trial are audited at shutdown (leak check `tail`).

FIXB31 (owner decision 2026-10-12, one 60-min run covers the acceptance variety): --goals all (alias --mix acceptance)
rotates ACCEPTANCE_MIX (tools, wood, chest, fish, combat + exit, sell, buy, ship, plant, water, pet, journal, menu, eat; the
bed goal at 24:00 as before). A HELD / FAILED goal is recorded (`soak_goal_result`: verified / partial / held / refused, the
last reason, the preserved journal path) and the rotation continues; a goal whose precondition fails NOW is not run
(`soak_goal_skipped reason=precondition:<x>`: time window, no rod, no parsnip, no seeds, no watering can, energy full,
no seed in season, combat input not approved). Global stops stay: leak, pass-out (mix: stop at the next boundary), crash,
stuck input, OPS §4 three zero-input trials in a row. Per-goal input envelopes are the FIXB25 --goal-inputs / --inputs-ok
approvals (combat 134, written only on the combat approval). The receipt adds a per-goal table; PASS still needs >= 6
verified.

FIXB32 D-SOAK4-1: completed action_result with cleanup_confirmed and an instant protocol outcome (selection,
transfer, container exit; including settled no-effect / zero-call rejection) is a third input family: its completed
SMAPI RESULT proves it, not a background HOLD
terminal. Unknown / held outcomes still need RELEASE_VERIFIED; menu commands still need their own VERDICT. Physical
inputs with an empty SMAPI slice remain unproved.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402
import win_facts as wf  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from arti_stardew_seeds import owned_crop_seeds  # noqa: E402

GOAL = "Arti, main mandiri: gather wood from one nearby tree, then store the newly obtained wood in a chest"
OWNER_TEXT = os.environ.get("M1_OWNER_APPROVAL") or "OWNER APPROVAL TEXT (set M1_OWNER_APPROVAL)"
OWNER_URL = os.environ.get("M1_OWNER_URL") or "https://github.example.invalid/owner/private-lab-repository/issues/example-b"
SEASONS = ("spring", "summer", "fall", "winter")
FAST_S, MEDIUM_S, SLOW_S = 2.0, 5.0, 60.0
HUNG_CRASH_S = 10.0
ENERGY_MIN = 30
ROLLOVER_SLACK_S = 300.0      # rollover wait limit = real seconds to 02:00 (at its start) + this
ZERO_INPUT_STREAK = 3
INPUT_OPS = {"click", "move", "type", "key", "hold", "pos", "close"}
NIGHT_MENUS = ("SaveGameMenu", "ShippingMenu", "ReadyCheckDialog")   # expected while the night rolls over
NO_TARGET_RE = re.compile(r"no_tree|no_target|no_offer|approach|blocked|unreachable", re.I)
PAD_FORBIDDEN_OPS = INPUT_OPS | {"fg"}
# Canonical bounded owner objectives for the v2 rotation (PLAN §5 V2SOAK1); no ad-hoc operator runner needed.
# Grammar admission is unit-tested. Actual target availability/traversal still needs live acceptance.
GOALS = {
    "wood": GOAL,
    "tools": "Arti, main mandiri: break one rock",
    "chest": "Arti, main mandiri: gather additional 1 wood and store",
    "shop": "Arti, main mandiri: jual satu parsnip",
    "fish": "Arti, tangkap satu ikan",   # FIXB31: the wording (was "catch one fish")
    "combat": "Arti, kalahkan satu slime",  # FIXB31: the wording (was "kill one slime")
    # FIXB25 D-SOAK-4: guaranteed-input menu fillers (PLAN §5 lists menu goals in the mix; both parse as menu objectives)
    "journal": "Arti, main mandiri: buka jurnal lalu tutup",
    "menu": "Arti, main mandiri: buka menu lalu tutup",
    # FIXB31 owner mix (2026-10-12): the rest of the acceptance variety, owner wording
    "sell": "Arti, main mandiri: jual satu parsnip",
    "buy": "Arti, main mandiri: beli 1 parsnip seeds",      # the season's seed (mix_goal_text); spring text by default
    "ship": "Arti, main mandiri: kirim 1 parsnip",
    "plant": "Arti, main mandiri: tanam satu benih",
    "water": "Arti, main mandiri: siram 1 petak",
    "pet": "Arti, main mandiri: elus kucingnya, Dudley",
    "eat": "Arti, main mandiri: makan",
}
GOAL_WINDOWS = {"combat": (900, 1800),                           # enter 09:00-18:00 only (PLAN §5.1 step 11)
                "sell": (900, 1700), "buy": (900, 1700),           # FIXB31: Pierre's hours
                "pet": (600, 1200)}                                # FIXB31: the owner's "mornings"
# FIXB31: --goals all / --mix acceptance (the owner's order)
ACCEPTANCE_MIX = ("tools", "wood", "chest", "fish", "combat", "sell", "buy", "ship", "plant", "water", "pet",
                  "journal", "menu", "eat")
MIX_ALIASES = ("all", "acceptance")
SEASON_SEEDS = {"spring": "parsnip seeds", "summer": "wheat seeds", "fall": "wheat seeds"}  # Pierre, cheapest per season
SEASON_SEED_QIDS = {"spring": "(O)472", "summer": "(O)483", "fall": "(O)483"}
ROD_QIDS = ("(T)BambooPole", "(T)TrainingRod", "(T)FiberglassRod", "(T)IridiumRod", "(T)AdvancedIridiumRod")
ENERGY_FULL = 270                            # a fresh farmer's max energy (no stardrops): `makan` only below it
PIERRE_GOALS = ("sell", "buy", "shop")       # FIXB31 review: Pierre's shop is closed on Wednesdays (day % 7 == 3)
OWNER_WAIT_POLL_S = 10.0
# FIXB25 D-SOAK-1 (no bed goal: ARTI passed out every night): in pad mode goals run until 24:00, then ARTI goes to bed by
# its own sleep rule (routine sleep goal, the runner's --overnight wait clears the day-start menus). Not in --goals.
BED_GOAL = ("bed", "Arti, main mandiri: tidur")
NIGHT_FROM = 2400
PAD_MIN_TRIAL_S = 60
BED_TRIAL_S = 600
# FIXB25 D-SOAK-2: end-of-night screens ARTI's own day-start rule clears (prep_trial run --day-start starts a trial on them)
DAY_START_MENUS = ("ShippingMenu", "LevelUpMenu")
DAY_START_TRIALS_MAX = 2
# FIXB25 D-SOAK-3: runner refusals before any input (not a zero-input trial: the goal leaves the rotation)
REFUSAL_REASONS = ("envelope_exceeds_approval", "input_decision_missing", "inputs_out_of_range")
INPUT_APPROVAL_DEFAULT = 48
INPUT_APPROVAL_MAX = 160
# FIXB25 D-SOAK-7: PASS needs at least this many OBJECTIVE_VERIFIED trials (0 trials is never PASS)
MIN_VERIFIED_DEFAULT = 6


class Stop(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


# ============================================================ pure helpers
def minutes_of(hhmm: int) -> int:
    return (int(hhmm) // 100) * 60 + int(hhmm) % 100


def seconds_until_0200(time_of_day: int) -> float:
    """A game day 06:00 -> 02:00 lasts ~14 real minutes (7 s per 10 game minutes = 0.7 s per game minute)."""
    return (minutes_of(2600) - minutes_of(time_of_day)) * 0.7


def trial_seconds(time_of_day: int):
    """S = min(600, seconds to 02:00 - 60); None = do not start (>= 22:00 or < 180 s left)."""
    left = seconds_until_0200(time_of_day)
    if time_of_day >= 2200 or left < 180:
        return None
    s = int(min(600, left - 60))
    assert 60 <= s <= 600, s
    return s


def pad_trial_seconds(time_of_day: int):
    """FIXB25 D-SOAK-1: pad mode keeps issuing goals until 24:00: S = min(600, seconds to 02:00 - 60), at least
    PAD_MIN_TRIAL_S; None from NIGHT_FROM or when less is left (the bed goal takes the rest of the night)."""
    left = seconds_until_0200(time_of_day)
    if time_of_day >= NIGHT_FROM or left - 60 < PAD_MIN_TRIAL_S:
        return None
    return int(min(600, left - 60))


def is_day_start_menu(menu) -> bool:
    return bool(menu) and any(m in str(menu) for m in DAY_START_MENUS)


def goal_inputs_map(spec) -> dict:
    """FIXB25 D-SOAK-3: --goal-inputs 'combat=134,fish=60' -> {kind: N} (1..INPUT_APPROVAL_MAX); ValueError otherwise."""
    out = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        name, _, n = part.partition("=")
        name, n = name.strip(), n.strip()
        if name not in GOALS or not n.isdigit() or not 1 <= int(n) <= INPUT_APPROVAL_MAX:
            raise ValueError(f"--goal-inputs: KIND=N with KIND in {', '.join(GOALS)} and N 1..{INPUT_APPROVAL_MAX} (got {part!r})")
        out[name] = int(n)
    return out


def computed_goal_inputs(text):
    """FIXB25 D-SOAK-3 (--inputs-ok): the runner's own envelope for a goal text (combat: 134 for one kill), else None."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
        import arti_stardew_combat as C
        return C.combat_input_envelope(text)
    except Exception:  # noqa: BLE001 - an unreadable envelope = no raise (the runner refuses with the number)
        return None


def owned_items(pose, inv_facts) -> set:
    """FIXB31: lower-case item names (pose hotbar, progress.inventory display names) and qualified ids ARTI owns."""
    out = set()
    for s in ((pose or {}).get("inventory") or {}).get("hotbar") or ():
        if isinstance(s, dict) and s.get("name"):
            out.add(str(s["name"]).lower())
    for f in (inv_facts or {}).values():
        if isinstance(f, dict) and f.get("occupied"):
            for k in ("qualified_item_id", "display_name", "name"):
                if f.get(k):
                    out.add(str(f[k]) if k == "qualified_item_id" else str(f[k]).lower())
    return out


def mix_goal_text(name, pose, owned=None):
    """FIXB31: the goal text for this pose (buy: the season's seed)."""
    if name in ("buy", "plant"):
        seed = SEASON_SEEDS.get(((pose or {}).get("date") or {}).get("season"))
        if name == "plant" and owned is not None and seed is not None:
            season = ((pose or {}).get("date") or {}).get("season")
            if seed not in owned and SEASON_SEED_QIDS[season] not in owned:
                return GOALS[name]  # runtime selects the actual in-season seed in the bag
        if seed:
            return (f"Arti, main mandiri: beli 1 {seed}" if name == "buy"
                    else f"Arti, main mandiri: plant one {seed}")
    return GOALS[name]


def goal_precondition(name, pose, owned, approved_inputs=None, inventory_known=True):
    """FIXB31: why goal `name` cannot run NOW (`precondition:<x>`), or None. Only observable facts; never a guess that
    blocks a goal whose facts are unreadable (review: a failed inventory read = ownership unknown -> the goal runs)."""
    tod = (pose or {}).get("time_of_day") or 0
    lo, hi = GOAL_WINDOWS.get(name, (0, 2600))
    if not lo <= tod < hi:
        return "precondition:time_window"
    day = ((pose or {}).get("date") or {}).get("day")
    if name in PIERRE_GOALS and type(day) is int and day % 7 == 3:
        return "precondition:shop_closed_wednesday"
    has = lambda *words: not inventory_known or any(w in o for o in owned for w in words)
    if name == "fish" and not (set(ROD_QIDS) & owned or has("rod", "bamboo pole")):  # has(): True when unknown
        return "precondition:no_rod"
    if name == "combat" and approved_inputs is None:
        return "precondition:combat_inputs_not_approved"   # the runner would refuse it (FIXB23 envelope 134)
    if name in ("sell", "ship", "shop") and inventory_known and not ("(O)24" in owned or "parsnip" in owned):
        return "precondition:no_parsnip"
    if name == "buy" and ((pose or {}).get("date") or {}).get("season") not in SEASON_SEEDS:
        return "precondition:no_seed_in_season"
    if name == "plant" and inventory_known and not (owned_crop_seeds(owned) or has("seeds")):
        return "precondition:no_seeds"
    if name == "plant":
        season = ((pose or {}).get("date") or {}).get("season")
        if season is not None and inventory_known and not owned_crop_seeds(owned, season):
            return "precondition:no_seed_in_season"
    if name == "water" and not has("watering can", "wateringcan"):
        return "precondition:no_watering_can"
    energy = (pose or {}).get("energy")
    if name == "eat" and isinstance(energy, (int, float)) and energy >= ENERGY_FULL:
        return "precondition:energy_full"
    return None


def goal_result_status(report, phys, refused) -> str:
    """Inputs show activity, not objective progress; a deadline has its own bucket (FIXB33)."""
    if refused:
        return "refused"
    if (report or {}).get("status") == "OBJECTIVE_VERIFIED":
        return "verified"
    if (report or {}).get("status") == "TRIAL_DEADLINE":
        return "deadline"
    return "partial" if phys else "held"


def sleep_evidence(journal_text) -> bool:
    """FIXB25 review MAJOR: the runner journal shows ARTI went to bed by its own rule: the Sleep question answered Yes,
    `bed_waiting_for_host`, or a bed finish (`finish_verified` observed_day_change)."""
    for line in (journal_text or "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev = e.get("ev")
        if ev == "bed_waiting_for_host":
            return True
        if ev == "objective_progress" and e.get("answered") == "Yes" and e.get("question") == "Sleep":
            return True
        if ev == "finish_verified" and ((e.get("verdict") or {}).get("reason") == "observed_day_change"):
            return True
    return False


def day_key(date) -> tuple:
    d = date or {}
    season = SEASONS.index(d.get("season")) if d.get("season") in SEASONS else -1
    return (int(d.get("year") or 0), season, int(d.get("day") or 0))


def park_client(pose, dx, dy):
    """Client coords of ARTI's tile + (dx,dy), same math as run_attempt.sh (camera centred on the player), clamped
    inside the client and above the toolbar."""
    px, py = round(pose["pixel"]["x"]), round(pose["pixel"]["y"])
    tx, ty = pose["tile"]["x"] + dx, pose["tile"]["y"] + dy
    cx = tx * 64 + 32 - (px + 32 - 640)
    cy = ty * 64 + 32 - (py + 32 - 360)
    return (tx, ty), (min(1240, max(40, cx)), min(590, max(40, cy)))


def park_offset(layout_best, me, default):
    """Away from the tree: tree west/north of ARTI -> (+4,+3), east/south -> (-4,-3) (OPERATOR_RUNBOOK section 7)."""
    if not layout_best:
        return default
    tx, ty = layout_best["tree"]
    return (4 if tx <= me[0] else -4, 3 if ty <= me[1] else -3)


def safe_park(pose, dx, dy):
    """The park offset, mirrored if its tile is a walk_to.KNOWN_HAZARDS tile (a park on the pet bowl gave
    CURSOR_NOT_INERT:BUILDING, live B3W1a). Stop when every mirror is a hazard."""
    import walk_to as W
    hz = W.hazard_tiles(pose.get("location"))
    for ddx, ddy in ((dx, dy), (-dx, dy), (dx, -dy), (-dx, -dy)):
        if (pose["tile"]["x"] + ddx, pose["tile"]["y"] + ddy) not in hz:
            return ddx, ddy
    raise Stop(3, f"every park mirror of ({dx},{dy}) is a known hazard tile")


def inventory_slots(facts, want_qid):
    """(active slot, first slot holding want_qid, {slot: qualified id}) from progress.inventory facts; names None when
    there are no facts."""
    if not facts:
        return None, None, None
    active, target, names = None, None, {}
    meta = next((f for f in facts.values() if isinstance(f, dict) and f.get("kind") == "meta"), {}) or {}
    active = meta.get("active_slot")
    for f in facts.values():
        if isinstance(f, dict) and f.get("kind") == "slot" and f.get("occupied"):
            names[f["slot"]] = f.get("qualified_item_id")
            if f.get("is_active"):
                active = f["slot"]
            if f.get("qualified_item_id") == want_qid and (target is None or f["slot"] < target):
                target = f["slot"]
    return active, target, names


RUN_RE = re.compile(r"\] background .*?\brun=(\d+)")
# FIXB28 D-SOAK-9: the leased one-shot menu commands (pad_button / ui_press / ui_nav / ui_key) log their own admission and a
# VERDICT line (no background run): `arti_command pad_button [ADMIT ]request_id=...` ... `arti_command pad_button VERDICT ...`
CMD_RE = re.compile(r"\] arti_command (pad_button|ui_press|ui_nav|ui_key) (?:(ADMIT|VERDICT) )?request_id=(\S+)(.*)")
# FIXB28 review: ui_key logs its VERDICT without the `arti_command` prefix (`] ui_key VERDICT request_id=...`)
CMD_VERDICT_RE = re.compile(r"\] (?:arti_command )?(pad_button|ui_press|ui_nav|ui_key) VERDICT request_id=(\S+)(.*)")
MENU_EVENTS = ("ui_press", "ui_nav", "pad_button", "ui_key")
# The synchronous SelectItemInput / PlayerInteractionInput / ContainerExitInput paths do not arm a background hold.
# ActionEvidenceView journals outcome + cleanup, not the primitive/kind or hold ticks. Never infer no hold from
# unchanged pose or zero swings: a movement / world-interact hold may have both.
INSTANT_OUTCOMES = {"SelectionObserved", "SelectionUnchanged", "SelectionRejected",
                    "TransferObserved", "TransferNoEffect", "TransferRejected", "MenuExited", "ExitRejected"}
RESULT_RE = re.compile(r"\] arti_command RESULT request_id=(\S+) status=completed\b(.*)")
TERM_RE = re.compile(r"\] background TERMINAL run=(\d+)\b.*?containment=(\S+)")
LEVEL_RE = re.compile(r"^\[\d\d:\d\d:\d\d (\w+)\s+([^\]]+)\]")


def smapi_stuck(text):
    """Release proof over a SMAPI slice: every background run has a TERMINAL with containment=RELEASE_VERIFIED, no
    T5PressReleaseUnresolved, no latch_restored_exact=False, no InputUncertain without a later TERMINAL."""
    runs, term, uncertain, unresolved, latch = set(), {}, {}, [], 0
    admitted, verdicts, uncertain_cmds, instant_results = {}, set(), [], set()
    for line in (text or "").splitlines():
        result = RESULT_RE.search(line)
        outcome = re.search(r"\boutcome=(\S+)", result.group(2)) if result else None
        if outcome and outcome.group(1) in INSTANT_OUTCOMES:
            instant_results.add(result.group(1))
        v = CMD_VERDICT_RE.search(line)
        if v:
            verdicts.add(v.group(2))
            if "uncertain" in v.group(3).lower():
                uncertain_cmds.append(line.strip()[:300])  # FIXB28 review: released, effect unknown: flagged
            continue
        c = CMD_RE.search(line)
        if c:
            if "refuse=-" in c.group(4) or "refuse=" not in c.group(4):
                admitted[c.group(3)] = line.strip()[:300]
            continue
        m = RUN_RE.search(line)
        if m:
            runs.add(int(m.group(1)))
        t = TERM_RE.search(line)
        if t:
            term[int(t.group(1))] = t.group(2)
            uncertain.pop(int(t.group(1)), None)
        if "T5PressReleaseUnresolved" in line:
            unresolved.append(line.strip()[:300])
        if "latch_restored_exact=False" in line:
            latch += 1
        if "InputUncertain" in line and m and not t:
            uncertain[int(m.group(1))] = line.strip()[:300]
    missing = sorted(r for r in runs if term.get(r) != "RELEASE_VERIFIED")
    commands_open = [v for k, v in admitted.items() if k not in verdicts]
    ok = not missing and not unresolved and not latch and not uncertain and not commands_open
    return dict(ok=ok, runs=len(runs), verified=sum(1 for v in term.values() if v == "RELEASE_VERIFIED"),
                missing=missing[:20], unresolved=unresolved[:5], latch_false=latch, uncertain_open=list(uncertain.values())[:5],
                commands=len(verdicts), commands_open=commands_open[:5], commands_uncertain=uncertain_cmds[:5],
                _verdict_ids=sorted(verdicts), _instant_ids=sorted(instant_results))


def stuck_pose_evidence(pose):
    """FIXB37: retain the deciding reads, without copying the full inventory/menu graph.

    An old DLL has no player_control block: null explicitly means unknown, never free.
    These facts are diagnostics only; they do not grant a stuck-oracle exemption.
    """
    if not pose:
        return None
    facts = {key: pose.get(key) for key in (
        "location", "tile", "pixel", "facing", "date", "time_of_day",
        "is_player_free", "can_player_move", "active_menu", "player_control")}
    inventory = pose.get("inventory") or {}
    facts["selected_slot"] = inventory.get("active_slot")
    return facts


def trial_input_families(journal_text):
    """The trial's physical inputs: menu commands (VERDICT), confirmed instant outcomes (completed RESULT), and all
    other settled action_result request ids (background TERMINAL RELEASE_VERIFIED). None when the journal is unreadable.
    FIXB32: only a completed instant outcome with confirmed cleanup is exempt from background release proof."""
    if not journal_text:
        return None
    menu, background, instant = set(), set(), set()
    for line in journal_text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        rid = e.get("request_id")
        if e.get("ev") in MENU_EVENTS and isinstance(rid, str):
            menu.add(rid)
        elif e.get("ev") == "action_result":
            a = e.get("action") or {}
            if isinstance(a.get("request_id"), str) and a.get("lifecycle") in ("completed", "uncertain", "interrupted"):
                if (a.get("lifecycle") == "completed" and a.get("cleanup_confirmed") is True
                        and a.get("outcome") in INSTANT_OUTCOMES):
                    instant.add(a["request_id"])
                else:
                    background.add(a["request_id"])
    return {"menu": menu, "background": background - menu, "instant": instant - menu - background}


def release_proof_missing(sm, families, phys):
    """Per input family: menu VERDICT by id, instant completed RESULT by id, background RELEASE_VERIFIED run.
    Without the journal: a background run, command VERDICT, or confirmed instant RESULT (never an empty slice)."""
    if not phys:
        return None
    verdicts = set(sm.get("_verdict_ids") or ())
    if families is None:
        return None if (sm["runs"] or sm.get("commands") or sm.get("_instant_ids")) else "physical inputs but an empty SMAPI slice"
    missing = sorted(families["menu"] - verdicts)
    if missing:
        return "menu command without a VERDICT: " + ", ".join(m[:16] for m in missing[:3])
    if families["background"] and not sm.get("verified"):
        return "background inputs without a RELEASE_VERIFIED run"
    missing = sorted(families.get("instant", set()) - set(sm.get("_instant_ids") or ()))
    if missing:
        return "instant input without a completed RESULT: " + ", ".join(m[:16] for m in missing[:3])
    if not any(families.values()) and not (sm["runs"] or sm.get("commands") or sm.get("_instant_ids")):
        return "physical inputs but an empty SMAPI slice"
    return None


def smapi_errors(text):
    """ERROR lines from Arti Stardew Bridge = crash signals; other ERROR lines are recorded only (e.g. the Steam
    achievements line at startup). Exceptions inside WARN lines (inbox read races) are not crashes."""
    bridge, other = [], []
    for line in (text or "").splitlines():
        m = LEVEL_RE.match(line.strip())
        if not m or m.group(1) != "ERROR":
            continue
        (bridge if "Arti Stardew Bridge" in m.group(2) else other).append(line.strip()[:400])
    return dict(bridge=bridge, other=other)


def swings_in(text):
    return sum(int(n) for n in re.findall(r"HOLD COMPLETION .*?kind=Tool .*?swings=(\d+)", text or ""))


def trial_stats(report, smapi_text):
    fg = (report or {}).get("final_goal") or {}
    g, tr = fg.get("gather") or {}, fg.get("transfer") or {}
    sc, cc = g.get("starting_count"), g.get("current_count")
    return dict(status=(report or {}).get("status"), reason=(report or {}).get("reason") or g.get("reason"),
                physical_inputs=(report or {}).get("physical_inputs"), elapsed=(report or {}).get("elapsed_seconds"),
                swings=swings_in(smapi_text), trees_felled=1 if g.get("target_status") == "gone" else 0,
                wood_delta=(cc - sc) if isinstance(cc, int) and isinstance(sc, int) else None,
                stored=tr.get("transferred"), store_reached=tr.get("status") == "PAIRED_TRANSFER" or bool(tr.get("transferred")))


def audit_ui_log(rows, fh, host, trial_windows):
    """Input reaching the HOST. rows: ui-op-log entries (utc 'YYYY-mm-ddTHH:MM:SSZ'); trial_windows: [(utc0, utc1)].
    Inside a trial: only `shot` and `fg <host>` are allowed. Outside: every input op must target the farmhand
    (hwnd == fh and, where logged, foreground true; `type` needs the last successful fg to be the farmhand)."""
    trial_bad, gap_bad = [], []
    cur = None
    fh, host = int(fh), int(host) if host else None
    for r in rows:
        op, h, u = r.get("op"), r.get("hwnd"), r.get("utc") or ""
        inside = any(a <= u <= b for a, b in trial_windows)
        if op == "fg":
            if r.get("ok"):
                cur = h
            if inside and h != host:
                trial_bad.append(r)
            continue
        if op not in INPUT_OPS:
            continue
        if inside:
            trial_bad.append(r)
            continue
        target = h if h is not None else cur
        if op == "close" or target != fh or r.get("foreground") is False:
            gap_bad.append(r)
    return dict(ok=not trial_bad and not gap_bad, trial_input_ops=trial_bad[:10], non_farmhand_ops=gap_bad[:10])


HOST_DRIVER_INPUT_OPS = {"key", "type", "hold", "click", "move"}


def _hwnd_is(value, host) -> bool:
    try:
        return value is not None and int(value) == int(host)
    except (TypeError, ValueError):
        return False


def host_driver_row(r, host) -> bool:
    """FIXB17 (review MAJOR): a reviewed HOST-driver row (ui_op with ARTI_UI_OP_HOST_DRIVER): tagged source=host_driver +
    target=host for the soak's HOST, the row's own hwnd IS the HOST (a tagged row without an hwnd fails), and an input
    op (key / type / hold / click / move) was sent while the HOST really was in front: ``foreground`` true and the
    logged foreground hwnd (``fg_hwnd``, and ``fg_hwnd_end`` when logged) is the HOST. A fg row must have succeeded."""
    if host is None or r.get("source") != "host_driver" or r.get("target") != "host":
        return False
    if not _hwnd_is(r.get("host_hwnd"), host) or not _hwnd_is(r.get("hwnd"), host):
        return False
    if r.get("op") in HOST_DRIVER_INPUT_OPS:
        return (r.get("foreground") is True and _hwnd_is(r.get("fg_hwnd"), host)
                and ("fg_hwnd_end" not in r or _hwnd_is(r.get("fg_hwnd_end"), host)))
    if r.get("op") == "fg":
        return r.get("ok") is True
    return True


def audit_window(rows, since, until=None):
    """Timestamp selection for historical replay only; live audits use committed row offsets."""
    return [r for r in rows if (since or "") <= (r.get("utc") or "")
            and (until is None or (r.get("utc") or "") < until)]


TAIL_DRAIN_READS = 10   # FIXB33 review r3: shutdown drain window = TAIL_DRAIN_READS * TAIL_DRAIN_SLEEP s (<= 2 s)
TAIL_DRAIN_SLEEP = 0.2


def audit_committed_rows(rows, offset):
    """Audit each committed row of the append-only ui_op log once, including late/identical appends."""
    if len(rows) < offset:
        raise Stop(3, "ui-op log shrank below the committed audit offset")
    return rows[offset:], len(rows)


def audit_pad(rows, host=None):
    """Pad mode: nothing may send input or foreground a window while the soak runs (the soak itself never does). FIXB17:
    the reviewed HOST-driver rows (host_driver_row) are excluded from the leak, counted and listed; anything else fails."""
    forbidden = [r for r in rows if r.get("op") in PAD_FORBIDDEN_OPS]
    driver = [r for r in forbidden if host_driver_row(r, host)]
    bad = [r for r in forbidden if not host_driver_row(r, host)]
    # FIXB25 D-SOAK-5: tagged HOST `pos` / `close` stay excluded only on the HOST hwnd (host_driver_row) and are listed
    window_ops = [r for r in driver if r.get("op") in ("pos", "close")]
    return dict(ok=not bad, input_ops=bad[:10], host_driver_ops=len(driver), host_driver_rows=driver,
                host_driver_window_ops=window_ops,
                note="pad mode: no ui_op input or fg is allowed except tagged HOST-driver rows on the HOST")


TOOLBAR_TPL = Path(HERE) / "restart_refs" / "toolbar_left.png"   # left frame edge of the HOST toolbar (12 x 82 px)
TOOLBAR_W, TOOLBAR_H = 800, 92


def locate_toolbar(rgb, where="auto", tpl_path=TOOLBAR_TPL, max_mad=0.06):
    """Find the HOST toolbar box in a client RGB array. Stardew draws it at the TOP when the player stands in the lower
    half of the screen and at the BOTTOM otherwise (Toolbar.draw: x = W/2 - 400, 800 x 96). D26 (soak1): the old check
    compared the bottom 80 px, which was world (dusk lighting) because the toolbar was at the top.
    Returns {side, box:[x0,y0,x1,y1], score, located}."""
    import numpy as np
    from PIL import Image
    tpl = np.asarray(Image.open(tpl_path).convert("RGB"), dtype=np.int16)
    th, tw = tpl.shape[:2]
    h, w = rgb.shape[:2]
    x_nom = w // 2 - 400
    ranges = {"top": range(0, 41), "bottom": range(max(0, h - 120), max(0, h - th) + 1)}
    best = None
    for side in (("top", "bottom") if where == "auto" else (where,)):
        for y in ranges[side]:
            for x in range(max(0, x_nom - 6), min(w - tw, x_nom + 6) + 1):
                sc = float(np.abs(rgb[y:y + th, x:x + tw].astype(np.int16) - tpl).mean()) / 255
                if best is None or sc < best[0]:
                    best = (sc, side, x, y)
    if best and best[0] <= max_mad:
        sc, side, x, y = best
        return dict(side=side, box=[x, max(0, y - 4), min(w, x + TOOLBAR_W), min(h, y + TOOLBAR_H - 4)],
                    score=round(sc, 4), located=True)
    side = "top" if where == "auto" else where       # sane default: top (the soak's HOST usually idles in its bed)
    y = 10 if side == "top" else h - TOOLBAR_H
    return dict(side=side, box=[x_nom, y, x_nom + TOOLBAR_W, y + TOOLBAR_H], score=round(best[0], 4) if best else None,
                located=False)


def frame_check(before_png, after_png, client, toolbar="auto"):
    """HOST frame check (only meaningful while the owner is idle on the host): the toolbar strip only (UI, unaffected
    by day/night lighting; located top or bottom, see locate_toolbar) by RGB mean-abs-diff, and the camera shift by
    phase correlation of the whole client (robust to a global lighting change). Thresholds are HYPOTHESIS."""
    import numpy as np
    from PIL import Image

    def load(p):
        a = np.asarray(Image.open(p).convert("RGB"), dtype=np.int16)
        x0, y0, x1, y1 = client
        return a[y0:y1, x0:x1]
    a, b = load(before_png), load(after_png)
    if a.shape != b.shape:
        return dict(ok=False, reason=f"shape {a.shape} != {b.shape}")
    loc = locate_toolbar(a, toolbar)
    x0, y0, x1, y1 = loc["box"]
    tb = float(np.abs(a[y0:y1, x0:x1] - b[y0:y1, x0:x1]).mean()) / 255
    ga, gb = a.mean(axis=2), b.mean(axis=2)
    fa, fb = np.fft.fft2(ga[::2, ::2] - ga[::2, ::2].mean()), np.fft.fft2(gb[::2, ::2] - gb[::2, ::2].mean())
    r = fa * np.conj(fb)
    r /= np.maximum(np.abs(r), 1e-9)
    c = np.abs(np.fft.ifft2(r))
    dy, dx = np.unravel_index(int(np.argmax(c)), c.shape)
    hh, ww = c.shape
    dy, dx = (dy - hh if dy > hh // 2 else dy) * 2, (dx - ww if dx > ww // 2 else dx) * 2
    peak = float(c.max())
    moved = max(abs(dx), abs(dy)) >= 4 and peak >= 0.05
    changed = tb > 0.02
    return dict(ok=not moved and not changed, toolbar_mad=round(tb, 4), toolbar=loc, shift=[int(dx), int(dy)],
                peak=round(peak, 4), moved=bool(moved), toolbar_changed=bool(changed))


# ============================================================ receipt (pure: rebuilt from soak.jsonl)
def build_receipt(events):
    start = next((e for e in events if e["ev"] == "start"), None) or {}
    end = next((e for e in reversed(events) if e["ev"] == "end"), None) or (events[-1] if events else {})
    t0, t1 = start.get("t", 0), end.get("t", 0)
    wall = max(0.0, t1 - t0)
    fg = [e for e in events if e["ev"] == "fg"]
    fh_sampled = 0.0
    for e, nxt in zip(fg, fg[1:] + [dict(t=t1)]):
        if e.get("fh_fg"):
            fh_sampled += max(0.0, min(nxt["t"] - e["t"], 5.0))
    ops = [e for e in events if e["ev"] == "op_interval"]
    op_seconds = sum(e.get("seconds", 0) for e in ops)
    trial_fh = [e for e in fg if e.get("phase") == "trial" and e.get("fh_fg")]
    trial_samples = [e for e in fg if e.get("phase") == "trial"]
    fh_total = fh_sampled + op_seconds
    inactive = 1 - fh_total / wall if wall else 0.0
    checks = {}
    for e in events:
        if e["ev"] == "check":
            checks.setdefault(e["kind"], []).append(e)
    trials = [e for e in events if e["ev"] == "trial_end"]
    ws = [e["bytes"] for e in events if e["ev"] == "ws" and e.get("bytes")]
    stop = next((e for e in reversed(events) if e["ev"] == "stop"), None)
    minutes = start.get("minutes", 60)
    leak_ok = all(c.get("ok") for c in checks.get("leak", []))
    host_driver_ops = sum(int((c.get("audit") or {}).get("host_driver_ops") or 0) for c in checks.get("leak", []))
    host_driver_rows = [r for c in checks.get("leak", []) for r in ((c.get("audit") or {}).get("host_driver_rows") or ())]
    host_driver_window_ops = [r for r in host_driver_rows if r.get("op") in ("pos", "close")]
    commands_uncertain = [x for c in checks.get("stuck", []) for x in ((c.get("smapi") or {}).get("commands_uncertain") or ())]
    verified = [t for t in trials if t.get("status") == "OBJECTIVE_VERIFIED" and not t.get("refused")]
    min_verified = int(start.get("min_verified", MIN_VERIFIED_DEFAULT))
    rollovers = [e for e in events if e["ev"] == "rollover"]
    pass_outs = [e for e in rollovers if e.get("new") is not None and e.get("pass_out")]
    crit = dict(wall_ok=wall >= minutes * 60, trial_time_inactive_100=not trial_fh and bool(trial_samples),
                wall_inactive_95=inactive >= 0.95,
                stuck_0=all(c.get("ok") for c in checks.get("stuck", [])) and bool(checks.get("stuck")),
                crash_0=all(c.get("ok") for c in checks.get("crash", [])) and not [e for e in events if e["ev"] == "crash_signal"],
                leak_0=leak_ok and bool(checks.get("leak")),
                menus_resolved=not (stop and stop.get("kind") == "menu"),
                # FIXB25 D-SOAK-7: trials happened and enough of them verified their goal
                trials_run=bool(trials), goals_verified=len(verified) >= min_verified,
                # FIXB25 review: a goal the runner refused (envelope / approval marker) needs attention: never a silent PASS
                goals_refused_0=not [e for e in events if e["ev"] == "goal_refused"],
                stopped_by_monitor=bool(stop and stop.get("code") in (3, 4)))
    pad = start.get("mode") == "pad"
    if pad:
        crit["pad_watch_0"] = not [e for e in events if e["ev"] == "pad_violation"]
        crit["fh_never_front"] = not [e for e in fg if e.get("fh_fg") and not e.get("owner_menu")]
        crit["no_pass_out"] = not pass_outs  # FIXB25 D-SOAK-1: every night ARTI went to bed by its own rule
    verdict = "PASS" if all(v for k, v in crit.items() if k != "stopped_by_monitor") and not crit["stopped_by_monitor"] else "NOT_PASS"
    if not trials:
        # FIXB25 D-SOAK-7: 0 trials is never PASS: INCONCLUSIVE when nothing else failed, else NOT_PASS
        others = all(v for k, v in crit.items() if k not in ("trials_run", "goals_verified", "goals_refused_0",
                                                             "stopped_by_monitor"))
        verdict = "INCONCLUSIVE" if others and not crit["stopped_by_monitor"] else "NOT_PASS"
    return dict(schema="arti_soak_receipt.v1", label=start.get("label"), verdict=verdict, criteria=crit,
                wall_seconds=round(wall, 1), minutes_target=minutes, farmhand_foreground_seconds=round(fh_total, 1),
                inactive_ratio=round(inactive, 4), operator_fg_intervals=[{k: e.get(k) for k in ("cause", "seconds", "utc")} for e in ops],
                trial_fg_samples=len(trial_samples), trial_fh_fg_samples=len(trial_fh), trials=trials,
                host_driver_ops=host_driver_ops,  # FIXB17: tagged HOST-driver ui_op rows (excluded from the leak)
                host_driver_rows=host_driver_rows,  # FIXB17 review: EVERY excluded row
                host_driver_window_ops=host_driver_window_ops,  # FIXB25 D-SOAK-5
                commands_uncertain=commands_uncertain,  # FIXB28 review: menu commands with an uncertain VERDICT
                goals_verified=len(verified), min_verified=min_verified, pass_outs=len(pass_outs),
                nights=[e for e in events if e["ev"] == "soak_night"],
                day_starts=[e for e in events if e["ev"] == "soak_day_start"],
                goals_refused=[e for e in events if e["ev"] == "goal_refused"],
                rollovers=rollovers, menus=[e for e in events if e["ev"] == "menu"],
                interventions=[e for e in events if e["ev"] == "intervention"], checks=checks,
                smapi_errors=[e for e in events if e["ev"] == "smapi_error"],
                crash_signals=[e for e in events if e["ev"] == "crash_signal"],
                working_set=dict(start=ws[0] if ws else None, end=ws[-1] if ws else None, max=max(ws) if ws else None,
                                 growth=(ws[-1] - ws[0]) if ws else None, note="no threshold yet (HYPOTHESIS until a baseline)"),
                stop=stop, start=start, label_note="tool verdict; the CT reviews before any VERIFIED LIVE claim",
                mode="pad" if pad else "v1", owner_playing=bool(start.get("owner_playing")),
                waiting_for_owner=[e for e in events if e["ev"] == "waiting_for_owner"],
                pad_violations=[e for e in events if e["ev"] == "pad_violation"],
                owner_apps=_owner_apps([e for e in events if e["ev"] == "owner_fg"]),
                goals_run=[t.get("goal") for t in [e for e in events if e["ev"] == "trial_start"] if t.get("goal")],
                goal_table=goal_table(events), mix=bool(start.get("mix")))


def goal_table(events) -> dict:
    """FIXB31: per goal kind {verified, partial, held, refused, skipped, last_reason, last_journal}."""
    table = {}
    for e in events:
        if e["ev"] not in ("soak_goal_result", "soak_goal_skipped"):
            continue
        row = table.setdefault(e.get("goal"), dict(verified=0, partial=0, held=0, deadline=0, refused=0, skipped=0,
                                                   last_reason=None, last_journal=None))
        if e["ev"] == "soak_goal_skipped":
            row["skipped"] += 1
            row["last_reason"] = e.get("reason")
        else:
            row[e.get("status")] = row.get(e.get("status"), 0) + 1
            row["last_reason"] = e.get("reason") or e.get("report_status")
            row["last_journal"] = e.get("journal") or row["last_journal"]
    return table


def _owner_apps(rows):
    counts = {}
    for e in rows:
        counts[e.get("exe") or "?"] = counts.get(e.get("exe") or "?", 0) + 1
    return counts


def receipt_md(r):
    c = r["criteria"]
    mb = lambda b: f"{b / 1e6:.0f} MB" if isinstance(b, (int, float)) else "n/a"
    lines = [f"# Soak receipt {r['label']}: {r['verdict']}", "",
             f"- wall {r['wall_seconds']:.0f} s (target {r['minutes_target']} min); farmhand foreground {r['farmhand_foreground_seconds']:.0f} s; "
             f"inactive ratio {r['inactive_ratio']:.4f}; trial foreground samples with farmhand in front: {r['trial_fh_fg_samples']}/{r['trial_fg_samples']}",
             f"- criteria: " + ", ".join(f"{k}={v}" for k, v in c.items()),
             f"- stop: {json.dumps(r['stop'], default=str) if r['stop'] else 'none (time reached)'}",
             f"- SMAPI working set start/end/max/growth: {mb(r['working_set']['start'])} / {mb(r['working_set']['end'])} / "
             f"{mb(r['working_set']['max'])} / {mb(r['working_set']['growth'])} ({r['working_set']['note']})", "",
             f"- goals verified: {r.get('goals_verified')} (PASS needs >= {r.get('min_verified')}); refused goals: "
             f"{len(r.get('goals_refused') or [])}; pass-outs: {r.get('pass_outs')}",
             "", "## Trials", "", "| label | goal | start game time | seconds limit | status | reason | phys inputs | swings | trees | wood delta | stored | store reached |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in r["trials"]:
        lines.append(f"| {t.get('label')} | {t.get('goal') or 'wood'} | {t.get('game_time')} | {t.get('seconds')} | {t.get('status')} | {t.get('reason')} | "
                     f"{t.get('physical_inputs')} | {t.get('swings')} | {t.get('trees_felled')} | {t.get('wood_delta')} | {t.get('stored')} | {t.get('store_reached')} |")
    table = r.get("goal_table") or {}
    if table:
        lines += ["", "## Goals (FIXB31: per goal kind)", "", "| goal | verified | partial | held | deadline | refused | skipped | last reason | last journal |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for g, row in table.items():
            lines.append(f"| {g} | {row['verified']} | {row['partial']} | {row['held']} | {row['deadline']} | {row['refused']} | {row['skipped']} | "
                         f"{row['last_reason']} | {row['last_journal']} |")
    lines += ["", "## Nights (FIXB25: ARTI's own bed goal)", ""] + (
        [f"- {e.get('utc')} {e.get('status')} day {json.dumps(e.get('day'), default=str)} time {e.get('time')}"
         + (f" overnight {e.get('overnight')}" if e.get("overnight") else "") + (f" reason {e.get('reason')}" if e.get("reason") else "")
         for e in r.get("nights") or []] or ["- none"])
    lines += ["", "## Day-start trials (FIXB25)", ""] + (
        [f"- {e.get('utc')} {e.get('status')} menu {e.get('menu')} goal {e.get('goal')}" for e in r.get("day_starts") or []] or ["- none"])
    lines += ["", "## Refused goals (not zero-input trials)", ""] + (
        [f"- {e.get('utc')} {e.get('goal')}: {e.get('reason')} (needed {e.get('needed')})" for e in r.get("goals_refused") or []] or ["- none"])
    rows = r.get("host_driver_rows") or []
    lines += ["", f"## HOST driver (tagged ui_op rows on the HOST, excluded from the leak): {r.get('host_driver_ops', 0)}", ""]
    if rows:
        lines += ["| utc | op | hwnd | detail | foreground |", "|---|---|---|---|---|"]
        for x in rows:
            detail = " ".join(str(x.get(k)) for k in ("key", "client", "ms", "text_len") if x.get(k) is not None)
            lines.append(f"| {x.get('utc')} | {x.get('op')} | {x.get('hwnd')} | {detail} | {x.get('foreground')} |")
    else:
        lines.append("- none")
    lines.append(f"- window ops (pos / close) among them: {len(r.get('host_driver_window_ops') or [])}")
    unc = r.get("commands_uncertain") or []
    lines += ["", f"## Menu commands with an uncertain VERDICT (released, effect unknown): {len(unc)}", ""]
    lines += [f"- {x}" for x in unc[:20]] or ["- none"]
    lines += ["", "## Operator foreground intervals (farmhand in front)", ""]
    lines += [f"- {o['utc']} {o['cause']} {o['seconds']:.1f} s" for o in r["operator_fg_intervals"]] or ["- none"]
    lines += ["", "## Rollovers", ""] + ([f"- {json.dumps({k: v for k, v in e.items() if k not in ('t',)}, default=str)}" for e in r["rollovers"]] or ["- none"])
    lines += ["", "## Menus and interventions", ""] + ([f"- {e['ev']}: {json.dumps({k: v for k, v in e.items() if k not in ('t', 'ev')}, default=str)[:300]}"
                                                       for e in r["menus"] + r["interventions"]] or ["- none"])
    lines += ["", "## Checks", ""]
    for kind, cs in sorted(r["checks"].items()):
        bad = [x for x in cs if not x.get("ok")]
        na = [x for x in cs if x.get("not_applicable")]
        lines.append(f"- {kind}: {len(cs) - len(bad)}/{len(cs)} ok" + (f"; not_applicable: {na[0]['not_applicable']}" if na else "")
                     + (f"; FAILED: {json.dumps(bad[0], default=str)[:400]}" if bad else ""))
    if r.get("mode") == "pad":
        lines += ["", f"## Pad mode (owner playing: {r.get('owner_playing')})", "",
                  f"- goals run: {', '.join(r.get('goals_run') or []) or 'none'}",
                  f"- owner foreground apps (switches per exe): {json.dumps(r.get('owner_apps'))}",
                  f"- waiting_for_owner: {len(r.get('waiting_for_owner') or [])}"]
        lines += [f"- pad violation: {json.dumps({k: v for k, v in e.items() if k not in ('t',)}, default=str)[:300]}"
                  for e in r.get("pad_violations") or []] or ["- pad violations: none"]
    lines += ["", "## SMAPI errors", ""] + ([f"- {e.get('line')}" for e in r["smapi_errors"][:20]] or ["- none"])
    lines += ["", "## Crash signals", ""] + ([f"- {json.dumps(e, default=str)[:300]}" for e in r["crash_signals"]] or ["- none"])
    lines += ["", f"Evidence label: {r['label_note']}."]
    return "\n".join(lines) + "\n"


# ============================================================ live adapter (never used by the offline tests)
class LiveAdapter:
    def __init__(self, out_dir):
        self.out = Path(out_dir) if out_dir else None
        self.smapi = wf.FileTail(paths.SMAPI_LOG)
        self._bash = None

    def now(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def read_json(self, p, retries=5):
        for _ in range(retries):
            try:
                return json.loads(ipc.read_text(Path(p), encoding="utf-8-sig"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                time.sleep(0.2)
        return None

    def read_text(self, p):
        try:
            return ipc.read_text(Path(p), encoding="utf-8-sig").strip()
        except OSError:
            return None

    def exists(self, p):
        return Path(p).exists()

    def pose(self):
        import poll_state
        self.last_pose_read = {}
        try:
            pose = poll_state.sample(3.0, diagnostics=self.last_pose_read)
            self.last_pose_read["status"] = "sampled" if pose else "no_state"
            return pose
        except OSError as exc:
            self.last_pose_read.update(status="os_error", error_type=type(exc).__name__, errno=exc.errno)
            return None

    def window(self, h):
        return wf.window(h)

    def foreground(self):
        return wf.foreground()

    def alive(self, pid):
        return wf.pid_alive(pid)

    def exe(self, pid):
        return wf.exe_name(pid)

    def working_set(self, pid):
        return wf.working_set_bytes(pid)

    def keys_down(self):
        return wf.keys_down()

    def trial_running(self):
        return wf.trial_running()

    def journal_lines(self):
        p = paths.JOURNAL_DIR / "g1_journal.jsonl"
        try:
            with open(p, "rb") as f:
                return f.read().count(b"\n")
        except OSError:
            return None

    def ui_log(self):
        """Re-verify file identity and every consumed byte before accepting append-only rows."""
        previous = getattr(self, "_ui_log_commit", None)
        self.ui_log_pending_bytes = 0

        def identity(stat):
            return stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime_ns", None)

        try:
            with open(paths.UI_LOG, "rb") as stream:
                stat = os.fstat(stream.fileno())
                data = stream.read()
                if identity(stat) != identity(os.stat(paths.UI_LOG)):
                    raise Stop(3, "leak_log_replaced_during_read")
        except FileNotFoundError:
            if previous is not None:
                raise Stop(3, "leak_log_missing_after_commit")
            return []
        except OSError as e:
            raise Stop(3, f"leak_log_unreadable: {e}") from e
        if previous is not None:
            file_id, consumed, digest = previous
            if identity(stat) != file_id:
                raise Stop(3, "leak_log_replaced")
            if len(data) < consumed:
                raise Stop(3, "leak_log_truncated_below_committed_bytes")
            if hashlib.sha256(data[:consumed]).digest() != digest:
                raise Stop(3, "leak_log_consumed_prefix_rewritten")
        committed = data.rfind(b"\n") + 1
        self.ui_log_pending_bytes = len(data) - committed
        rows = []
        try:
            for line in data[:committed].splitlines():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("ui-op row is not an object")
                rows.append(row)
        except ValueError as e:
            raise Stop(3, f"leak_log_invalid_committed_row: {e}") from e
        self._ui_log_commit = identity(stat), committed, hashlib.sha256(data[:committed]).digest()
        return rows

    def _py(self, tool, *args, timeout=900, env=None):
        p = subprocess.run([sys.executable, os.path.join(HERE, tool), *map(str, args)], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr

    def ui(self, *args):
        return self._py("ui_op.py", *args, timeout=120)

    def prep(self, *args):
        return self._py("prep_trial.py", *args, timeout=300)

    def walk(self, tx, ty, face):
        return self._py("walk_to.py", tx, ty, *(["--face", face] if face else []), timeout=600)

    def find_layout(self):
        rc, so, se = self._py("find_layout.py", "--plan-only", timeout=600)
        for line in so.splitlines():
            if line.startswith("LAYOUT "):
                return json.loads(line[7:])
        return None

    def mod_config(self):
        return paths.read_mod_config()

    def fg_app(self):
        return wf.foreground_app()

    def start_pad_watch(self, out, stop):
        log = open(Path(out).with_suffix(".console.txt"), "w", encoding="utf-8")
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "pad_watch.py"), "watch", "--out", str(out), "--quiet",
                              "--stop-file", str(stop)], stdout=log, stderr=subprocess.STDOUT)
        return dict(p=p, out=Path(out), stop=Path(stop), offset=0, log=log)

    def pad_watch_rows(self, h):
        rows = []
        try:
            with open(h["out"], "rb") as f:
                f.seek(h["offset"])
                data = f.read()
        except OSError:
            return rows
        done = data[:data.rfind(b"\n") + 1]
        h["offset"] += len(done)
        for line in done.decode("utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        if h["p"].poll() not in (None, 0) and not h.get("reported"):
            h["reported"] = True
            rows.append(dict(utc=wf.utc(), violations=[dict(kind="exited", rc=h["p"].poll())]))
        return rows

    def stop_pad_watch(self, h):
        ipc.publish(h["stop"], "stop", critical=True)
        try:
            h["p"].wait(timeout=10)
        except subprocess.TimeoutExpired:
            h["p"].terminate()                    # our own read-only monitor child, never a game process
        h["log"].close()

    def cabin_exit(self):
        bash = self._bash or _git_bash()
        env = dict(os.environ, DRY="1", NO_NUDGE="1", PYTHON=sys.executable, MODE="v1")
        p = subprocess.run([bash, os.path.join(HERE, "run_attempt.sh"), "dry", "x", "x", "x"], capture_output=True,
                           text=True, timeout=300, env=env)
        return p.returncode, p.stdout, p.stderr

    def start_run(self, k, extra=()):
        so = open(self.out / f"trial-{k}-stdout.txt", "w", encoding="utf-8")
        se = open(self.out / f"trial-{k}-stderr.txt", "w", encoding="utf-8")
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "prep_trial.py"), "run", *extra], stdout=so, stderr=se,
                             text=True)
        return dict(p=p, so=so, se=se, k=k)

    def run_poll(self, h):
        return h["p"].poll()

    def run_output(self, h):
        h["so"].close(); h["se"].close()
        k = h["k"]
        return (ipc.read_text(Path(self.out / f"trial-{k}-stdout.txt"), encoding="utf-8"),
                ipc.read_text(Path(self.out / f"trial-{k}-stderr.txt"), encoding="utf-8"))

    def rename(self, src, dst):
        os.rename(src, dst)

    def copy(self, src, dst):
        shutil.copy2(src, dst)

    def shot(self, hwnd, out):
        rc, so, se = self.ui("shot", hwnd, out)
        try:
            return json.loads(so.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    def inventory(self):
        import walk_to as W
        try:
            return W.read_observation("progress.inventory")
        except OSError:
            return None

    def levelup(self, hwnd, tag):
        import restart_farmhand as R
        out = self.out / f"fh-{tag}-{wf.stamp()}.png"
        info = self.shot(hwnd, out)
        if not info:
            return dict(path=None, levelup=False, dismissable=False, ok_center=None)
        return dict(R.find_levelup(out, info["client"], R.load_refs()), path=str(out))

    def frame_check(self, before, after, client, toolbar="auto"):
        try:
            return frame_check(before, after, client, toolbar)
        except ImportError as e:
            return dict(ok=True, not_applicable=f"numpy/Pillow missing: {e}")


def _git_bash():
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if os.path.isfile(c):
            return c
    return shutil.which("bash") or "bash"


# ============================================================ the soak
class Soak:
    def __init__(self, a, ad, out_dir):
        self.a, self.ad, self.out = a, ad, Path(out_dir) if out_dir else None
        self.k = 0
        self.need_layout = False
        self.staged = False
        self.layout = None
        self.not_free = 0
        self.zero_streak = 0
        self.cabin_tries = 0
        self.phase = "gap"
        self.trial_running = False
        self.crash = []
        self.host_gone = False
        self.hung_since = {}
        self.trial_windows = []
        self.audit_from = None
        self.audit_offset = None
        self.smapi_trial = ""
        self.smapi_partial = ""
        self.last_session = None
        self.last_journal = None
        self.next = dict(fast=0.0, medium=0.0, slow=0.0)
        self.shot_n = 0
        self.warned = set()
        self.pad = False                          # Primitives v2 pad mode: no input, no foreground, ever
        self.owner = bool(getattr(a, "owner_playing", False))
        self.goals = [g for g in (getattr(a, "goals", None) or "wood").split(",") if g]
        # FIXB31: --goals all / --mix acceptance: the acceptance rotation, preconditions, rotation never stops on a goal
        self.mix = self.goals in ([m] for m in MIX_ALIASES) or getattr(a, "mix", None) == "acceptance"
        if self.mix:
            self.goals = list(ACCEPTANCE_MIX)
        self.pass_out_seen = False
        self.goal_i = 0
        self.pad_bad = []
        self.pw = None
        self.last_app = None
        self.mod_cfg = None
        self.last_menu = None
        # FIXB25
        self.min_verified = int(getattr(a, "min_verified", MIN_VERIFIED_DEFAULT))
        self.goal_inputs = goal_inputs_map(getattr(a, "goal_inputs", "") or "")
        self.inputs_ok = bool(getattr(a, "inputs_ok", False))
        self.night_key = None
        self.night_state = None
        self.day_start_tries = {}
        self.current_goal = None
        self.last_report = None
        self.last_end_menu = None
        self.last_end_date = None
        self.last_journal_text = ""

    # ------------------------------------------------ events
    def ev(self, ev, **kw):
        e = dict(t=round(self.ad.now(), 3), utc=wf.utc(self.ad.now()), ev=ev, **kw)
        if self.out:
            ipc.append_text(self.out / "soak.jsonl", json.dumps(e, ensure_ascii=False, default=str) + "\n")
        if ev not in ("fg",):
            print(json.dumps(e, ensure_ascii=False, default=str)[:600], flush=True)
        if ev == "rollover" and kw.get("pass_out") and kw.get("new") is not None:
            self.pass_out_seen = True        # FIXB31: a global stop in the acceptance mix (next boundary)
        return e

    def events(self):
        return [json.loads(l) for l in ipc.read_text(self.out / "soak.jsonl", encoding="utf-8").splitlines() if l.strip()]

    # ------------------------------------------------ guarded input
    def ui(self, *args):
        """Every ui_op call of the soak. Pad mode never foregrounds or sends input (PLAN §6.2): a tool bug stops it."""
        if self.pad and str(args[0]) in PAD_FORBIDDEN_OPS:
            raise AssertionError(f"tool bug: ui_op {args[0]} in pad mode (v2 never foregrounds or sends input)")
        return self.ad.ui(*args)

    def pad_violation(self, kind, **kw):
        self.pad_bad.append(kind)
        self.ev("pad_violation", kind=kind, phase=self.phase, **kw)

    # ------------------------------------------------ guarded port access
    def pose(self):
        if self.trial_running:
            raise AssertionError("pose read while a trial runs (port 3017 belongs to the runtime)")
        p = self.ad.pose()
        self.last_menu = (p or {}).get("active_menu") if p else self.last_menu
        return p

    def owner_menu_window(self):
        """Pad mode: the owner may bring the farmhand to the front to clear a farmhand menu. That is the owner's own
        input, not the soak's (the soak never sends any): excused while waiting_for_owner, or in a gap whose last pose
        showed a farmhand menu. During a trial the farmhand in front is always a violation."""
        return self.phase == "waiting_for_owner" or (self.phase != "trial" and bool(self.last_menu))

    def inventory(self):
        if self.trial_running:
            raise AssertionError("inventory read while a trial runs (port 3017 belongs to the runtime)")
        return self.ad.inventory()

    def select_tool(self):
        """Select the goal's tool (by qualified id) before every trial and verify it (live B4: the Wild Plum stayed
        selected after the eat witness and three soak trials ran with use_tool_gate equipped=false, zero inputs).
        Operator path: `ui_op key <slot> <farmhand>` (foregrounds + verifies the farmhand first), then the host back in
        front; verified by progress.inventory is_active; journaled as tool_select."""
        want = self.a.tool_qid
        if not want or want == "none":
            return
        if self.pad:
            # FIXB31 D-SOAK3-1 (V2SOAK1A3 t3/t4: the Pickaxe from the `tools` trial stayed in hand, wood / chest HELD with
            # 0-2 inputs): the soak sends no key input in pad mode; the runtime's own chains select the goal's tool on
            # the pad path: the gather / episode / one-tree / store chain its Axe (stone: Pickaxe; backpack only: the
            # item_to_toolbar stage first, `gather_tool_rule`), the tool-work stage its tool (`tool_work_rule`).
            self.ev("tool_select", qid=want, skipped="pad mode: the runtime's goal chain selects its own tool; no key input")
            return
        active, target, names = inventory_slots(self.inventory(), want)
        if names is None:
            raise Stop(3, "progress.inventory observation unavailable: cannot verify the selected tool")
        if target is None:
            raise Stop(3, f"{want} is not in the inventory: the goal cannot use it")
        if active == target:
            self.ev("tool_select", qid=want, slot=target, already=True, verified=True)
            return
        if target > 9:
            raise Stop(3, f"{want} is in slot {target + 1}: no number key for it; move it to slots 1-10")
        key = str(target + 1) if target < 9 else "0"

        def run():
            rc, so, se = self.ui("key", key, self.fh)
            self.fg_host()
            return rc
        rc = self.op("tool_select", run)
        a2, _, _ = inventory_slots(self.inventory(), want)
        ok = rc == 0 and a2 == target
        self.ev("tool_select", qid=want, from_slot=active, from_item=names.get(active), slot=target, key=key, rc=rc,
                verified=ok, active_after=a2)
        if not ok:
            raise Stop(3, f"tool select {want} (key {key}) not verified: active slot {a2}")

    # ------------------------------------------------ monitors
    def monitor_tick(self):
        now = self.ad.now()
        if now >= self.next["fast"]:
            self.fast()
            self.next["fast"] = now + FAST_S
        if now >= self.next["medium"]:
            self.medium()
            self.next["medium"] = now + MEDIUM_S
        if now >= self.next["slow"]:
            self.slow()
            self.next["slow"] = now + SLOW_S

    def fast(self):
        ad = self.ad
        fg = ad.foreground()
        excused = self.pad and fg == self.fh and self.owner_menu_window()
        self.ev("fg", phase=self.phase, fg=fg, fh_fg=fg == self.fh, host_fg=fg == self.host,
                **({"owner_menu": True} if excused else {}))
        if self.pad:
            if fg == self.fh and not excused:
                self.pad_violation("farmhand_foreground", fg=fg)
            if hasattr(ad, "fg_app"):
                app = ad.fg_app() or {}
                key = (app.get("exe"), app.get("pid"))
                if key != self.last_app:
                    self.last_app = key
                    self.ev("owner_fg", exe=app.get("exe"), pid=app.get("pid"), host=fg == self.host,
                            note="exe name only (no window title)")
        for role, h, pid in (("farmhand", self.fh, self.fh_pid), ("host", self.host, self.host_pid)):
            w = ad.window(h)
            gone = not w.get("exists") or not ad.alive(pid)
            if gone:
                self.signal(f"{role}_gone", hwnd=h, pid=pid)
                if role == "host":
                    self.host_gone = True
            if w.get("hung"):
                since = self.hung_since.setdefault(role, ad.now())
                if ad.now() - since > HUNG_CRASH_S:
                    self.signal(f"{role}_hung", seconds=round(ad.now() - since, 1))
            else:
                self.hung_since.pop(role, None)
            if w.get("iconic") and f"{role}_minimized" not in self.warned:
                self.warned.add(f"{role}_minimized")
                self.ev("warning", what=f"{role} window minimized (minimize is out of scope for A4)", phase=self.phase)
        ex = ad.read_json(paths.FARMHAND_EXIT)
        if ex != self.exit_baseline:
            self.signal("farmhand_exit_record_changed", record=ex)

    def signal(self, kind, **kw):
        if kind in self.crash:          # one event per kind; the soak stops at the next boundary anyway
            return
        self.crash.append(kind)
        self.ev("crash_signal", kind=kind, phase=self.phase, **kw)

    def medium(self, collect=None):
        ad = self.ad
        s = ad.read_json(paths.COMMAND_DIR / "session.json") or {}
        brief = {k: s.get(k) for k in ("session_id", "world_ready", "input_busy", "inflight_request_id")}
        if brief != self.last_session:
            self.ev("session", **brief)
            self.last_session = brief
        text = self.smapi_partial + ad.smapi.read_new()
        lines, _, self.smapi_partial = text.rpartition("\n")
        if lines:
            lines += "\n"
            if self.out:
                with open(self.out / "smapi-slice.txt", "a", encoding="utf-8") as f:
                    f.write(lines)
            if (self.trial_running if collect is None else collect):
                self.smapi_trial += lines
            errs = smapi_errors(lines)
            for line in errs["bridge"]:
                self.ev("smapi_error", source="bridge", line=line)
                self.signal("smapi_bridge_error")
            for line in errs["other"]:
                self.ev("smapi_error", source="other", line=line)
        n = ad.journal_lines()
        if n != self.last_journal:
            self.ev("journal", lines=n)
            self.last_journal = n
        self.poll_pad_watch()

    def poll_pad_watch(self):
        if self.pw is None:
            return
        for row in self.ad.pad_watch_rows(self.pw):
            for v in row.get("violations") or ():
                self.pad_violation("pad_watch_" + v.get("kind", "?"), detail=v, utc_row=row.get("utc"))

    def slow(self):
        ad = self.ad
        self.ev("ws", pid=self.fh_pid, bytes=ad.working_set(self.fh_pid))
        if self.a.host_shot_every and self.out and (self.shot_n * SLOW_S) % self.a.host_shot_every == 0:
            info = ad.shot(self.host, self.out / f"host-{self.shot_n:03d}.png")
            self.ev("host_shot", path=(info or {}).get("out"))
        self.shot_n += 1

    def idle(self, seconds, phase):
        """Wait while monitoring (gaps / rollover). The stop file is honoured."""
        self.phase = phase
        end = self.ad.now() + seconds
        while self.ad.now() < end:
            self.check_stopfile()
            self.monitor_tick()
            self.ad.sleep(0.5)

    def check_stopfile(self):
        if self.ad.exists(paths.RUN / "SOAK_STOP"):
            raise Stop(4, "stop file RUN/SOAK_STOP")

    # ------------------------------------------------ operator actions (farmhand in front; counted)
    def op(self, cause, fn):
        t0 = self.ad.now()
        try:
            return fn()
        finally:
            self.ev("op_interval", cause=cause, seconds=round(self.ad.now() - t0, 2), t0=t0)

    def fg_host(self):
        if self.host_gone or self.pad:            # pad mode never foregrounds anything (the owner keeps the HOST)
            return
        rc, so, se = self.ui("fg", self.host)
        if '"ok": true' not in (so or ""):
            self.ev("warning", what="host foreground not confirmed", out=(so or se)[-200:])

    def wait_for_owner(self, pose, why):
        """Pad mode: a farmhand menu (LevelUp, dialogue, ...) is never clicked. Wait (monitoring, stop file honoured)
        until it clears or turns into the night menus, at most --owner-wait seconds."""
        menu = pose.get("active_menu")
        self.ev("waiting_for_owner", menu=menu, why=why, day=pose.get("date"), time=pose.get("time_of_day"))
        t0 = self.ad.now()
        while self.ad.now() - t0 < getattr(self.a, "owner_wait", 900.0):
            self.idle(OWNER_WAIT_POLL_S, "waiting_for_owner")
            p = self.pose()
            now_menu = (p or {}).get("active_menu")
            # FIXB25: a night menu ends the wait only when it is not the very menu waited on (a ShippingMenu that
            # stays open is not resolved by being a night menu)
            if p and (not now_menu or (any(m in str(now_menu) for m in NIGHT_MENUS) and now_menu != menu)):
                self.ev("owner_resolved", menu=menu, seconds=round(self.ad.now() - t0, 1),
                        now=(p or {}).get("active_menu"))
                return p
        raise Stop(3, f"menu {menu!r} still open after {getattr(self.a, 'owner_wait', 900.0):.0f}s of waiting_for_owner")

    def handle_menu(self, pose):
        menu = pose.get("active_menu")
        self.ev("menu", name=menu, day=pose.get("date"), time=pose.get("time_of_day"))
        if self.pad:
            return self.wait_for_owner(pose, "farmhand menu: pad mode never clicks (the owner or the runtime's day-start rule)")
        if "LevelUp" not in str(menu):
            raise Stop(3, f"unknown menu {menu!r}: screenshot + ask the owner (never auto-click)")
        if not self.a.dismiss_levelup:
            raise Stop(3, f"LevelUp menu: click OK yourself (or run with --dismiss-levelup) and restart the soak")
        import restart_farmhand as R
        logs = []

        def run():
            n, p = R.dismiss_levelups(self.ad, self.fh, lambda step, **kw: logs.append(dict(step=step, **kw)), first_pose=pose)
            self.fg_host()
            return n, p
        try:
            n, p = self.op("levelup_click", run)
        except R.LevelUpRefused as e:
            self.fg_host()
            raise Stop(3, f"LevelUp not dismissable: {e}")
        self.ev("intervention", kind="levelup_ok_click", clicks=n, steps=logs)
        return p

    # ------------------------------------------------ main loop
    def preflight(self):
        ad = self.ad
        launch = ad.read_json(paths.FARMHAND_LAUNCH) or {}
        self.fh_pid = launch.get("pid")
        self.fh = int(ad.read_text(paths.FH_HWND_FILE) or 0)
        self.host = int(ad.read_text(paths.HOST_HWND_FILE) or 0)
        w, hw = ad.window(self.fh), ad.window(self.host)
        self.host_pid = hw.get("pid")
        self.exit_baseline = ad.read_json(paths.FARMHAND_EXIT)
        bad = []
        if not w.get("exists") or w.get("pid") != self.fh_pid:
            bad.append(f"farmhand hwnd {self.fh} pid {w.get('pid')} != launch pid {self.fh_pid}")
        elif "[farmhand]" not in (w.get("title") or ""):
            bad.append(f"farmhand not joined: {w.get('title')!r}")
        if (ad.exe(self.fh_pid) or "").lower() != "stardewmoddingapi.exe":
            bad.append(f"pid {self.fh_pid} is not StardewModdingAPI.exe")
        if not hw.get("exists") or self.host == self.fh or self.host_pid == self.fh_pid:
            bad.append(f"host hwnd {self.host} missing or equal to the farmhand")
        running = ad.trial_running()
        if running is not False:
            bad.append("a trial runner is running" if running else "cannot list python processes (PowerShell CIM)")
        sess = ad.read_json(paths.COMMAND_DIR / "session.json") or {}
        if sess.get("world_ready") is not True:
            bad.append(f"session.json world_ready={sess.get('world_ready')!r}")
        req = getattr(self.a, "mode", "v1") or "v1"
        self.mod_cfg = ad.mod_config() if hasattr(ad, "mod_config") else None
        try:
            self.pad = paths.operator_mode(req, config=self.mod_cfg) == "pad"
        except paths.ModeUnknown as e:
            bad.append(str(e))
        if self.owner and not self.pad:
            bad.append("--owner-playing needs pad mode (deploy the v2 DLL with --preset v2-pad, or pass --mode pad)")
        if self.pad and not bad:
            r = self.pad_preflight(sess)
            if not r["ok"]:
                bad.append("pad preflight: " + "; ".join(r["reasons"]))
        if bad:
            raise Stop(2, "precondition: " + "; ".join(bad))
        return sess

    def pad_preflight(self, sess=None):
        import pad_watch as PW
        if sess is None:
            sess = self.ad.read_json(paths.COMMAND_DIR / "session.json")
        return PW.preflight(sess, self.ad.foreground() == self.fh, self.mod_cfg)

    def next_goal(self, pose):
        """(name, text) of the next goal whose time window admits the current game time; None = none admits it now.
        FIXB31 mix: also the goal's observable preconditions (`soak_goal_skipped reason=precondition:<x>`)."""
        tod = pose.get("time_of_day") or 0
        owned, known = self.owned(pose) if self.mix else (set(), True)
        for i in range(len(self.goals)):
            name = self.goals[(self.goal_i + i) % len(self.goals)]
            lo, hi = GOAL_WINDOWS.get(name, (0, 2600))
            why = None
            if not lo <= tod < hi:
                self.ev("goal_skipped", goal=name, time=tod, window=[lo, hi])
                why = "precondition:time_window"
            elif self.mix:
                why = goal_precondition(name, pose, owned, self.approval_inputs(name, GOALS[name]), inventory_known=known)
            if why is None:
                self.goal_i = (self.goal_i + i + 1) % len(self.goals)
                return name, mix_goal_text(name, pose, owned if known else None)
            if self.mix:
                self.ev("soak_goal_skipped", goal=name, reason=why, time=tod, day=pose.get("date"))
        return None

    def owned(self, pose):
        """FIXB31: (what ARTI owns, known). The pose hotbar + progress.inventory between trials; review: a failed
        inventory read is `known=False` (ownership unknown: no item precondition skips the goal)."""
        try:
            facts = self.inventory()
        except Exception:  # noqa: BLE001 - a precondition read never stops the soak
            facts = None
        known = isinstance(facts, dict) and bool(facts)
        return owned_items(pose, facts if known else None), known

    def run(self):
        a, ad = self.a, self.ad
        code = 0
        try:
            sess = self.preflight()
            self.audit_offset = len(ad.ui_log())  # pin existing rows before starting the soak
            self.t_start = ad.now()
            ad.smapi.start()
            self.audit_from = wf.utc(self.t_start)
            self.ev("start", label=a.label, minutes=a.minutes, host_idle=a.host_idle, dismiss_levelup=a.dismiss_levelup,
                    src=a.src, dll8=a.dll8, fh_pid=self.fh_pid, fh_hwnd=self.fh, host_pid=self.host_pid, host_hwnd=self.host,
                    session_id=sess.get("session_id"), goal=GOAL, mode="pad" if self.pad else "v1",
                    owner_playing=self.owner, goals=self.goals if self.pad else ["wood"], mix=self.mix,
                    min_verified=self.min_verified, goal_inputs=self.goal_inputs, inputs_ok=self.inputs_ok)
            if self.pad and self.out is not None and hasattr(ad, "start_pad_watch"):
                self.pw = ad.start_pad_watch(self.out / "pad-watch.jsonl", self.out / "PAD_WATCH_STOP")
                self.ev("pad_watch_started", out=str(self.out / "pad-watch.jsonl"))
            self.preserve_journal(f"before-{a.label}", only_if_used=True)
            first = True
            while True:
                self.check_stopfile()
                if ad.now() - self.t_start >= a.minutes * 60:
                    self.ev("time_reached", wall=round(ad.now() - self.t_start, 1))
                    break
                if self.host_gone:
                    raise Stop(3, "host gone")
                if self.crash:
                    raise Stop(3, f"crash signals between trials: {self.crash}")
                if self.pad_bad:
                    raise Stop(3, f"pad violations: {sorted(set(self.pad_bad))}")
                if self.mix and self.pass_out_seen:
                    raise Stop(3, "pass-out: ARTI was not in bed when the day rolled over (acceptance mix global stop)")
                self.phase = "gap"
                self.monitor_tick()
                pose = self.pose()
                if not pose:
                    raise Stop(3, "no pose (mod not connected to the port?)")
                if first:
                    self.inventory_flag(pose)
                    first = False
                menu = pose.get("active_menu")
                if self.pad and is_day_start_menu(menu):
                    self.day_start(pose)           # FIXB25 D-SOAK-2: ARTI's own day-start rule clears it
                    continue
                if menu and any(m in str(menu) for m in NIGHT_MENUS):
                    self.rollover(pose, f"night menu {menu}")
                    continue
                if menu:
                    self.handle_menu(pose)
                    continue
                s = (pad_trial_seconds if self.pad else trial_seconds)(pose["time_of_day"])
                energy = pose.get("energy")
                low = isinstance(energy, (int, float)) and energy < ENERGY_MIN
                if s is None or low:
                    why = "energy" if s is not None else "late"
                    if self.pad and self.night_key != day_key(pose.get("date")) and pose.get("is_player_free"):
                        self.night(pose, why)      # FIXB25 D-SOAK-1: ARTI goes to bed by its own rule
                        continue
                    self.rollover(pose, why)
                    continue
                if self.pad:                       # no Cabin walk, no Farm requirement: the goal's runtime moves ARTI
                    if not pose.get("is_player_free"):
                        self.not_free += 1
                        if self.not_free > 3:
                            raise Stop(3, "player not free with no menu for 4 reads (stuck?)")
                        self.idle(5.0, "gap")
                        continue
                    self.not_free = 0
                    goal = self.next_goal(pose)
                    if goal is None:
                        self.idle(30.0, "gap")
                        continue
                    self.trial(pose, s, goal)
                    continue
                if pose.get("location") == "Cabin":
                    self.cabin_tries += 1
                    if self.cabin_tries > 2:
                        raise Stop(3, "Cabin exit walk failed twice")
                    self.op("cabin_exit", lambda: (ad.cabin_exit(), self.fg_host()))
                    continue
                self.cabin_tries = 0
                if pose.get("location") != "Farm":
                    raise Stop(3, f"ARTI is in {pose.get('location')!r}, not on the Farm")
                if not pose.get("is_player_free"):
                    self.not_free += 1
                    if self.not_free > 3:
                        raise Stop(3, "player not free with no menu for 4 reads (stuck?)")
                    self.idle(5.0, "gap")
                    continue
                self.not_free = 0
                if not self.staged and (a.stage == "always" or (a.stage == "auto" and self.need_layout)):
                    self.stage(pose)
                    self.staged, self.need_layout = True, False
                    continue
                self.trial(pose, s)
                self.staged, self.layout = False, None
            code = 0
        except Stop as e:
            code = e.code
            kind = "menu" if "menu" in e.reason.lower() or "levelup" in e.reason.lower() else None
            self.ev("stop", code=e.code, reason=e.reason, kind=kind)
        except KeyboardInterrupt:
            code = 3
            self.ev("stop", code=3, reason="interrupted (Ctrl+C)")
        except Exception as e:                    # a tool bug can never end as PASS
            import traceback
            traceback.print_exc()
            code = 3
            self.ev("stop", code=3, reason=repr(e), kind="exception")
        finally:
            if self.pw is not None:
                try:
                    ad.stop_pad_watch(self.pw)
                    self.poll_pad_watch()
                except Exception as e:            # the receipt is still written
                    self.ev("warning", what=f"pad_watch stop failed: {e!r}")
            try:
                code = self.tail_audit(code)      # FIXB25 D-SOAK-8
            except Stop as e:
                self.ev("check", kind="leak", label="tail", ok=False, reason=str(e),
                        row_start=self.audit_offset)
                self.ev("stop", code=e.code, reason=str(e))
                if code in (0, 1):
                    code = e.code
            except Exception as e:                # noqa: BLE001 - the receipt is still written
                self.ev("check", kind="leak", label="tail", ok=False, reason=f"tail audit failed: {e!r}")
                self.ev("stop", code=3, reason=f"tail audit failed: {e!r}")
                if code in (0, 1):
                    code = 3
            if self.out and (self.out / "soak.jsonl").exists():
                self.ev("end", wall=round(ad.now() - getattr(self, "t_start", ad.now()), 1))
                r = build_receipt(self.events())
                ipc.publish(self.out / "receipt.json", json.dumps(r, indent=1, default=str), critical=True)
                ipc.publish(self.out / "SOAK_RECEIPT.md", receipt_md(r), critical=True)
                print(f"SOAK {r['verdict']} (receipt {self.out / 'SOAK_RECEIPT.md'})")
                if code == 0 and r["verdict"] != "PASS":
                    code = 1
        return code

    def tail_audit(self, code):
        """FIXB25 D-SOAK-8: the ui-op rows after the last leak check (final gap, rollover, time reached) are audited at
        shutdown like every trial's (pad: audit_pad; v1: the gap rule). A leak there fails the soak (exit 3)."""
        if self.audit_from is None or not getattr(self, "fh", None) or self.out is None:
            return code
        row_start = self.audit_offset
        committed = self.ad.ui_log()
        pending_initial = pending = getattr(self.ad, "ui_log_pending_bytes", 0)
        # FIXB33 review r3: a bounded drain of TAIL_DRAIN_READS re-reads, TAIL_DRAIN_SLEEP s apart (<= 2 s): a row whose
        # newline lands late is still audited once; no early exit on a stable byte count (scheduling margin). Unresolved
        # bytes at the end of the window are a failure. Periodic audits defer in-flight rows, shutdown cannot.
        for _ in range(TAIL_DRAIN_READS):
            if not pending:
                break
            self.ad.sleep(TAIL_DRAIN_SLEEP)
            committed = self.ad.ui_log()
            pending = getattr(self.ad, "ui_log_pending_bytes", 0)
        pending = getattr(self.ad, "ui_log_pending_bytes", 0)
        rows, row_end = audit_committed_rows(committed, row_start)
        audit = audit_pad(rows, self.host) if self.pad else audit_ui_log(rows, self.fh, self.host, [])
        reason = "leak_tail_unresolved_bytes" if pending else None
        if pending:
            audit.update(ok=False, reason=reason, pending_bytes=pending)
        ok = audit["ok"]
        self.ev("check", kind="leak", label="tail", ok=ok, audit=audit, rows=len(rows), since=self.audit_from,
                row_start=row_start, row_end=row_end, pending_bytes=pending, pending_bytes_initial=pending_initial,
                reason=reason)
        self.audit_offset = row_end
        if not ok and code in (0, 1):
            self.ev("stop", code=3, reason=reason or "leak into the host after the last trial (tail audit)")
            return 3
        return code

    def inventory_flag(self, pose):
        inv = (pose.get("inventory") or {}).get("hotbar") or []
        names = [str(s.get("name") or "") for s in inv if s]
        full = len(names) >= 12 and all(names)
        if full and not any(n in ("Wood", "Sap") or "Seed" in n for n in names):
            self.ev("warning", what="inventory 12/12 with no Wood/Sap/seed stack: drops will not be picked up", hotbar=names)

    def rollover(self, pose, why):
        """Wait (monitoring) until the next morning: free, no menu, 06:00 <= time, and the clock wrapped below the
        starting time or the date advanced (the date may tick before or after the night menus)."""
        ad = self.ad
        old = day_key(pose.get("date"))
        start_tod = pose.get("time_of_day") or 0
        t0 = ad.now()
        self.ev("rollover_wait", why=why, day=pose.get("date"), time=pose.get("time_of_day"), energy=pose.get("energy"))
        menus = []
        limit = max(0.0, seconds_until_0200(start_tod)) + ROLLOVER_SLACK_S
        if self.owner:
            self.ev("waiting_for_owner", why="night: the owner sleeps on the HOST (D13); the soak never clicks the HOST",
                    day=pose.get("date"), time=start_tod)
        while ad.now() - t0 < limit:
            if ad.now() - self.t_start >= self.a.minutes * 60:
                self.ev("rollover", old=pose.get("date"), new=None, seconds_blocked=round(ad.now() - t0, 1), menus=menus,
                        note="soak time reached while waiting")
                return
            self.idle(10.0, "rollover")
            p = self.pose()
            if not p:
                continue
            if p.get("active_menu"):
                if p["active_menu"] not in menus:
                    menus.append(p["active_menu"])
                if self.pad and is_day_start_menu(p["active_menu"]) and day_key(p.get("date")) > old:
                    # FIXB25 D-SOAK-2: the morning screen is ARTI's own day-start rule's (a --day-start trial clears it)
                    self.ev("rollover", old=pose.get("date"), new=p.get("date"), seconds_blocked=round(ad.now() - t0, 1),
                            menus=menus, why=why, day_start_menu=p["active_menu"], pass_out=self._pass_out(old))
                    return
                if any(m in str(p["active_menu"]) for m in NIGHT_MENUS):
                    continue                      # saving / shipping summary / ready check: the rollover timeout covers it
                p = self.handle_menu(p) or p
                if not p or p.get("active_menu"):
                    continue
            tod = p.get("time_of_day") or 0
            if (tod < start_tod or day_key(p.get("date")) > old) and p.get("is_player_free") and tod >= 600:
                self.ev("rollover", old=pose.get("date"), new=p.get("date"), seconds_blocked=round(ad.now() - t0, 1),
                        menus=menus, why=why, pass_out=self._pass_out(old))
                return
        raise Stop(3, f"rollover wait > {limit:.0f}s (seconds to 02:00 + {ROLLOVER_SLACK_S:.0f})")

    def _pass_out(self, old_day) -> bool:
        """FIXB25 D-SOAK-1: a night that rolled over without ARTI in bed (pad mode; v1 never sends ARTI to bed)."""
        return bool(self.pad) and not (self.night_key == old_day and self.night_state in ("slept", "in_bed"))

    def night(self, pose, why):
        """FIXB25 D-SOAK-1 (no bed goal: ARTI passed out every night at 02:00): the routine sleep goal (`tidur`) as an
        --overnight trial; ARTI's own sleep rule walks home (FIXB27 D-B24-1: the locked home leg of the sleep objective),
        answers the bed question and waits for the HOST (the HOST
        driver sleeps the HOST); the runner keeps the runtime alive until the day-start rule cleared the morning screens.
        Once per game day; a goal that did not end in bed falls back to the rollover wait (a pass-out, counted)."""
        self.night_key = day_key(pose.get("date"))
        self.night_state = "bed_goal"
        self.ev("soak_night", status="bed_goal", why=why, day=pose.get("date"), time=pose.get("time_of_day"))
        self.pad_trial(pose, BED_TRIAL_S, BED_GOAL, extra=("--overnight",))
        rep = self.last_report or {}
        on = rep.get("overnight") or {}
        end_menu = str(self.last_end_menu or "")
        slept = rep.get("status") == "OBJECTIVE_VERIFIED"
        # FIXB25 review MAJOR: a ShippingMenu / LevelUpMenu also follows a 02:00 pass-out, so it is no bed evidence. In bed =
        # the runner's own sleep evidence in the trial journal (the bed question answered / waiting for the HOST / a bed
        # finish), or a ReadyCheckDialog / SaveGameMenu at the trial's end BEFORE the date changed.
        same_day = day_key(self.last_end_date) == self.night_key if self.last_end_date else False
        in_bed = not slept and (sleep_evidence(self.last_journal_text)
                                or (same_day and any(m in end_menu for m in ("ReadyCheckDialog", "SaveGameMenu"))))
        self.night_state = "slept" if slept else "in_bed" if in_bed else "not_in_bed"
        self.ev("soak_night", status=self.night_state, day=pose.get("date"), time=pose.get("time_of_day"),
                overnight=on.get("status"), day_start=(rep.get("day_start") or {}).get("status"),
                reason=rep.get("reason"), end_menu=self.last_end_menu)
        p = self.pose()
        if p and day_key(p.get("date")) > self.night_key and self.night_state == "not_in_bed":
            # the day rolled over during a bed goal that never got ARTI into bed: a pass-out (02:00), counted
            self.ev("rollover", old=pose.get("date"), new=p.get("date"), seconds_blocked=0.0, menus=[],
                    why="bed goal ended after the night", pass_out=True)

    def day_start(self, pose):
        """FIXB25 D-SOAK-2 (a farmhand LevelUp / ShippingMenu after the night was only waited on -> stop after 900 s): a
        trial started ON the screen (`prep_trial.py run --day-start`); the runner's day-start pre-phase (ARTI's own rule)
        clears it before the next goal runs. At most DAY_START_TRIALS_MAX per game day, then the old waiting_for_owner."""
        menu = pose.get("active_menu")
        key = day_key(pose.get("date"))
        n = self.day_start_tries.get(key, 0)
        if self.ad.foreground() == self.fh:
            return self.wait_for_owner(pose, "day-start menu with the farmhand in front (the owner is clearing it)")
        if n >= DAY_START_TRIALS_MAX:
            return self.wait_for_owner(pose, f"day-start menu not cleared by ARTI's rule after {n} trials")
        self.day_start_tries[key] = n + 1
        goal = self.next_goal(pose) or ("journal", GOALS["journal"])
        secs = pad_trial_seconds(pose.get("time_of_day") or 600) or 300
        self.ev("soak_day_start", status="started", menu=menu, goal=goal[0], n=n + 1, day=pose.get("date"))
        self.pad_trial(pose, secs, goal, extra=("--day-start",))
        ds = (self.last_report or {}).get("day_start") or {}
        self.ev("soak_day_start", status="cleared" if ds.get("status") == "done" else str(ds.get("status") or "not_run"),
                menu=menu, goal=goal[0], n=n + 1, actions=ds.get("actions"), reason=ds.get("reason"))

    def approval_inputs(self, name, text):
        """FIXB25 D-SOAK-3: the approved input for this goal kind (--goal-inputs KIND=N, else --inputs-ok = the runner's own
        envelope), None = the owner default 48."""
        n = self.goal_inputs.get(name)
        if n is None and self.inputs_ok:
            n = computed_goal_inputs(text)
        return n if n is not None and n != INPUT_APPROVAL_DEFAULT else None

    def goal_refused(self, report):
        """FIXB25 D-SOAK-3: the runner refused the goal before any input (envelope / approval marker): not a zero-input
        trial; the goal kind leaves the rotation (the last one left stops the soak)."""
        name = (self.current_goal or ("wood", GOAL))[0]
        self.ev("goal_refused", goal=name, reason=report.get("reason"), needed=report.get("input_envelope_needed"))
        if name in self.goals and len(self.goals) > 1:
            self.goals = [g for g in self.goals if g != name]
            self.goal_i %= len(self.goals)
        elif name in self.goals:
            raise Stop(3, f"every goal refused by the runner (last: {name}: {report.get('reason')})")

    def stage(self, pose):
        ad = self.ad
        lay = ad.find_layout()
        for leg_no in range(1, 4):                 # D22: walk a suggested leg toward the nearest tree cluster, rerun
            if not lay or lay.get("reason") != "walk_leg" or not lay.get("walk_leg"):
                break
            leg = lay["walk_leg"]["leg"]

            def walk_leg():
                rc, so, se = ad.walk(leg[0], leg[1], None)
                self.fg_host()
                return rc
            rc = self.op("staging_leg", walk_leg)
            self.ev("staging_leg", n=leg_no, leg=leg, toward=lay["walk_leg"].get("toward_tree"), rc=rc)
            if rc != 0:
                break
            lay = ad.find_layout()
        self.ev("layout", best=(lay or {}).get("best"), ok=(lay or {}).get("ok"), reason=(lay or {}).get("reason"),
                source=(lay or {}).get("source"))
        if not lay or not lay.get("ok"):
            self.ev("warning", what="no layout found; letting ARTI's own approach try")
            return
        best = lay["best"]
        self.layout = best

        def walk():
            rc, so, se = ad.walk(best["stand"][0], best["stand"][1], best["face"])
            self.ui("key", "1", self.fh)          # Axe selected (use_tool_gate equipped_axe)
            self.fg_host()
            return rc
        rc = self.op("staging_walk", walk)
        self.ev("staged", rc=rc, stand=best["stand"], face=best["face"], chest=best.get("chest"),
                note=None if best.get("chest") else "no chest within 12 tiles: store will fail (place one with place_item.py)")

    def trial(self, pose, seconds, goal=None):
        a, ad = self.a, self.ad
        if self.pad:
            return self.pad_trial(pose, seconds, goal or ("wood", GOAL))
        self.k += 1
        label = f"{a.label}-t{self.k}"
        me = (pose["tile"]["x"], pose["tile"]["y"])
        self.select_tool()
        dx, dy = park_offset(getattr(self, "layout", None), me, (a.park_dx, a.park_dy))
        dx, dy = safe_park(pose, dx, dy)
        tile, (cx, cy) = park_client(pose, dx, dy)

        def park():
            rc, so, se = self.ui("fg", self.fh)
            if '"ok": true' not in (so or ""):      # never send mouse input unless the farmhand is verified in front
                self.fg_host()
                return False
            self.ui("move", self.fh, cx, cy)
            ad.sleep(0.4)
            self.fg_host()
            return True
        if not self.op("park", park):
            raise Stop(3, "cursor park: farmhand foreground not confirmed; no mouse input sent, no approval written")
        ad.sleep(1.0)
        before = self.host_frame("before", label) if a.host_idle else None
        text = (f"{OWNER_TEXT}; soak {a.label} trial t{self.k} (runtime {a.src}, DLL {a.dll8}), seconds {seconds}, "
                f"operator pre-positioned ARTI at ({me[0]},{me[1]}), cursor parked on tile ({tile[0]},{tile[1]})")
        rc, so, se = ad.prep("approval", "--owner-text", text, "--owner-url", OWNER_URL, "--goal", GOAL, "--seconds", seconds)
        if rc != 0:
            raise Stop(3, f"approval failed: {(se or so).strip()[-300:]}")
        self.medium(collect=False)                 # flush SMAPI bytes from before the trial
        self.smapi_trial = ""
        self.ev("trial_start", k=self.k, label=label, seconds=seconds, game_time=pose.get("time_of_day"),
                day=pose.get("date"), tile=list(me), park=[dx, dy], park_client=[cx, cy], energy=pose.get("energy"))
        out, err, t0, t1 = self.run_trial()
        self.after_trial(label, pose, seconds, t0, t1, out, err, before)

    def run_trial(self, extra=()):
        """Start the runner, monitor until it exits (it is never killed). Returns (stdout, stderr, t0, t1)."""
        ad = self.ad
        t0 = ad.now()
        self.trial_running, self.phase = True, "trial"
        h = ad.start_run(self.k, **({"extra": tuple(extra)} if extra else {}))
        try:
            while ad.run_poll(h) is None:
                self.monitor_tick()
                ad.sleep(0.5)
        finally:
            out, err = ad.run_output(h)
            self.medium(collect=True)              # the runner's last SMAPI lines belong to this trial
            self.trial_running = False
            self.phase = "gap"
        return out, err, t0, ad.now()

    def pad_trial(self, pose, seconds, goal, extra=()):
        """v2 pad-mode trial: pre-flight from session.json, no park, no tool key, nothing foregrounded."""
        a, ad = self.a, self.ad
        for attempt in range(1, 4):
            r = self.pad_preflight()
            if r["ok"]:
                break
            self.ev("pad_preflight", ok=False, attempt=attempt, reasons=r["reasons"])
            if attempt == 3:
                raise Stop(3, "pad preflight refused 3 times: " + "; ".join(r["reasons"]))
            self.idle(5.0, "gap")
        self.k += 1
        label = f"{a.label}-t{self.k}"
        me = (pose["tile"]["x"], pose["tile"]["y"])
        self.select_tool()
        name, text_goal = goal
        self.current_goal = goal
        inputs = self.approval_inputs(name, text_goal)
        text = (f"{OWNER_TEXT}; soak {a.label} trial t{self.k} (runtime {a.src}, DLL {a.dll8}), seconds {seconds}, "
                f"pad mode (v2 controller model{', owner playing the HOST' if self.owner else ''}): no cursor park, "
                f"farmhand never foregrounded, goal {name}" + (f", approved input {inputs} (operator)" if inputs else ""))
        rc, so, se = ad.prep("approval", "--owner-text", text, "--owner-url", OWNER_URL, "--goal", text_goal, "--seconds", seconds,
                             *(("--inputs", inputs) if inputs else ()))
        if rc != 0:
            raise Stop(3, f"approval failed: {(se or so).strip()[-300:]}")
        self.medium(collect=False)
        self.smapi_trial = ""
        self.ev("trial_start", k=self.k, label=label, seconds=seconds, game_time=pose.get("time_of_day"),
                day=pose.get("date"), tile=list(me), location=pose.get("location"), goal=name, mode="pad",
                energy=pose.get("energy"), pad=r["facts"], inputs=inputs, run_flags=list(extra))
        out, err, t0, t1 = self.run_trial(extra)
        self.after_trial(label, pose, seconds, t0, t1, out, err, None)

    def after_trial(self, label, pose, seconds, t0, t1, out, err, before):
        self.trial_windows.append((wf.utc(t0), wf.utc(t1)))
        if self.host_gone:
            # The running trial cannot be aborted (the runner is never killed): the soak waited for it to end at its own
            # limit, sends nothing more (no pose reads, clicks, foreground or host shots), keeps the evidence and stops.
            self.ev("trial_end", k=self.k, label=label, seconds=seconds, wall=round(t1 - t0, 1), host_gone=True)
            self.ev("check", kind="crash", label=label, ok=False, signals=list(self.crash))
            self.preserve_journal(label)
            raise Stop(3, f"host gone during {label}: no further operator action")
        self.post_trial(label, pose, seconds, t0, t1, out, err, before)

    def host_frame(self, tag, label):
        info = self.ad.shot(self.host, self.out / f"host-{label}-{tag}.png") if self.out else None
        return (str(self.out / f"host-{label}-{tag}.png"), info) if info else None

    def post_trial(self, label, pose, seconds, t0, t1, out, err, before):
        a, ad = self.a, self.ad
        report_path, report = None, None
        for line in (out or "").splitlines():
            if line.startswith('{"exit"'):
                try:
                    report_path = json.loads(line).get("report")
                except ValueError:
                    pass
        if report_path and ad.exists(report_path):
            report = ad.read_json(report_path)
            if self.out:
                ad.copy(report_path, self.out / f"report-{label}.json")
        stderr_file = report_path and str(report_path)[:-5] + "-stderr.txt"
        trial_err = (ad.read_text(stderr_file) or "") if stderr_file else ""
        self.medium(collect=True)
        stats = trial_stats(report, self.smapi_trial)
        self.last_report = report or {}
        refused = bool(report and report.get("status") == "FAILED" and str(report.get("reason")) in REFUSAL_REASONS
                       and not (stats.get("physical_inputs") or 0))
        self.ev("trial_end", k=self.k, label=label, seconds=seconds, game_time=pose.get("time_of_day"),
                wall=round(t1 - t0, 1), report=report_path,
                **({"goal": self.current_goal[0]} if self.pad and self.current_goal else {}),
                **({"refused": True} if refused else {}), **stats)
        # --- stuck input
        p1 = self.pose(); p1_at = wf.utc(ad.now())
        p1_read = dict(getattr(ad, "last_pose_read", {}) or {})
        ad.sleep(3.0)
        p2 = self.pose(); p2_at = wf.utc(ad.now())
        p2_read = dict(getattr(ad, "last_pose_read", {}) or {})
        self.medium(collect=True)                  # late TERMINAL lines, if any
        sess = ad.read_json(paths.COMMAND_DIR / "session.json") or {}
        sm = smapi_stuck(self.smapi_trial)
        menu = (p2 or {}).get("active_menu")
        self.last_end_menu = menu
        self.last_end_date = (p2 or {}).get("date")
        same = bool(p1 and p2 and p1.get("pixel") == p2.get("pixel"))
        free = bool(p2 and p2.get("is_player_free"))
        phys = stats.get("physical_inputs") or 0
        # FIXB28 D-SOAK-9 + review: the release proof per input family (menu commands by their VERDICT, background
        # inputs by a RELEASE_VERIFIED run, FIXB32 instant inputs by completed RESULT), from the trial's own journal
        families = trial_input_families(ad.read_text(paths.JOURNAL_DIR / "g1_journal.jsonl"))
        proof_why = release_proof_missing(sm, families, phys)
        proof_missing = proof_why is not None
        sm.pop("_verdict_ids", None)
        sm.pop("_instant_ids", None)
        stuck_ok = (same and (free or bool(menu)) and sess.get("input_busy") is False
                    and sess.get("inflight_request_id") is None and sm["ok"] and not proof_missing)
        self.ev("check", kind="stuck", label=label, ok=stuck_ok, pixel_same=same, free=free, menu=menu,
                poses=[dict(read_completed_utc=p1_at, read_diagnostic=p1_read, state=stuck_pose_evidence(p1)),
                       dict(read_completed_utc=p2_at, read_diagnostic=p2_read, state=stuck_pose_evidence(p2))],
                release_proof=("missing: " + proof_why) if proof_missing else "checked",
                commands_uncertain=len(sm.get("commands_uncertain") or []),
                input_busy=sess.get("input_busy"), inflight=sess.get("inflight_request_id"), smapi=sm,
                keys_down=ad.keys_down(), note="keys_down is record-only (the owner may be typing on the host)")
        # --- crash
        crash = [c for c in self.crash]
        if not report:
            crash.append("report_missing")
        if "Traceback (most recent call last)" in (err or "") or "Traceback (most recent call last)" in trial_err:
            crash.append("runner_traceback")
        self.ev("check", kind="crash", label=label, ok=not crash, signals=crash)
        # --- leak
        audit_until = wf.utc(ad.now())
        row_start = self.audit_offset
        rows, row_end = audit_committed_rows(ad.ui_log(), row_start)
        audit = audit_pad(rows, self.host) if self.pad else audit_ui_log(rows, self.fh, self.host, self.trial_windows[-1:])
        fgs = [e for e in self.events() if e["ev"] == "fg" and e.get("phase") == "trial" and t0 <= e["t"] <= t1] if self.out else []
        fh_in_trial = sum(1 for e in fgs if e.get("fh_fg"))
        host_frac = round(sum(1 for e in fgs if e.get("host_fg")) / len(fgs), 3) if fgs else None
        frame = dict(not_applicable="owner may be playing on the host (run with --host-idle for the frame check)")
        if a.host_idle and before:
            after = self.host_frame("after", label)
            if after:
                frame = ad.frame_check(before[0], after[0], before[1]["client"], a.host_toolbar)
        if self.pad:
            self.poll_pad_watch()
            frame = dict(not_applicable="pad mode: pad_watch + the foreground timeline replace the HOST frame check")
        leak_ok = audit["ok"] and fh_in_trial == 0 and frame.get("ok", True) and not self.pad_bad
        self.ev("check", kind="leak", label=label, ok=leak_ok, audit=audit, trial_fg_samples=len(fgs),
                row_start=row_start, row_end=row_end,
                pad_violations=sorted(set(self.pad_bad)) or None,
                fh_foreground_samples=fh_in_trial, host_foreground_fraction=host_frac, frame=frame,
                not_applicable=frame.get("not_applicable"))
        self.audit_from = audit_until
        self.audit_offset = row_end
        # --- menu at the boundary
        if menu or (p2 and not p2.get("is_player_free")):
            self.ev("check", kind="menu", label=label, ok=True, menu=menu, free=free,
                    note="handled at the next boundary (LevelUp OK with --dismiss-levelup, else stop)")
        if self.pad and self.current_goal and self.current_goal[0] == BED_GOAL[0]:
            self.last_journal_text = ad.read_text(paths.JOURNAL_DIR / "g1_journal.jsonl") or ""  # FIXB25 review MAJOR
        dest = self.preserve_journal(label)
        if self.pad and self.current_goal:
            # FIXB31: every goal's outcome is defect-triage data (the rotation continues after a HELD / FAILED goal)
            self.ev("soak_goal_result", goal=self.current_goal[0], label=label,
                    status=goal_result_status(report, phys, refused), report_status=(report or {}).get("status"),
                    reason=(report or {}).get("reason") or stats.get("reason"), physical_inputs=phys,
                    journal=str(dest) if dest else None)
        # --- decisions
        if not stuck_ok:
            raise Stop(3, f"stuck input after {label}: {json.dumps(dict(same=same, free=free, sess=sess.get('input_busy'), smapi_ok=sm['ok'], proof_missing=proof_missing))}")
        if crash:
            raise Stop(3, f"crash after {label}: {crash}")
        if not leak_ok:
            raise Stop(3, f"leak into the host after {label}" + (f" (pad: {sorted(set(self.pad_bad))})" if self.pad_bad else ""))
        if refused:
            self.goal_refused(report)              # FIXB25 D-SOAK-3: not a zero-input trial
            return
        self.zero_streak = self.zero_streak + 1 if phys == 0 else 0
        if self.zero_streak >= ZERO_INPUT_STREAK:
            raise Stop(3, f"{ZERO_INPUT_STREAK} consecutive trials with physical_inputs == 0")
        if self.a.stage == "auto" and (phys == 0 or NO_TARGET_RE.search(str(stats.get("reason") or ""))):
            self.need_layout = True

    def preserve_journal(self, label, only_if_used=False):
        ad = self.ad
        src = paths.JOURNAL_DIR
        if not ad.exists(src) or (only_if_used and not ad.exists(src / "g1_journal.jsonl")):
            return None
        base = paths.RUN / f"journal-{label}-{wf.stamp(ad.now())}"
        dest, i = base, 1
        while ad.exists(dest):
            i += 1
            dest = Path(f"{base}-{i}")
        for attempt in range(1, 6):
            try:
                ad.rename(src, dest)
                self.ev("journal_move", dest=str(dest), attempts=attempt)
                return dest
            except PermissionError as e:
                self.ev("journal_move_retry", attempt=attempt, error=str(e))
                ad.sleep(2.0)
        raise Stop(3, f"cannot preserve-move {src} after 5 tries (something holds it; never delete it)")


# ============================================================ CLI
def plan_first(a, ad):
    """--dry-run: read-only preflight + one pose (only when no trial runs) -> the first trial's commands."""
    s = Soak(a, ad, None)
    s.ev = lambda ev, **kw: dict(ev=ev, **kw)
    sess = s.preflight()
    if s.pad:
        pose = ad.pose()
        if not pose:
            raise Stop(2, "no pose (mod not connected?)")
        secs = trial_seconds(pose["time_of_day"])
        goal = s.next_goal(pose)
        cmds = ["pad_watch.py watch --out RUN/soak-<label>-<utc>/pad-watch.jsonl --quiet --stop-file .../PAD_WATCH_STOP",
                "pad_watch.py preflight (session.json pad fields + mod config)",
                f"prep_trial.py approval --owner-text '...soak {a.label} trial t1 pad mode...' --goal '{goal[1] if goal else '-'}' "
                f"--seconds {secs}",
                "prep_trial.py run   (monitors: fg/windows/pids + owner app 2 s; session/SMAPI/journal/pad_watch 5 s)",
                "post-checks: stuck, crash, leak (no ui_op input at all, farmhand never in front, pad_watch clean)",
                "never: ui_op fg/click/move/key/hold/type, cursor park, Cabin walk, LevelUp click"]
        print(json.dumps(dict(dry_run=True, mode="pad", owner_playing=s.owner, session=sess.get("session_id"),
                              pose=dict(location=pose.get("location"), time=pose.get("time_of_day"), day=pose.get("date"),
                                        menu=pose.get("active_menu")),
                              trial_seconds=secs, first_goal=goal[0] if goal else None, commands=cmds), indent=1, default=str))
        return 0
    pose = ad.pose()
    if not pose:
        raise Stop(2, "no pose (mod not connected?)")
    secs = trial_seconds(pose["time_of_day"])
    me = (pose["tile"]["x"], pose["tile"]["y"])
    tile, (cx, cy) = park_client(pose, a.park_dx, a.park_dy)
    cmds = []
    if pose.get("active_menu"):
        cmds.append(f"menu {pose['active_menu']!r}: " + ("LevelUp OK (banner+OK template)" if a.dismiss_levelup else "STOP"))
    if secs is None or (pose.get("energy") or 0) < ENERGY_MIN:
        cmds.append(f"rollover wait (time {pose['time_of_day']}, energy {pose.get('energy')}): pose every 10 s <= seconds to 02:00 + {ROLLOVER_SLACK_S:.0f}s")
    if pose.get("location") == "Cabin":
        cmds.append("DRY=1 NO_NUDGE=1 bash run_attempt.sh dry x x x ; ui_op.py fg HOST")
    if a.stage == "always":
        cmds.append("find_layout.py --plan-only ; walk_to.py SX SY --face F ; ui_op.py key 1 FH ; ui_op.py fg HOST")
    cmds += [f"ui_op.py fg {s.fh} ; ui_op.py move {s.fh} {cx} {cy} ; ui_op.py fg {s.host}   (park tile {tile})",
             f"prep_trial.py approval --owner-text '...soak {a.label} trial t1...' --owner-url {OWNER_URL} --goal '{GOAL}' --seconds {secs}",
             "prep_trial.py run   (monitors: fg/windows/pids 2 s; session/SMAPI/journal 5 s; working set + host shot 60 s)",
             "post-checks: 2 poses 3 s apart, session flags, SMAPI release proof, ui-op-log audit, fg timeline"
             + (", host frame check" if a.host_idle else ""),
             f"mv run/journal run/journal-{a.label}-t1-<utc>"]
    print(json.dumps(dict(dry_run=True, session=sess.get("session_id"), pose=dict(location=pose.get("location"), tile=me,
                                                                                    time=pose.get("time_of_day"), day=pose.get("date"),
                                                                                    energy=pose.get("energy"), menu=pose.get("active_menu")),
                          trial_seconds=secs, commands=cmds), indent=1, default=str))
    return 0


def cmd_run(a, adapter=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", a.label):
        print("--label: letters, digits, _ . - only", file=sys.stderr)
        return 2
    goals = getattr(a, "goals", "wood")
    owner = getattr(a, "owner_playing", False)
    if getattr(a, "mix", None) == "acceptance" or goals in MIX_ALIASES:
        goals = ",".join(ACCEPTANCE_MIX)                 # FIXB31: --goals all / --mix acceptance
    bad_goals = [g for g in (goals or "").split(",") if g and g not in GOALS]
    if bad_goals or not goals:
        print(f"--goals: comma list of {', '.join(GOALS)} (unknown: {bad_goals})", file=sys.stderr)
        return 2
    if owner and a.host_idle:
        print("--owner-playing and --host-idle exclude each other (the owner is not idle)", file=sys.stderr)
        return 2
    try:
        goal_inputs_map(getattr(a, "goal_inputs", "") or "")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if owner and getattr(a, "mode", "v1") == "v1":
        print("--owner-playing needs pad mode (v1 parks the owner's cursor)", file=sys.stderr)
        return 2
    if a.src is None:
        try:
            a.src = subprocess.check_output(["git", "-C", str(paths.WORKTREE), "rev-parse", "--short", "HEAD"], text=True).strip()
        except (OSError, subprocess.SubprocessError):
            a.src = "unknown"
    if a.dll8 is None:
        try:
            import hashlib
            a.dll8 = hashlib.sha256(ipc.read_bytes(paths.MOD / "ArtiStardewBridge.dll")).hexdigest()[:8]
        except OSError:
            a.dll8 = "unknown"
    if a.dry_run:
        ad = adapter or LiveAdapter(None)
        try:
            return plan_first(a, ad)
        except Stop as e:
            print(f"REFUSED: {e.reason}", file=sys.stderr)
            return e.code
    base = paths.RUN / f"soak-{a.label}-{wf.stamp()}"
    out, i = base, 1
    while out.exists():
        i += 1
        out = Path(f"{base}-{i}")
    out.mkdir(parents=True)
    ad = adapter or LiveAdapter(out)
    if adapter is not None and getattr(ad, "out", None) is None:
        ad.out = out
    return Soak(a, ad, out).run()


def cmd_report(a):
    d = Path(a.dir)
    events = [json.loads(l) for l in ipc.read_text(d / "soak.jsonl", encoding="utf-8").splitlines() if l.strip()]
    r = build_receipt(events)
    dest = d / "SOAK_RECEIPT.md"
    if dest.exists():
        dest = d / f"SOAK_RECEIPT-rebuilt-{wf.stamp()}.md"
    ipc.publish(dest, receipt_md(r), critical=True)
    print(json.dumps(dict(verdict=r["verdict"], criteria=r["criteria"], wall=r["wall_seconds"],
                          inactive_ratio=r["inactive_ratio"], trials=len(r["trials"]), receipt=str(dest)), indent=1))
    return 0 if r["verdict"] == "PASS" else 1


def build_parser():
    ap = argparse.ArgumentParser(prog="soak.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    r = sub.add_parser("run", help="run the soak (or --dry-run: plan the first trial, send nothing)")
    r.add_argument("--label", required=True, help="soak label, e.g. soak1 (trials are <label>-t<k>)")
    r.add_argument("--minutes", type=float, default=60.0)
    r.add_argument("--host-idle", action="store_true", help="the owner is idle on the host: enable the host frame check")
    r.add_argument("--host-toolbar", choices=("auto", "top", "bottom"), default="auto",
                   help="where the HOST toolbar is for the frame check (auto = locate it; falls back to top)")
    r.add_argument("--dismiss-levelup", action="store_true", help="click OK on a plain LevelUp (operator intervention, counted)")
    r.add_argument("--src", help="runtime short sha for the approval text (default: git -C $ARTI_M1_WORKTREE)")
    r.add_argument("--dll8", help="first 8 hex of the deployed DLL (default: hash of the installed DLL)")
    r.add_argument("--tool-qid", default="(T)Axe",
                   help="tool the goal needs, selected + verified before every trial ('none' = skip; default the Axe)")
    r.add_argument("--park-dx", type=int, default=4)
    r.add_argument("--park-dy", type=int, default=3)
    r.add_argument("--stage", choices=("auto", "always", "never"), default="auto",
                   help="layout staging: auto = only after a trial without inputs / target (default)")
    r.add_argument("--host-shot-every", type=int, default=60, help="seconds between HOST PrintWindow shots (0 = off)")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--mode", choices=paths.OPERATOR_MODES, default="auto",
                   help="auto (default: pad when the installed mod config has GamepadInputEnabled) | pad | v1")
    r.add_argument("--owner-playing", "--owner-plays", dest="owner_playing", action="store_true",
                   help="V2SOAK1: the owner actively plays the HOST (pad mode; no HOST frame check; waits for the owner)")
    r.add_argument("--goals", default="wood", help=f"pad mode: comma list rotated per trial ({', '.join(GOALS)}), "
                                                     "or `all` (FIXB31: the acceptance mix)")
    r.add_argument("--mix", choices=("acceptance",), default=None,
                   help="FIXB31: the acceptance rotation (= --goals all): preconditions skip, HELD goals continue")
    r.add_argument("--owner-wait", type=float, default=900.0,
                   help="pad mode: seconds a farmhand menu may wait for the owner / the runtime before the soak stops")
    r.add_argument("--goal-inputs", default="",
                   help="FIXB25: approved input per goal kind, e.g. combat=134 (prep_trial approval --inputs N; operator decision)")
    r.add_argument("--inputs-ok", action="store_true",
                   help="FIXB25: approve each goal kind's own envelope (combat 134 for one kill); others stay 48")
    r.add_argument("--min-verified", type=int, default=MIN_VERIFIED_DEFAULT,
                   help="FIXB25: PASS needs at least this many OBJECTIVE_VERIFIED trials (default 6)")
    r.set_defaults(f=cmd_run)
    p = sub.add_parser("report", help="rebuild the receipt from DIR/soak.jsonl")
    p.add_argument("dir")
    p.set_defaults(f=cmd_report)
    return ap


def main(argv=None, adapter=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "run":
        if not 1 <= a.minutes <= 240:
            print("--minutes must be 1..240", file=sys.stderr)
            return 2
        return cmd_run(a, adapter)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
