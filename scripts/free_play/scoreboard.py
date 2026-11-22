"""Regenerate the machine scoreboard from EVALUATION.json files of any evaluator version.

This file never regrades: every number is printed as recorded in the run's own EVALUATION.json. Evaluations keep the
version that produced them; a run evaluated by an older evaluator is listed with that version under the goal-gate table
and in Explicit inputs, so a count of MET criteria is never compared silently across versions.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.free_play.evaluate import EVALUATOR_VERSION, SCHEMA, rank, read
from scripts.free_play.decision_visibility import scoreboard_line
from scripts.free_play.invented_rule import INVENTED_TURN_RATE_BELOW, counts_text


OLD_VERSION_NOTE = "evaluated by {version}"
# ADR 103: an evaluation of any version loads; a section its evaluator did not write is shown as absent, never regraded.
ABSENT = "absent"
GATE_COLUMNS = ('active_minutes', 'skill_level_gain', 'quest_or_letter_completed', 'bundle_item_or_donation',
                'own_money_earned', 'invented_facts')
# ADR 100: before 1.6.0 the invented-facts zero rested on self-declared coverage and no named reviewer.
PRE_160_NOTE = ("; Invented =0 under evaluators before 1.6.0 rests on self-declared claim coverage "
                "and an unnamed reviewer, not comparable with a 1.6.0 MET")
# ADR 117: before 1.7.0 any contradicted claim was NOT_MET; the row keeps its recorded number and status.
PRE_170_NOTE = ("; its invented-facts criterion was zero contradicted claims, not the 1.7.0 bar, "
                "so read MET counts across versions with that in mind")


def _version_tuple(version):
    try:
        return tuple(int(x) for x in str(version).split('.'))
    except ValueError:
        return ()


def version_note(version):
    """One note for a run graded by another evaluator version; each clause only for versions older than it."""
    note = OLD_VERSION_NOTE.format(version=version)
    if _version_tuple(version) < (1, 6, 0):
        note += PRE_160_NOTE
    if _version_tuple(version) < (1, 7, 0):
        note += PRE_170_NOTE
    return note + "."


def load_evaluations(roots):
    found = {}
    for root in roots:
        root = Path(root)
        paths = [root] if root.is_file() else sorted(root.rglob("EVALUATION.json"))
        for path in paths:
            if (path.parent / "WITNESS.json").exists():
                continue  # ADR 132: a witness run is never scored, whatever file sits in its folder
            item = read(path)
            version = item.get("evaluator_version")
            if item.get("schema") != SCHEMA or not isinstance(version, str) or not version:
                raise ValueError(f"Incompatible evaluation: {path.name}; need schema {SCHEMA} and an evaluator_version")
            if item['run'] in found and found[item['run']] != item:
                raise ValueError(f"Conflicting duplicate run: {item['run']}")
            found[item['run']] = item
    return sorted(found.values(), key=lambda e: e['run'])


def display(value, precision=1):
    return "unknown" if value is None else f"{value:.{precision}f}"


def _sec(e, *keys):
    """The nested section at `keys`, or None when any level is missing or not a dict (an older evaluator's file)."""
    node = e
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _field(section, key, fmt=None):
    """One cell: absent when the whole section is missing; otherwise the recorded value, printed as before."""
    if section is None:
        return ABSENT
    value = section.get(key)
    return fmt(value) if fmt else f"{value}"


def _pair(section, a, b):
    return ABSENT if section is None else f"{_field(section, a)} / {_field(section, b)}"


def write_scoreboard(evaluations, output):
    rank(evaluations)
    lines = ["# Free-play scoreboard (automatic)", "", f"Machine-written by evaluator **{EVALUATOR_VERSION}**. Regenerate with `python -m scripts.free_play.scoreboard <run-root>`. The evaluator also regenerates this table after every invocation.",
             *session_metrics_table(evaluations),
             "", "Best is maximum ARMED-to-stop endurance within each path type in this cohort, including ties. It is not a PM-1 or streaming-readiness pass. Explicit overrides never affect ranking. Active time is the documented terminal-stall proxy. Unknown evidence stays unknown. "
             "Stop class names the stopping rule as recorded by that run's evaluator: `host_resource` is the machine's disk stopping her (ADR 109, since 1.6.2) or a machine-wide stall behind a no-progress stop (ADR 128, since 1.7.4; 1.7.6 adds the FIX75C goal-lifetime line only), "
             "`harness` the HOST game, its telemetry, the save barrier or bridge readiness, `owner-hard-stop` the owner's rules, `budget` the planned duration, `primitive` everything else.",
             "", "| Run | Path / persona | Minutes | Active | Days | Bed / pass-out | Saved | PM-1 verified | Act disagreement | Why | Reflections / day changes | Pages / render max B | Stop class | Best |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for e in sorted(evaluations, key=lambda e: e['run']):
        i, d, p, m, acts = (_sec(e, k) for k in ('identity', 'endurance', 'pm1', 'mind', 'agreement'))
        acts = _sec(acts or {}, 'acts') if acts is not None else None
        lines.append(f"| {e['run']} | {_pair(i, 'path', 'persona_mode')} | {_field(d, 'minutes_played', display)} | "
                     f"{_field(d, 'active_minutes', display)} | {_field(d, 'game_days')} | {_pair(d, 'nights_bed', 'nights_pass_out')} | "
                     f"{_field(d, 'saved_nights')} | {ABSENT if p is None else _field(p, 'verified_primitives') + '/16'} | "
                     f"{ABSENT if acts is None else _field(acts, 'disagreement_pct', display) + '%'} | "
                     f"{ABSENT if m is None else _field(m, 'why_count') + '/' + _field(m, 'decisions')} | "
                     f"{ABSENT if m is None else _field(m, 'reflections') + '/' + _field(m, 'day_changes')} | {_pair(m, 'paging', 'render_max')} | "
                     f"{_field(d, 'stop_class')} | {'yes' if _sec(e, 'verdict', 'best_so_far') else ''} |")
    lines += journey_table(evaluations)
    lines += ["", "## Goal gate (all criteria required)", "",
              f"Native measurements and exact-reply semantic review, evaluator {EVALUATOR_VERSION}. UNKNOWN is missing evidence; "
              "positive event counts may be lower bounds. Active minutes remain the documented proxy. "
              "Native zero coverage spans gameplay, excluding harness shutdown. V2 invented counts contradicted claims; "
              f"since 1.7.0 MET needs 0 major and contradicted claims in under {INVENTED_TURN_RATE_BELOW:.0%} of reply turns (ADR 117), before it 0 contradicted; "
              "legacy reviews count contradicted turns. Unverifiable claims, quest flags, reward/stat gains, and mail read counts "
              "are separate in EVALUATION.json. Runs keep the evaluator version that graded them and are never regraded here; "
              "a run from another version is listed with its own version below the table.", "",
              f"| Run | Gate | Active >=180m | Skill +1 | Quest/letter +1 | Bundle item/donation +1 | Own money | Invented: 0 major, <{INVENTED_TURN_RATE_BELOW:.0%} turns (before 1.7.0: =0) |",
              "|---|---|---|---|---|---|---|---|"]
    for e in sorted(evaluations, key=lambda e: e['run']):
        gate = _sec(e, 'goal_gate')
        criteria = _sec(gate, 'criteria') if gate is not None else None
        cells = [f"{c['status']} ({_short(c.get('value'))})" if isinstance(c := (criteria or {}).get(name), dict) else ABSENT
                 for name in GATE_COLUMNS]
        lines.append('| ' + ' | '.join([e['run'], _field(gate, 'status'), *cells]) + ' |')
    lines += ["", "### Machine stall (information, not a criterion)", "",
              "Since 1.7.5 (ADR 130) frozen minutes leave Active above; the stall itself is shown here and decides nothing: "
              "the owner (2026-11-07) waits a stall out, so no length of stall voids a session or blocks a tag. "
              "Stalled = minutes of HOST heartbeat silence (>= 10 s) or runtime stall rows, merged. Earlier evaluators recorded none.", "",
              "| Run | Stalled min | Longest stall s | Charged in stop window s | Stop class |", "|---|---:|---:|---:|---|"]
    for e in sorted(evaluations, key=lambda e: e['run']):
        d = _sec(e, 'endurance')
        charged = _sec(d, 'stop_window_stall') if d is not None else None
        lines.append(f"| {e['run']} | {ABSENT if d is None or 'machine_stalled_minutes' not in d else display(d.get('machine_stalled_minutes'))} | "
                     f"{ABSENT if d is None or 'longest_machine_stall_s' not in d else display(d.get('longest_machine_stall_s'), 0)} | "
                     f"{ABSENT if charged is None else display(charged.get('charged_stall_s'), 0)} | {_field(d, 'stop_class')} |")
    notes = [f"- {e['run']}: " + version_note(e['evaluator_version'])
             for e in sorted(evaluations, key=lambda e: e['run']) if e['evaluator_version'] != EVALUATOR_VERSION]
    if any(_sec(e, 'goal_gate') is None for e in evaluations):
        notes.append(f"- `{ABSENT}`: that run's evaluator wrote no such section (goal gate since 1.3.4, journey since 1.2.0, "
                     "experience gate since 1.4.0, decision visibility since 1.5.0); it is shown as recorded, never regraded.")
    if notes:
        lines += [""] + notes
    lines += experience_table(evaluations)
    lines += visibility_section(evaluations)
    lines += ["", "## Explicit inputs", ""]
    for e in sorted(evaluations, key=lambda e: e['run']):
        lines.append(f"- {e['run']}: source `{_field(_sec(e, 'identity'), 'source_sha')}`; receipt `{_field(_sec(e, 'inputs'), 'legacy_receipt')}`; "
                     f"override `{_field(_sec(e, 'verdict'), 'override')}`; evaluator `{e['evaluator_version']}`.")
    output = Path(output)
    if output.name.casefold() == "pm1_scoreboard.md":
        raise ValueError("PM1_SCOREBOARD.md is maintained by CT; choose SCOREBOARD_AUTO.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding='utf-8')


def experience_table(evaluations):
    """1.4.0 experience gate (ADR 75): a separate, report-only section. Never a ranking input and never a goal_gate column."""
    lines = ["", "## Experience gate (provisional, report-only)", "",
             "Automatic transcript metrics from the live experience gate design (LX-5/9/10/11/12; LX-1/6 from the semantic review). "
             "Judge- and owner-scored criteria are UNKNOWN until those scorers run, so the provisional status is UNKNOWN at best. "
             "Starting thresholds, owner choices pending; never a ranking, tagging or goal-gate input. Details per criterion in EVALUATION.json/MD.", "",
             "| Run | Provisional | Automatic | LX-1 invented | LX-5 dropped | LX-6 grounded | LX-9 moods | LX-10 repetition | LX-11 statue | LX-12 interrupt |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for e in sorted(evaluations, key=lambda e: e['run']):
        g = _sec(e, 'experience_gate')
        if not g:
            lines.append(f"| {e['run']} | " + " | ".join([ABSENT] * 9) + " |")
            continue
        criteria = _sec(g, 'criteria') or {}
        cells = [f"{c['status']} ({_short(c.get('value'))})" if isinstance(c := criteria.get(k), dict) else ABSENT
                 for k in ('LX-1', 'LX-5', 'LX-6', 'LX-9', 'LX-10', 'LX-11', 'LX-12')]
        lines.append('| ' + ' | '.join([e['run'], _field(g, 'status'), _field(g, 'automatic_status'), *cells]) + ' |')
    return lines


def visibility_section(evaluations):
    """1.5.0 decision visibility (ADR 91): one line per run in its own section. Never a ranking input and never a
    goal_gate column: what she saw on the page she decided from, against what the journal offered."""
    lines = ["", "## Decision visibility (report-only)", "",
             "What ARTI saw on the page she decided from, from the archived provider requests (objects store), against the "
             "journal's offers. UNKNOWN when a run has no archived requests or no provider reservations. Never a ranking, "
             "tagging or goal-gate input; per destination, skill, fact and decision in EVALUATION.json/MD.", ""]
    lines += [scoreboard_line(e) for e in sorted(evaluations, key=lambda e: e["run"])]
    return lines


def _short(value):
    if isinstance(value, dict) and 'contradicted' in value:
        return counts_text(value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_cell(v)}" for k, v in value.items())
    return _cell(value)


def _val(field):
    return None if not field else field.get("value")


def _cell(value, suffix=""):
    return "unknown" if value is None else f"{value}{suffix}"


def metric_cell(value):
    """ADR 231 part F: a number with four decimals; a count by class (``mines.fight_outcomes_by_class``, attempts 85,
    86 and 87) as its own ``class=count`` pairs in key order (``none`` when empty); any other value as its text.
    Attempts 85-87 raised ``unsupported format string passed to dict.__format__`` here after writing EVALUATION."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.4f}"
    if isinstance(value, dict):
        return "; ".join(f"{k}={value[k]}" for k in sorted(value, key=str)) or "none"
    return str(value)


def session_metrics_table(evaluations):
    from scripts.free_play.session_metrics import METRICS
    columns = [(group, key) for group, keys in METRICS.items() for key in keys]
    lines = ['', '## Session metrics beside the goal gate', '',
             'Measured each session; targets disabled by default. Shares are fractions. Social rates use observed game dates (including partial days). '
             'Variety is time weighted with coverage in EVALUATION.json; `*` is a lower bound. Older evaluations show absent.', '',
             '| Run | Goal gate | Targets enabled | ' + ' | '.join(k for _, k in columns) + ' |',
             '|---|---|---|' + '---:|' * len(columns)]
    for e in sorted(evaluations, key=lambda e: e['run']):
        metrics = e.get('session_metrics')
        cells = []
        for group, key in columns:
            field = (metrics or {}).get(group, {}).get(key) or {}
            value = field.get('value')
            cells.append(ABSENT if metrics is None else 'UNKNOWN' if value is None else
                         metric_cell(value) + ('*' if field.get('lower_bound') else ''))
        gate = e.get('goal_gate') or {}
        enabled = (gate.get('measured_criteria') or {}).get('enabled', False)
        lines.append('| ' + ' | '.join([e['run'], gate.get('status', ABSENT), str(enabled), *cells]) + ' |')
    return lines


def journey_table(evaluations):
    """1.2.0 journey columns (game's own yardsticks). Separate from endurance; never ranks runs."""
    lines = ["", "## Journey (game's own yardsticks)", "",
             "Summary only, never a pass/fail and never a ranking input. Sources per field are in each EVALUATION.json "
             "(snapshot, save_diff = staged template vs last saved night, journal, unknown). Δearned = ARTI's individual "
             "earnings (team totalMoneyEarned in EVALUATION.json). Grandpa = Utility.getGrandpaScore criteria at the last "
             "saved night. Persona = owner up/down ratings in PERSONA_SAMPLES.md.", "",
             "| Run | Δearned g | Harvested | New places | Skill lv+ | Tools+ | Bundles done/total | Quests done | Goals done/set | Progress % | Loop % | Repeat-fail % | Grandpa pts/21 | Journey pts | Persona rated |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for e in sorted(evaluations, key=lambda e: e['run']):
        j = _sec(e, "journey")
        if not j:
            lines.append(f"| {e['run']} | " + " | ".join([ABSENT] * 14) + " |")
            continue
        p, u, f, per = (_sec(j, k) or {} for k in ("progress", "understanding", "finishing", "persona"))
        earned = _val(p.get("money_earned_individual"))
        earned = _val(p.get("money_earned")) if earned is None else earned
        skills = _val(p.get("skill_levels"))
        samples = per.get("samples") if isinstance(per.get("samples"), list) else None
        lines.append("| " + " | ".join([
            e['run'], _cell(earned), _cell(_val(p.get("crops_harvested"))), _cell(_val(p.get("new_places"))),
            _cell(sum(max(0, v) for v in skills.values()) if skills else None), _cell(_val(p.get("tool_upgrades"))),
            f"{_cell(f.get('bundles_done'))}/{_cell(f.get('bundles_total'))}", _cell(_val(p.get("quests_completed"))),
            f"{_cell(u.get('goals_done_with_evidence'))}/{_cell(u.get('goals_set'))}", _cell(u.get("progress_choice_pct")),
            _cell(u.get("loop_proxy_pct")), _cell(u.get("repeat_failure_pct")), _cell(f.get("grandpa_points")),
            f"{_cell(j.get('journey_points'))}{'*' if j.get('partial') else ''}",
            f"{_cell(per.get('rated'))}/{_cell(len(samples) if samples is not None else None)}"]) + " |")
    lines += ["", "`*` = partial: at least one component unknown (counted as 0); see EVALUATION.json `journey.partial_missing`."]
    return lines


def _md(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def write_persona_samples(evaluations, output):
    """Aggregated persona excerpts for the owner's 👍/👎. Existing ratings are kept, bound to reply_sha256."""
    output = Path(output)
    kept = {}
    if output.is_file():
        for line in output.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and len(cells[-1]) == 12 and all(ch in "0123456789abcdef" for ch in cells[-1]):
                kept[cells[-1]] = cells[-2]
    lines = ["# Persona samples (owner rates 👍/👎)", "",
             f"Machine-written by evaluator **{EVALUATOR_VERSION}**: 10 transcript excerpts per session, spread over the "
             "session (owner probes, discoveries, status turns). Viewer names and @handles are scrubbed; viewer-chat turns "
             "are excluded. Put 👍 or 👎 in the owner column; regeneration keeps a rating while its reply hash (last column, "
             "first 12 hex of reply_sha256) is unchanged. The streamer-heart measure a machine cannot judge.", ""]
    for e in sorted(evaluations, key=lambda e: e['run']):
        samples = ((e.get("journey") or {}).get("persona") or {}).get("samples") or []
        lines += [f"## {e['run']}", ""]
        if not samples:
            lines += ["No transcript turns with a reply.", ""]
            continue
        lines += ["| Turn | Game time | Source | Context | Reply | 👍/👎 (owner) | Hash |", "|---:|---|---|---|---|---|---|"]
        for s in samples:
            short = s["reply_sha256"][:12]
            when = " ".join(str(x) for x in (s.get("date"), s.get("clock")) if x is not None)
            lines.append(f"| {s.get('turn_n')} | {_md(when)} | {_md(s.get('source'))} | {_md(s.get('context'))} | "
                         f"{_md(s.get('reply'))} | {kept.get(short, '')} | {short} |")
        lines.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, default=ROOT / 'runbooks/SCOREBOARD_AUTO.md')
    args = parser.parse_args(argv)
    evaluations = load_evaluations(args.roots)
    write_scoreboard(evaluations, args.output)
    write_persona_samples(evaluations, Path(args.output).parent / "PERSONA_SAMPLES.md")


if __name__ == '__main__':
    main()
