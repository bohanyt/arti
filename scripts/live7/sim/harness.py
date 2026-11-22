"""Drive one simulated Live7 day through the real boundary and summarize its decisions."""
from __future__ import annotations

from dataclasses import dataclass, field

from .runtime import SimRuntime
from .world import World

PASS_OUT = 2600


@dataclass
class DayResult:
    status: str
    verdict: str | None
    reason: str | None
    boundaries: list   # model boundaries: clock/location/planner/rendered/choice
    starts: list       # every started call with outcome, clock and location
    rows: list         # part_live_row journal rows (real classification)
    events: list = field(repr=False, default_factory=list)
    world: World | None = field(repr=False, default=None)
    stalled: str | None = None

    # -------------------------------------------------------------- queries used by scenario tests
    @property
    def slept(self):
        return self.verdict == "achieved" and self.reason == "live7_sleep_verified"

    @property
    def bed_time(self):
        return next((s["clock"] for s in self.starts if s["call"][0] == "tidur"
                     and (s.get("outcome") or ("",))[0] == "done"), None)

    def first_offer(self, name, *, rendered=True):
        """Clock of the first boundary whose (rendered, by default) offer contained ``name``."""
        key = "rendered" if rendered else "planner"
        for b in self.boundaries:
            if any(entry[0] == name for entry in b[key]):
                return b["clock"]
        return None

    def offered(self, b, name, *, rendered=True):
        return any(entry[0] == name for entry in b["rendered" if rendered else "planner"])

    def trips(self, since=0):
        """Completed pergi_ke legs as (from, to, clock)."""
        return [(s["location"], s["end_location"], s["clock"]) for s in self.starts
                if s["call"][0] == "pergi_ke" and s.get("outcome", ("",))[0] == "done" and s["clock"] >= since]

    def longest_shuttle(self):
        """Longest run of consecutive trips that only alternate between two places."""
        best = run = 0
        pair = None
        for a, b, _ in self.trips():
            here = frozenset((a, b))
            run = run + 1 if here == pair else 1
            pair = here
            best = max(best, run)
        return best

    def timeline(self):
        """Human-readable per-game-time lines (CLI and failure messages)."""
        lines = []
        for s in self.starts:
            out = s.get("outcome") or ("pending", "")
            lines.append("D%d %04d %-9s %-6s %s %s -> %s %s" % (
                s["day"], s["clock"], s["location"], s["by"], s["call"][0], s["call"][1] or "", out[0], out[1]))
        return lines

    def offers_table(self):
        return ["D%d %04d %-9s planner=%s | seen=%s | choice=%s" % (
            b["day"], b["clock"], b["location"], sorted({n for n, _ in b["planner"]}),
            [n for n, _ in b["rendered"]], b["choice"]) for b in self.boundaries]


def run_day(world, policy, *, config=None, max_boundaries=400, resume=None, prompt_padding="", wet_soil=True):
    """Start the real Live7 campaign on ``world`` and run until the session ends or the night passes.

    ``wet_soil=False`` replays the pre-live7-game-m2 plain scene+window merge (stale_scene worlds only)."""
    rt = SimRuntime(world, policy, prompt_padding=prompt_padding,
                    config=dict(config or {}, **({"stardew_live7_resume": resume} if resume else {})))
    rt.wet_soil = wet_soil
    session = rt._skill_session
    rt._live7_begin(session)
    stalled = None
    idle = 0
    for _ in range(max_boundaries):
        if session.status != "active":
            break
        run = session.current
        if run is not None and run.pending is None:
            rt.execute_current()
            idle = 0
        elif run is None:
            world.idle()  # waiting for a next task still lets the native clock run
            idle += 1
        if world.day == 1 and world.clock >= PASS_OUT:
            world.day, world.clock = 2, 600  # vanilla 02:00 pass-out; the real day guard judges it
        rt.process_skill_boundary()
        if idle > 200:
            stalled = "no_skill_started"
            break
    else:
        stalled = "max_boundaries"
    rows = [e for e in rt.events if e.get("ev") == "part_live_row"]
    return DayResult(session.status, session.verdict, session.reason, rt.boundaries, rt.starts, rows,
                     rt.events, world, stalled)
