"""Recovery-step1 classifier, preserved for evaluator v1. See README.md.

Outcomes a..f and precedence are intentionally unchanged from the retained classifier.
"""
import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import json
import re

MOVE = {"move_through", "approach_target", "world_key.move_cardinal"}
WORLD = {"world_interact", "world_key.use_tool"}
NATIVE_NOT_TAKEN = {"native_no_input_refusal_reconciled", "released_before_latch_reconciled"}
PY_FAULTS_A = {"invalid_model_choice", "question_chooser_no_pick"}
RES_RE = re.compile(r"arti_command RESULT request_id=(\S+) status=(\S+) accepted=\S+ success=\S+ outcome=(\S+) reject_reason=(\S+)(?: arm_status=(\S+))?")
DIT_RE = re.compile(r"arti_command DITOLAK request_id=(\S+) reason=(\S+)(?: code=(\S+))?(?: detail=(\S+))?")
DIT2_RE = re.compile(r"arti_command ditolak: request_id=(\S+) reason=(\S+)")


def native_index(path):
    idx = {}
    for line in ipc.read_text(path, encoding="utf-8", errors="replace").splitlines():
        m = DIT_RE.search(line)
        if m:
            idx[m[1]] = f"{m[2]}:{m[3] or '-'}:{m[4] or '-'}"; continue
        m = DIT2_RE.search(line)
        if m:
            idx[m[1]] = m[2]; continue
        m = RES_RE.search(line)
        if m and m[2] == "rejected" and m[1] not in idx:
            idx[m[1]] = f"{m[4]}:{m[5] or '-'}"
    return idx


def windows(c1):
    for j in sorted(c1.glob("day-*/journal/g1_journal.jsonl")):
        day = j.parent.parent.name[5:8]; cur = None
        for i, line in enumerate(ipc.read_text(j, encoding="utf-8", errors="replace").splitlines(), 1):
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("ev") == "trial_decision":
                if cur: yield cur
                cur = {"ref": f"d{int(day)}:J{i}", "rows": []}
            elif cur is not None:
                cur["rows"].append((i, r))
        if cur: yield cur


def classify(w, nidx):
    cls = None; outs = set(); codes = []; fault = False; model = False; detail = []; effect = False; soft = False
    for i, r in w["rows"]:
        e = r.get("ev")
        if e == "skill_model_decision":
            model = True; t = r.get("trigger")
            ch = r.get("choice")
            cls = {"live7_next_skill": "skill", "live7_gift_question": "question"}.get(t, "menu")
            if ch == "invalid": outs.add("a")
        elif e == "skill_step":
            op = r.get("operation"); k = r.get("kind")
            if cls is None or cls == "observe":
                cls = ("move" if op in MOVE else "world" if op in WORLD else "slot" if op == "select_item"
                       else "observe" if k in ("inspect", "finish") else cls or "observe")
        elif e in ("tool_work_effect", "extras_effect", "interaction_effect"):
            effect = True
        elif e == "skill_choice_rejected":
            outs.add("a"); codes.append("a:" + str(r.get("code")))
        elif e == "live7_gift_answer" and r.get("pick") == "invalid":
            outs.add("a"); codes.append("a:gift_no_pick"); cls = cls or "question"
        elif e == "play_decision_refused":
            outs.add("b"); codes.append("b:" + str(r.get("reason")))
        elif e == "skill_step_boundary" and str(r.get("reason", "")).endswith("_not_offered"):
            outs.add("b"); codes.append("b:" + r["reason"])
        elif e == "action_result":
            a = r.get("action") or {}; lc = a.get("lifecycle"); rs = str(a.get("reason")); ws = a.get("world_effect_status")
            oc = str(a.get("outcome") or "")
            if "Transfer" in oc or "Transfer" in rs: cls = "transfer"
            elif oc in ("QuestionOpened", "MenuOpened", "ContainerOpened", "DialogueOpened") and cls in (None, "observe"): cls = "world"
            if lc == "not_taken":
                if rs in NATIVE_NOT_TAKEN:
                    outs.add("c"); codes.append("c:" + nidx.get(a.get("request_id"), rs))
                else:
                    outs.add("b"); codes.append("b:" + rs.split(":")[0])
            elif lc == "rejected":
                outs.add("c"); codes.append("c:" + nidx.get(a.get("request_id"), rs))
            elif lc == "completed" and oc == "InputCompleted" and ws == "unchanged":
                soft = True
            elif lc in ("uncertain", "interrupted") or ws in ("unchanged", "uncertain", "zero_effect") or "NOT_COMPLETED" in oc:
                outs.add("d"); codes.append("d:" + (oc or rs)[:40])
            else:
                outs.add("e")
        elif e == "ui_press" and r.get("phase") == "result":
            if cls in (None, "observe"): cls = "menu"
            st = r.get("status"); oc = r.get("outcome")
            if st == "not_taken": outs.add("b"); codes.append("b:ui_" + str(r.get("reason")))
            elif st == "rejected": outs.add("c"); codes.append("c:" + nidx.get(r.get("request_id"), str(r.get("reason"))))
            elif oc == "UiPressNoEffect": outs.add("d"); codes.append("d:UiPressNoEffect")
            elif r.get("verified"): outs.add("e")
            else: outs.add("d"); codes.append("d:ui_unverified")
        elif e == "live7_ui_choice":
            p = str(r.get("pick"))
            if p.startswith("transfer"): cls = "transfer"
            elif cls in (None, "observe"): cls = "menu"
            if not r.get("verified") and "debt_active" in str(r.get("reason")):
                outs.add("c"); codes.append("c:" + str(r.get("reason")))
            elif not r.get("verified") and str(r.get("reason")) in ("ui_pad_target_changed", "session_precondition:slot_busy"):
                outs.add("b"); codes.append("b:" + str(r.get("reason")))
        elif e == "ui_key_result":
            if cls in (None, "observe", "skill"): cls = "question"
            outs.add("e" if r.get("verified") else "d")
        elif e == "container_exit" and r.get("phase") == "result":
            cls = cls if cls not in (None, "observe") else "menu"; outs.add("e" if r.get("verified") else "d")
        elif e == "FAULT":
            cause = str(r.get("cause"))
            if cause in PY_FAULTS_A: outs.add("a"); codes.append("a:" + cause)
            else: fault = True; detail.append(cause)
            if "latch" in cause or cause == "TransferUncertain": outs.add("d")
        elif e in ("live7_recovery_loop", "live7_no_progress", "SAFETY_STOP"):
            fault = True; detail.append(e)
    cls = cls or "observe"
    if soft:
        if effect: outs.add("e")
        else: outs.add("d"); codes.append("d:InputCompleted_no_effect_row")
    for o in ("a", "c", "b", "d"):
        if o in outs:
            return cls, o, codes, fault, model
    if "e" in outs or (not fault):
        return cls, "e", codes, fault, model
    return cls, "f", codes + [f"f:{x}" for x in detail], fault, model
