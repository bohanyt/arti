"""Offline ADR 216 replay. Invented fixtures only in tests; never calls a model.

Replay: --runs DIR [DIR ...] --days A-B --scope SCOPE --out SCRATCH
Apply separately: --apply --out SCRATCH --target MIND
Apply creates and verifies its own dated sibling backup before appending only
missing stream envelopes to discoveries.jsonl. Reports stay inside scratch.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime import stream_notes as S

HISTORY = re.compile(r"^(\d\d:\d\d:\d\d(?:\.\d+)?) .*?\[History Recorded\] \[[^]]+\] \[([^]]+)\] (.*)$")
_ISSUED_BACKUPS = {}


def _safe_path(path, *, target=False):
    path = Path(path).resolve()
    parts = {p.casefold() for p in path.parts}
    if "mabar" in parts and not target:
        raise ValueError("scratch/report may not be under mabar")
    if parts & {"saves", "save", "savegame", "savegameinfo"} or any((p / "SaveGameInfo").exists() for p in (path, *path.parents)):
        raise ValueError("writes into the save are forbidden")
    if path == ROOT or path.is_relative_to(ROOT):
        raise ValueError("scratch/target/report must be outside the repository")
    return path


def _forbidden(value):
    if isinstance(value, str):
        if "2026-11-15" in value or "20261115" in value or "spring16" in value:
            raise ValueError("the 2026-11-15 stream is excluded")
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if dt.tzinfo and dt.astimezone(S.WIB).strftime("%Y-%m-%d") == "2026-11-15":
                raise ValueError("the 2026-11-15 stream is excluded")
    elif isinstance(value, dict):
        # Inspect recorded date/time metadata, never scan arbitrary dialogue for dates.
        for key, val in value.items():
            if key in {"utc", "wib", "date", "at", "ts", "timestamp", "utc_ms", "arti_before_utc", "arti_after_utc"}:
                _forbidden(val)
                if isinstance(val, (float, int)) and val > 1_000_000_000:
                    at = val / 1000 if key == "utc_ms" else val
                    try:
                        recorded = S.stamp(at)
                    except (ValueError, OverflowError, OSError):
                        continue
                    _forbidden(recorded)
            elif isinstance(val, (dict, list)):
                _forbidden(val)
    elif isinstance(value, list):
        for val in value:
            _forbidden(val)


def wall(row):
    for key in ("wall_ts", "at", "utc_ms", "utc", "timestamp", "ts", "created_at"):
        value = row.get(key)
        if isinstance(value, (int, float)):
            if math.isfinite(value):
                at = float(value) / (1000 if key == "utc_ms" else 1)
                try:
                    datetime.fromtimestamp(at, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    continue
                return at
            continue
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return dt.replace(tzinfo=dt.tzinfo or S.WIB).timestamp()
            except ValueError:
                continue
    return None


def _validate_days(row, days):
    if isinstance(row, dict):
        if row.get("date") is not None:
            try:
                day = S.game_day(row["date"])
            except (ValueError, TypeError):
                pass  # an ISO stream date is checked by _forbidden, not a game day
            else:
                _range(day, days)
        for value in row.values():
            if isinstance(value, (dict, list)):
                _validate_days(value, days)
    elif isinstance(row, list):
        for value in row:
            _validate_days(value, days)


def _load(path, counts, days):
    rows = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not object")
        except ValueError:
            counts["unparsed:" + path.name] += 1
            continue
        _forbidden(row)
        rows.append(row)
    return rows


class OutsideDays(ValueError):
    """A row from a game day outside --days: skipped and counted, never stamped onto another day."""


def _range(date, days):
    day = S.game_day(date)
    if not days[0] <= day <= days[1]:
        raise OutsideDays("game day outside --days")
    return day


def _row(at, session, speaker, ingress, text, **kw):
    return dict(v=1, **S.stamp(at), session=session, speaker=speaker, ingress=ingress,
                text=text, game_active=True, privacy_epoch=0, current_epoch=0,
                owner_marked=ingress in ("ptt", "mic_pasif"), **kw)


def parse_run(run, days, counts):
    """Lenient adapters; every unreadable/relevant unstampable row is counted."""
    run = Path(run).resolve()
    _forbidden(str(run))
    if not run.is_dir():
        raise ValueError("run directory is missing")
    lines, observations, vocabulary, pages, moments, viewers = [], [], [], [], [], set()
    derived = []  # recordings older than native_vitals: day (+ clock/place when present) from other journal rows
    request_times = {}
    # Bridge filename supplies WIB date; midnight rollover is chronological.
    for path in sorted((run / "session_logs").glob("*_bridge.log")):
        _forbidden(path.name)
        try:
            date = datetime.strptime(path.name[:10], "%Y-%m-%d").date()
        except ValueError:
            counts["unparsed:" + path.name] += len(path.read_text(errors="replace").splitlines())
            continue
        try:
            previous = datetime.strptime(path.name[:17], "%Y-%m-%d_%H%M%S").replace(tzinfo=S.WIB).timestamp()
        except ValueError:
            counts["unparsed:log_start_time"] += 1
            previous = None
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = HISTORY.match(raw)
            if not match:
                counts[("unparsed:" if "[History Recorded]" in raw else "ignored:") + path.name] += 1
                continue
            clock, speaker, text = match.groups()
            try:
                dt = datetime.fromisoformat(str(date) + "T" + clock).replace(tzinfo=S.WIB)
            except ValueError:
                counts["unparsed:" + path.name] += 1
                continue
            if previous and dt.timestamp() < previous - 12 * 3600:
                date += timedelta(days=1)
                dt += timedelta(days=1)
            previous = dt.timestamp()
            _forbidden(dt.isoformat())
            if speaker.startswith("Viewer "):
                name = re.sub(r" \(YouTube\)$", "", speaker[7:]).lstrip("@")
                viewers.add(name)
                who, ingress = "viewer", "youtube"
            elif speaker == "Streamer":
                who, ingress = "owner", "ptt"
            elif speaker == "Arti (VTuber)":
                who, ingress = "arti", "spoken_reply"
            else:
                who, ingress = "system", "system"
            lines.append(_row(previous, run.name, who, ingress, text))
    path = run / "owner_reactions.jsonl"
    for row in _load(path, counts, days) if path.exists() else []:
        game = row.get("game") or {}
        if not isinstance(game, dict) or not any(isinstance(row.get(k), str) for k in ("owner_text", "arti_before", "arti_after")):
            counts["unparsed:owner_reactions.jsonl"] += 1
            continue
        if game.get("date") is not None:
            try:
                _range(game["date"], days)
            except OutsideDays:
                counts["outside_days:owner_reactions"] += 1
                continue
            except ValueError:
                counts["unparsed:owner_game_date"] += 1
        for field, speaker, ingress, at in (
            ("owner_text", "owner", "mic_pasif" if row.get("kind") == "mic_pasif" else "ptt", wall(row)),
            ("arti_before", "arti", "spoken_reply", wall({"utc": row.get("arti_before_utc")})),
            ("arti_after", "arti", "spoken_reply", wall({"utc": row.get("arti_after_utc")}))):
            text = row.get(field)
            if text is None:
                continue
            if not isinstance(text, str) or at is None:
                counts["unparsed:owner_reactions.jsonl:" + field] += 1
                continue
            # Recorded passive owner reactions carry the owner's provenance;
            # recompute W3 surface mark, rather than treat all passive as owner.
            new = _row(at, run.name, speaker, ingress, text, game_time=game)
            if ingress == "mic_pasif":
                from arti_runtime.mic_priority import mark
                new["owner_marked"] = mark(text, last_reply=row.get("arti_before") or "",
                    last_reply_end_ts=wall({"utc": row.get("arti_before_utc")}) or 0, now=at)[0]
            # The same utterance may appear in both adapters. Preserve its
            # stronger ingress metadata on the canonical bridge row.
            duplicate = next((old for old in lines if old["speaker"] == speaker and old["text"] == text
                              and abs(old["wall_ts"] - at) <= 3), None)
            if duplicate:
                duplicate.update(ingress=ingress, owner_marked=new["owner_marked"])
                new["adapter_duplicate"] = True
            lines.append(new)
    for path in sorted(run.glob("runtime/live7/*/day-*/journal/g1_journal.jsonl")):
        for row in _load(path, counts, days):
            game = row.get("game") or {}
            if not isinstance(game, dict):
                counts["unparsed:journal_game"] += 1
                continue
            if game.get("date") is not None:
                try:
                    _range(game["date"], days)
                except OutsideDays:
                    counts["outside_days:journal"] += 1
                    at = wall(row)
                    if row.get("ev") == "native_vitals" and at is not None:
                        observations.append((at, dict(day=S.game_day(game["date"]), clock=game.get("time"),
                                                      place=game.get("location"), outside=True)))
                    continue
                except ValueError:
                    counts["unparsed:journal_game_date"] += 1
                    continue
            at = wall(row)
            if at is not None and row.get("ev") != "native_vitals":
                stamp = game.get("date") or (row.get("game_time") if isinstance(row.get("game_time"), str) else None)
                if stamp:
                    try:
                        day = S.game_day(stamp)
                    except ValueError:
                        day = None
                    if day is not None:
                        place = game.get("location") if isinstance(game.get("location"), str) else (
                            row.get("location") if isinstance(row.get("location"), str) else None)
                        clock = game.get("time") if type(game.get("time")) is int else None
                        derived.append((at, dict(day=day, clock=clock, place=place, derived=True,
                                                 outside=not days[0] <= day <= days[1])))
            if at is not None:
                for key in (row.get("generation_id"), row.get("request_index")):
                    if key is not None:
                        request_times.setdefault((path.parent, str(key)), at)
            if row.get("ev") == "native_vitals":
                try:
                    obs = dict(day=_range(game.get("date"), days), clock=game.get("time"), place=game.get("location"))
                    if at is None:
                        raise TypeError("no wall time")
                    observations.append((at, obs))
                except (TypeError, ValueError):
                    counts["unparsed:native_vitals"] += 1
            elif isinstance(row.get("text"), str) and (row.get("kind") or row.get("ev")):
                if at is None:
                    counts["unparsed:journal_text_time"] += 1
                else:
                    vocabulary.append((at, row["text"]))
            else:
                counts["ignored:journal"] += 1
    for path in sorted(run.glob("runtime/live7/*/day-*/journal/provider_archive/*-request.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except ValueError:
            counts["unparsed:page_json"] += 1
            continue
        _forbidden(row)
        try:
            _validate_days(row, days)
        except OutsideDays:
            counts["outside_days:page"] += 1
            continue  # a page from another day never lends its vocabulary
        at = wall(row) if isinstance(row, dict) else None
        if at is None and isinstance(row, dict):
            for key in ("metadata", "request", "meta"):
                at = wall(row.get(key, {})) if isinstance(row.get(key), dict) else None
                if at is not None:
                    break
        if at is None and isinstance(row, dict):
            at = next((request_times[(path.parent.parent, str(key))]
                       for key in (row.get("generation_id"), row.get("request_index"))
                       if (path.parent.parent, str(key)) in request_times), None)
        if at is None:
            # The page file is written when the request is sent: its modification time is that wall time.
            try:
                at = path.stat().st_mtime
                counts["page_time_from_file"] += 1
            except OSError:
                at = None
        if at is None:
            counts["unparsed:page_time"] += 1
        else:
            pages.append((at, list(S.strings(row))))
    if not observations and derived:
        # No native_vitals in this recording: stamp lines from the nearest journal row that carries the game day.
        observations = derived
        counts["observations_derived"] += len(derived)
    observations.sort(key=lambda r: r[0])
    path = run / "host_story.jsonl"
    for row in _load(path, counts, days) if path.exists() else []:
        try:
            day = _range(row.get("date"), days)
        except OutsideDays:
            counts["outside_days:host_story"] += 1
            continue
        except ValueError:
            counts["unparsed:host_date"] += 1
            continue
        texts = [t for t in S.strings([row.get("text"), row.get("lines"), row.get("speaker")])
                 if t.strip() and t.strip().upper() != "UNKNOWN"]
        if not any(isinstance(row.get(k), str) and row.get(k).strip() for k in ("text", "lines")):
            texts = []  # an episode start/end marker carries no words: it never takes a moment slot
        if not texts:
            counts["unparsed:host_text"] += 1
            continue
        at = wall(row)
        mapped = at is None
        if mapped and type(row.get("time")) is int:
            # Locate the host's game stamp on the native wall timeline. A
            # Stardew minute is not an elapsed real minute. No speed or clock
            # extrapolation: use the closest native clock on that game date.
            seconds = lambda clock: (clock // 100 * 60 + clock % 100) * 60
            matching = [(ts, obs) for ts, obs in observations
                        if obs["day"] == day and type(obs["clock"]) is int]
            if matching:
                at = min(matching, key=lambda item: (
                    abs(seconds(item[1]["clock"]) - seconds(row["time"])), item[0]))[0]
        if at is None:
            counts["unparsed:host_time"] += 1
        else:
            moments.append((at, " ".join(texts), mapped))
    # A recorded privacy transition is trusted only from the owner's ingress.
    from arti_screen_privacy import ScreenPrivacy
    privacy = ScreenPrivacy()
    lines.sort(key=lambda r: r["wall_ts"])
    for row in lines:
        if row["speaker"] == "owner" and (row["ingress"] == "ptt" or row["owner_marked"]):
            privacy.apply_streamer_text(row["text"])
        row.update(privacy_epoch=privacy.epoch, current_epoch=privacy.epoch,
                   restricted=privacy.restricted, viewer_names=viewers)
        row["recent_moments"] = [text[:S.MOMENT_CHARS] for ts, text, mapped in moments
            if not S.contains_viewer(text, viewers) and (
                abs(row["wall_ts"] - ts) <= 180 if mapped else 0 <= row["wall_ts"] - ts <= 180)][-3:]
    return lines, observations, vocabulary, pages


def replay(runs, days, scope, out, report=None):
    out = _safe_path(out)
    report = Path(report or "stream_notes_report.json")
    report = _safe_path(report if report.is_absolute() else out / report)
    if not report.is_relative_to(out):
        raise ValueError("report must stay inside scratch")
    runs = [Path(r).resolve() for r in runs]
    if any(out == r or out.is_relative_to(r) or r.is_relative_to(out) for r in runs):
        raise ValueError("scratch and input runs must be separate")
    counts, batches = Counter(), []
    # Validate every input before creating scratch or writing a single note.
    for run in runs:
        batches.append(parse_run(run, days, counts))
    decisions = []
    # Her seen vocabulary accumulates across streams in time order, as her first-seen ledger does: what she saw
    # on 11-10 is still seen on 11-12. Runs are processed oldest first whatever the argument order.
    batches.sort(key=lambda batch: min((r["wall_ts"] for r in batch[0]), default=0))
    earlier, earlier_pages = [], []
    for lines, observations, vocabulary, pages in batches:
        for row in lines:
            at = row["wall_ts"]
            observation = next((obs for ts, obs in reversed(observations) if ts <= at), None)
            outside = bool(observation and (observation.get("outside")
                                             or not days[0] <= observation["day"] <= days[1]))
            seen = earlier + [text for ts, text in vocabulary if ts <= at]
            current_page = max(((ts, text) for ts, text in pages if ts <= at), default=(0, []), key=lambda p: p[0])[1]
            keep, reason = S.about_the_game(row, dict(game=seen, page=earlier_pages + current_page))
            if row.get("adapter_duplicate"):
                keep, reason = False, "adapter_duplicate"
            if keep and observation is None:
                keep, reason = False, "no_observation"
            if outside:
                keep, reason = False, "outside_days"
            if keep:
                said = " ".join(row["text"].casefold().split())
                if any(k and r["speaker"] == row["speaker"] and abs(r["wall_ts"] - at) <= 60
                       and " ".join(r["text"].casefold().split()) == said for r, _, k, _ in decisions):
                    keep, reason = False, "repeat_within_60s"
            decisions.append((row, observation, keep, reason))
        earlier.extend(text for ts, text in vocabulary)
        earlier_pages.extend(sorted(set(S.page_names([text for ts, text in pages]))))
    out.mkdir(parents=True, exist_ok=True)
    _manifest(out)  # reject pre-existing reparse points before any scratch writes
    detail = []
    for row, observation, keep, reason in decisions:
        if keep:
            reason = S.record_line(out, scope, row, observation)
        counts[reason] += 1
        detail.append(dict(time=row["wib"], speaker=row["speaker"], said_as=S.said_as(row["text"]),
                           reason=reason, text=row["text"]))
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(dict(counts=dict(counts), lines=detail), ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(counts)


def _manifest(folder):
    rows = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("backup refuses links")
        if path.is_file():
            rows[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


@dataclass
class _VerifiedBackup:
    target: Path
    backup: Path
    manifest: dict


def create_verified_backup(target):
    target = _safe_path(target, target=True)
    if not target.is_dir():
        raise ValueError("target mind does not exist")
    before = _manifest(target)
    backup = target.with_name(target.name + ".stream-notes-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    shutil.copytree(target, backup)
    if before != _manifest(backup) or before != _manifest(target):
        raise ValueError("backup full file list/sha256 verification failed")
    proof = _VerifiedBackup(target, backup, before)
    _ISSUED_BACKUPS[id(proof)] = proof
    return proof


def load_drop(path):
    """A hand-drop list (ADR 216): {"drop": [{"text_ref": <text owner id>, ...}]}. Ids only matter; any other
    field (time, speaker, reason) is for the reader."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    items = data.get("drop") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("text_ref"), str) for i in items):
        raise ValueError("drop list must be {\"drop\": [{\"text_ref\": ...}]}")
    return [i["text_ref"] for i in items]


def append_verified(scratch, target, backup=None, drop=(), dropped=None):
    """No append can happen without this invocation's verified full backup.

    `drop`: text owner ids the reader removed (false accepts); each is skipped with its receipt, and a listed id
    that is not in scratch is refused (a typo never passes silently). `dropped` (a list) receives the skipped ids."""
    scratch, target = _safe_path(scratch), _safe_path(target, target=True)
    if (not isinstance(backup, _VerifiedBackup) or backup.target != target
            or _ISSUED_BACKUPS.get(id(backup)) is not backup
            or backup.backup.parent != target.parent or not backup.backup.is_dir()
            or backup.manifest != _manifest(backup.backup) or backup.manifest != _manifest(target)):
        raise ValueError("apply requires a verified backup created in this invocation")
    path = target / "stardew" / "discoveries.jsonl"
    source = scratch / "stardew" / "discoveries.jsonl"
    rows = []
    for raw in source.read_text(encoding="utf-8-sig").splitlines():
        row = json.loads(raw)
        _forbidden(row)
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not str(row.get("source", "")).startswith(("stream:owner:", "stream:arti:"))
                or row.get("kind") not in ("text_read", "seen_receipt", "said_text")):  # ADR 232: her own lines
            raise ValueError("scratch contains a non-stream discovery")
        rows.append((row, raw))
    owners = ("text_read", "said_text")  # ADR 232 part A: her own lines' text owner is said_text
    ids = {r["id"] for r, _ in rows if r.get("kind") in owners}
    for row, _ in rows:
        if row["kind"] == "seen_receipt" and row.get("receipt", {}).get("text_ref") not in ids:
            raise ValueError("scratch receipt has no text owner")
    drop = set(drop)
    if drop - ids:
        raise ValueError("drop list names ids that are not in scratch: %d" % len(drop - ids))
    existing = path.read_bytes() if path.exists() else b""
    known = {json.loads(line)["id"] for line in existing.decode("utf-8-sig").splitlines() if line.strip()}
    fresh = []
    for row, raw in rows:
        ref = row["id"] if row["kind"] in owners else row.get("receipt", {}).get("text_ref")
        if ref in drop:
            if dropped is not None and row["kind"] in owners:
                dropped.append(ref)
            continue
        if row["id"] not in known:
            fresh.append(raw)
            known.add(row["id"])
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            if existing and not existing.endswith(b"\n"):
                handle.write(b"\n")
            handle.write(("\n".join(fresh) + "\n").encode("utf-8"))
    _ISSUED_BACKUPS.pop(id(backup), None)
    return len(fresh)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path)
    parser.add_argument("--days")
    parser.add_argument("--scope")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--drop", type=Path, help="apply only: a hand-drop list of text owner ids")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.target is None or args.runs or args.report:
                raise ValueError("apply requires --target and existing --out; no replay/report")
            scratch, target = _safe_path(args.out), _safe_path(args.target, target=True)
            if scratch == target or scratch.is_relative_to(target) or target.is_relative_to(scratch):
                raise ValueError("scratch and target must be separate")
            drop = load_drop(args.drop) if args.drop else []
            backup = create_verified_backup(target)
            dropped = []
            counts = {"appended": append_verified(scratch, target, backup, drop=drop, dropped=dropped),
                      "dropped": dropped}
        else:
            if args.drop:
                raise ValueError("--drop belongs to --apply")
            if args.target or not args.runs or not args.scope or not re.fullmatch(r"\d+-\d+", args.days or ""):
                raise ValueError("replay requires --runs --days A-B --scope; target requires --apply")
            days = tuple(map(int, args.days.split("-")))
            if not 1 <= days[0] <= days[1]:
                raise ValueError("invalid --days")
            counts = replay(args.runs, days, args.scope, args.out, args.report)
    except (ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
