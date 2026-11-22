"""Turn a scoped evaluator journal into a computed bench verdict (ADR 235, ADR 242).

PASS: the situation was reached and every proof mark held. FAIL: reached, and a forbidden mark appeared or a
required one was missing at the limit. NOT REACHED: the situation never appeared (staging, setup or startup did
not put her there); the refs name the step that failed. INCONCLUSIVE: a safety stop, a broken journal or an
interruption after arming. No model writes any of these.
"""
import time
from pathlib import Path

from scripts.free_play import evaluate, soak
from scripts.free_play.expectations import Engine

PASS, FAIL, NOT_REACHED, INCONCLUSIVE, PENDING = "PASS", "FAIL", "NOT REACHED", "INCONCLUSIVE", "PENDING"
SETUP_FAILURES = ("skill_failed", "skill_step_boundary", "bench_setup_failed")


class Judge:
    def __init__(self, run, spec, clock=time.time):
        self.run = Path(run)
        self.spec = spec
        self.reader = evaluate.JournalTail(run)
        self.opportunity = Engine(spec["opportunity"])
        self.marks = Engine(spec["must"] + spec.get("must_not", []))
        self.safety = []
        self.setup_refs = []
        self.clock = clock
        self.decided_at = None

    def restored(self):
        try:
            receipt = soak.read(self.run / "restore.json")
        except (OSError, ValueError):
            return False
        return all(receipt.get(k) is True for k in ("saves_identical", "files_identical", "file_sets_identical"))

    def _consume(self):
        for row, ref in self.reader.rows(final=self._final):
            # ADR 241 B: live7_no_progress is a safety row only with outcome SAFETY_STOP (older runtimes).
            safety = row.get("ev") in ("SAFETY_STOP", "HOST_SAFETY_STOP", "native_knockout")
            if safety or row.get("outcome_class") == "SAFETY_STOP":
                # A cooperative STOP is acknowledged by soak using the scoped cycle receipt.
                if row.get("reason") != "stop:live7_stop_file":
                    self.safety.append(ref)
            if (self.opportunity.result()["status"] != "PASS" and row.get("ev") in SETUP_FAILURES
                    and (row.get("ev") != "skill_step_boundary" or row.get("outcome") == "fail")
                    and len(self.setup_refs) < 6):
                # Why the situation was not reached: the failed setup step names itself (benchrun2 03-quest-log).
                self.setup_refs.append(f"{ref} {row.get('skill') or ''} {row.get('reason') or ''}".strip())
            self.opportunity.consume(row, ref)
            self.marks.consume(row, ref)

    def result(self, *, final=False, interruption=None, armed=True, interruption_refs=()):
        self._final = final
        try:
            self._consume()
        except OSError as exc:
            self.reader.errors.append(f"journal read failed: {type(exc).__name__}")
        trigger = self.opportunity.result()
        marks = self.marks.result()
        conditions = marks["conditions"]
        failed = [c for c in conditions if c["status"] == "FAIL"]
        missing = [c["id"] for c in conditions if c["status"] == "PENDING"]
        references = [r for c in trigger["conditions"] + conditions for r in c["refs"]]
        reached = trigger["status"] == "PASS"
        if interruption and not armed and not reached:
            verdict, reason = NOT_REACHED, "not reached: " + interruption
            references.extend(interruption_refs)
        elif interruption or self.reader.errors or self.safety:
            verdict = INCONCLUSIVE
            reason = interruption or (self.reader.errors or ["native safety stop"])[0]
            references.extend(self.safety)
            references.extend(interruption_refs)
        elif not reached:
            verdict = NOT_REACHED if final else PENDING
            reason = self.spec["inconclusive"] + (" Setup failed: " + "; ".join(self.setup_refs)
                                                   if self.setup_refs else "")
            references.extend(r.split(" ")[0] for r in self.setup_refs)
        elif failed:
            verdict, reason = FAIL, failed[0]["id"] + ": " + failed[0]["reason"]
        elif not missing and (final or not self.spec.get("observe_to_limit")):
            verdict, reason = PASS, "All required marks observed in the scoped journal."
        elif final:
            verdict, reason = FAIL, "Situation observed; missing " + ", ".join(missing)
        else:
            verdict, reason = PENDING, "Waiting for the remaining proof window."
        if verdict != PENDING and self.decided_at is None:
            self.decided_at = self.clock()
        return dict(id=self.spec["id"], verdict=verdict, reason=reason, restored=self.restored(),
                    opportunity=trigger["conditions"], marks=conditions, missing=missing,
                    setup_refs=list(self.setup_refs), decided_at=self.decided_at,
                    journal_refs=list(dict.fromkeys(references)))


def summarize(check, situations):
    verdicts = [r["verdict"] for r in situations]
    complete = len(verdicts) == len(check["situations"])
    if FAIL in verdicts:
        status = FAIL
    elif complete and all(v == PASS for v in verdicts):
        status = PASS
    elif INCONCLUSIVE in verdicts or not verdicts:
        status = INCONCLUSIVE
    elif NOT_REACHED in verdicts:
        status = NOT_REACHED
    else:
        status = INCONCLUSIVE
    seconds = [r.get("seconds") for r in situations]
    return dict(id=check["id"], fix=check["fix"], question=check["question"], verdict=status,
                needs=check.get("needs", []),
                seconds=round(sum(seconds), 1) if seconds and all(type(s) in (int, float) for s in seconds) else None,
                unattempted=[s["id"] for s in check["situations"] if s["id"] not in {r["id"] for r in situations}],
                journal_refs=[f"{check['id']}/{row['id']}/{ref}" for row in situations for ref in row["journal_refs"]],
                situations=situations)


def table(results, gate_lines=()):
    """The one table the bench prints: check | verdict | seconds | journal refs, then one line per failed gate."""
    rows = ["| check | verdict | seconds | journal refs |", "|---|---|---|---|"]
    blocked = None
    for result in results:
        refs = ", ".join(result["journal_refs"][:4]) + (f" (+{len(result['journal_refs']) - 4})"
                                                        if len(result["journal_refs"]) > 4 else "")
        needs = f" - needs {'; '.join(result['needs'])}" if result.get("needs") and result["verdict"] != PASS else ""
        seconds = "" if result.get("seconds") is None else f"{result['seconds']:.0f}"
        reason = next((s["reason"] for s in result["situations"] if s["verdict"] == result["verdict"]), "")
        verdict = result["verdict"]
        left = [g for s in result["situations"] for g in s.get("left_open", [])]
        if left:
            # ADR 242 follow-up: a game the bench could not close is named in capitals; the list stops there.
            blocked = "; ".join(f"PID {g.get('pid')} ({str(g.get('role', '')).upper()}): {str(g.get('why', '')).upper()}"
                                for g in left)
            verdict += f" - GAME LEFT OPEN: {blocked}; BASELINE NOT RESTORED"
        elif not result["situations"] and result.get("unattempted"):
            verdict = "NOT RUN" + (" - A GAME WAS LEFT OPEN BEFORE IT" if blocked else "")
        rows.append(f"| {result['id']} | {verdict}{needs} | {seconds} | {refs or reason[:140]} |")
    return "\n".join(rows + [line for line in gate_lines]) + "\n"
