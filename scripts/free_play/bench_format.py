"""The versioned, declarative input shared by bench planning and execution."""
from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.free_play.expectations import Engine, validate_match

SCHEMA = "arti.free_play.bench/1"
LIBRARY = Path(__file__).with_name("bench_checks")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
# One key each, pressed into her window by the recorded ui_op path. F is the game's journal key (QuestLog).
MENU_KEYS = {"f": "QuestLog", "e": "GameMenu"}


def require(condition, description):
    if not condition:
        raise ValueError(description)


def shape(value, required, optional=()):
    require(isinstance(value, dict) and required <= value.keys()
            and not (value.keys() - required - set(optional)),
            f"expected fields {sorted(required)} with optional {sorted(optional)}")


def identifier(value):
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value), "invalid bench identifier")
    return value


def identifier_word(value):
    require(isinstance(value, str) and WORD.fullmatch(value), "invalid native menu type name")
    return value


def line(value):
    require(isinstance(value, str) and value.strip() and len(value.splitlines()) == 1,
            "expected one nonempty line")


def marks(specs, kinds):
    Engine(specs, allow_absence="absent" in kinds)
    for spec in specs:
        require(spec["kind"] in kinds, "invalid proof mark kind")
        predicates = spec["steps"] if spec["kind"] == "sequence" else [spec["match"]]
        require(all(isinstance(p.get("ev"), str) and p["ev"] for p in predicates),
                "proof marks must name journal events")


def situation(spec, mode):
    shape(spec, {"id", "start", "opportunity", "must", "inconclusive"},
          {"must_not", "said_lines", "buttons", "observe_to_limit", "voice_memory"})
    identifier(spec["id"])
    line(spec["inconclusive"])
    start = spec["start"]
    shape(start, {"savestate", "native"}, {"debug", "capture", "setup", "host_gift_at", "clock", "record_from",
                                            "last", "focus", "from_checkpoint"})
    identifier(start["savestate"])
    if "record_from" in start:
        require(identifier(start["record_from"]) != start["savestate"], "record must name a later savestate")
    validate_match(start["native"])
    if "clock" in start:
        hour = start["clock"]
        require(type(hour) is int and 600 <= hour <= 2350 and hour % 100 in range(0, 60, 10),
                "invalid staging clock")
    if "capture" in start:
        line(start["capture"])
    if "from_checkpoint" in start:
        # ADR 242: a missing savestate the bench imports itself (copy only) from this live checkpoint folder.
        line(start["from_checkpoint"])
        require(Path(start["from_checkpoint"]).is_absolute(), "from_checkpoint must be an absolute folder")
    if "debug" in start:
        require(isinstance(start["debug"], list) and start["debug"], "debug must be a nonempty list")
        for command in start["debug"]:
            line(command)
            require(command.startswith("/debug "), "only checked /debug staging commands are allowed")
    if "last" in start:
        # ADR 242: one staging step after the final readiness read, right before ARMED. Readiness refuses an open
        # menu or a running event, so an open menu or a festival entry is staged here. `expect` names what it opens.
        last = start["last"]
        shape(last, {"expect"}, {"debug", "key"})
        identifier_word(last["expect"])
        require(("debug" in last) != ("key" in last), "the last staging step is one debug line or one key")
        if "debug" in last:
            line(last["debug"])
            require(last["debug"].startswith("/debug "), "only checked /debug staging commands are allowed")
        else:
            require(last["key"] in MENU_KEYS, f"menu key must be one of {sorted(MENU_KEYS)}")
    if "focus" in start:
        # ADR 242: the bench makes her game window the active one, then hands focus back to the HOST.
        focus = start["focus"]
        shape(focus, {"after_s", "hold_s"})
        require(all(type(focus[k]) is int for k in focus) and 0 <= focus["after_s"] <= 120
                and 1 <= focus["hold_s"] <= 120, "focus needs after_s 0..120 and hold_s 1..120")
    if "host_gift_at" in start:
        hour = start["host_gift_at"]
        require(type(hour) is int and 600 <= hour <= 2400 and hour % 100 < 60,
                "invalid HOST gift time")
    marks(spec["opportunity"], {"event", "sequence"})
    marks(spec["must"], {"event", "once", "sequence", "bracketed"})  # W1b B5: bracketed by front readbacks
    if "observe_to_limit" in spec:
        require(type(spec["observe_to_limit"]) is bool, "observe_to_limit must be true or false")
    if "voice_memory" in spec:
        require(type(spec["voice_memory"]) is bool, "voice_memory must be true or false")
    if "must_not" in spec:
        marks(spec["must_not"], {"absent"})
        Engine(spec["must"] + spec["must_not"])
    for key, steps in (("setup", start.get("setup", [])), ("buttons", spec.get("buttons", []))):
        require(isinstance(steps, list) and len(steps) <= 32, "expected at most 32 scripted choices")
        for step in steps:
            shape(step, {"skill", "arg"}, {"when", "select"})
            line(step["skill"])
            require(step["arg"] is None or isinstance(step["arg"], str), "invalid choice argument")
            if "when" in step:
                validate_match(step["when"])
            if "select" in step:
                require(step["arg"] is None, "select and an explicit argument are mutually exclusive")
                validate_match(step["select"])
    require(bool(spec.get("buttons")) == (mode == "buttons"), "buttons mode requires scripted choices")
    if "said_lines" in spec:
        from scripts.free_play.said_lines import parse
        require(mode == "play", "said lines require play mode")
        parse(spec["said_lines"])


def validate(check):
    shape(check, {"schema", "id", "question", "fix", "mode", "minutes", "situations"}, {"needs", "staged", "left"})
    for key in ("needs", "staged", "left"):
        if key in check:
            # ADR 242: rows only another branch writes, what staging did, and what is left to her.
            require(isinstance(check[key], list) and check[key], f"{key} must be a nonempty list")
            for entry in check[key]:
                line(entry)
    require(check["schema"] == SCHEMA, "unsupported bench schema")
    identifier(check["id"])
    line(check["question"])
    line(check["fix"])
    require(check["mode"] in ("play", "buttons"), "expected play or buttons mode")
    require(type(check["minutes"]) is int and 1 <= check["minutes"] <= 5,
            "expected a limit between one and five elapsed minutes")
    require(isinstance(check["situations"], list) and 1 <= len(check["situations"]) <= 2,
            "expected one or two situations")
    seen = set()
    for spec in check["situations"]:
        situation(spec, check["mode"])
        require(spec["id"] not in seen, "duplicate situation id")
        seen.add(spec["id"])
    return check


def resolve(names, folder=LIBRARY):
    """`01,03` or `01 03` or full ids: a two-digit prefix names exactly one check file (ADR 242)."""
    words = [w for name in names for w in str(name).split(",") if w]
    if words == ["all"]:
        return words
    stems = sorted(p.stem for p in folder.glob("*.json"))
    resolved = []
    for word in words:
        hits = [s for s in stems if s == word or s.startswith(word + "-")]
        require(len(hits) == 1, f"check {word!r} names {len(hits)} check files")
        resolved.append(hits[0])
    return resolved


def load(selection, folder=LIBRARY):
    selection = resolve(selection, folder)
    if selection == ["all"]:
        selection = sorted(p.stem for p in folder.glob("*.json"))
    require(bool(selection) and len(set(selection)) == len(selection), "empty or duplicate checks")
    checks = []
    for name in selection:
        with (folder / (identifier(name) + ".json")).open(encoding="utf-8") as stream:
            check = validate(json.load(stream))
        require(check["id"] == name, "check id must match its filename")
        checks.append(check)
    return checks
