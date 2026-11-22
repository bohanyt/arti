"""WS-G4/G5 prep: the Python reflex constants mirror the mod's Protocol/ReflexContracts.cs (constants only)."""
from __future__ import annotations

import re
from pathlib import Path

import arti_stardew_command as cmd

ROOT = Path(__file__).resolve().parents[1]
CS = ROOT / "stardew-mod" / "ArtiStardewBridge" / "Protocol" / "ReflexContracts.cs"


def _cs_consts():
    src = CS.read_text(encoding="utf-8")
    strings = dict(re.findall(r'public const string (\w+) = "([^"]+)";', src))
    numbers = {k: float(v) for k, v in re.findall(r"public const (?:int|double) (\w+) = ([0-9.]+);", src)}
    return src, strings, numbers


def _cs_list(src, strings, name):
    m = re.search(rf"public static readonly IReadOnlyList<string> {name} = new\[\]\s*\{{(.*?)\}};", src, re.S)
    assert m, name
    out = []
    for tok in re.split(r"[,\s]+", m.group(1).strip()):
        if not tok:
            continue
        out.append(tok.strip('"') if tok.startswith('"') else strings[tok])
    return tuple(out)


def test_requests_and_verdicts_match():
    src, strings, _ = _cs_consts()
    assert _cs_list(src, strings, "Requests") == cmd.REFLEX_REQUESTS
    assert _cs_list(src, strings, "FishVerdicts") == cmd.REFLEX_VERDICTS_FISH
    assert _cs_list(src, strings, "CombatVerdicts") == cmd.REFLEX_VERDICTS_COMBAT
    assert _cs_list(src, strings, "ChargeVerdicts") == cmd.REFLEX_VERDICTS_CHARGE
    assert _cs_list(src, strings, "KindsWithArg") == cmd.REFLEX_VERDICT_KINDS_WITH_ARG
    assert _cs_list(src, strings, "Reasons") == cmd.REFLEX_REASONS
    assert _cs_list(src, strings, "RetreatReasons") == cmd.REFLEX_RETREAT_REASONS
    assert _cs_list(src, strings, "CombatUntil") == cmd.REFLEX_COMBAT_UNTIL


def test_bounds_match():
    _, _, n = _cs_consts()
    pairs = {
        "FishMaxSecondsMax": cmd.REFLEX_FISH_MAX_S_MAX,
        "FishMaxSecondsDefault": cmd.REFLEX_FISH_MAX_S_DEFAULT,
        "FishCastPowerMin": cmd.REFLEX_FISH_CAST_POWER_MIN,
        "FishCastPowerMax": cmd.REFLEX_FISH_CAST_POWER_MAX,
        "FishCastPowerDefault": cmd.REFLEX_FISH_CAST_POWER_DEFAULT,
        "FishHookRetriesMax": cmd.REFLEX_FISH_HOOK_RETRIES_MAX,
        "FishStaminaFloor": cmd.REFLEX_FISH_STAMINA_FLOOR,
        "CombatMaxSecondsMax": cmd.REFLEX_COMBAT_MAX_S_MAX,
        "CombatHpFloorPctMin": cmd.REFLEX_COMBAT_HP_FLOOR_PCT_MIN,
        "CombatHpHealGapPct": cmd.REFLEX_COMBAT_HP_HEAL_GAP_PCT,
        "CombatMaxHeals": cmd.REFLEX_COMBAT_MAX_HEALS,
        "CombatRadiusTilesMax": cmd.REFLEX_COMBAT_RADIUS_TILES_MAX,
        "CombatLatestStartTime": cmd.REFLEX_COMBAT_LATEST_START_TIME,
        "CombatRetreatTime": cmd.REFLEX_COMBAT_RETREAT_TIME,
        "CombatHardStopTime": cmd.REFLEX_COMBAT_HARD_STOP_TIME,
        "ChargeLevelMax": cmd.REFLEX_CHARGE_LEVEL_MAX,
        "ChargeMsPerLevel": cmd.REFLEX_CHARGE_MS_PER_LEVEL,
        # WS-G5 production wiring
        "CombatMaxSecondsDefault": cmd.REFLEX_COMBAT_MAX_S_DEFAULT,
        "CombatHpFloorPctDefault": cmd.REFLEX_COMBAT_HP_FLOOR_PCT_DEFAULT,
        "CombatMaxKillsMax": cmd.REFLEX_COMBAT_MAX_KILLS_MAX,
        "CombatMaxKillsDefault": cmd.REFLEX_COMBAT_MAX_KILLS_DEFAULT,
        "CombatEarliestStartTime": cmd.REFLEX_COMBAT_EARLIEST_START_TIME,
        "CombatHardFloorPct": cmd.REFLEX_COMBAT_HARD_FLOOR_PCT,
        "ChargeTargetLevelMax": cmd.REFLEX_CHARGE_TARGET_LEVEL_MAX,
        "ChargeMaxTicksMin": cmd.REFLEX_CHARGE_MAX_TICKS_MIN,
        "ChargeMaxTicksMax": cmd.REFLEX_CHARGE_MAX_TICKS_MAX,
        "ChargeMaxTicksDefault": cmd.REFLEX_CHARGE_MAX_TICKS_DEFAULT,
    }
    for name, py in pairs.items():
        assert n[name] == py, name


def test_verdict_pattern_shapes():
    pat = re.compile(cmd.REFLEX_VERDICT_PATTERN)
    for wire in ("FishCaught", "Aborted(stamina_low)", "Retreated(hp_floor)", "ChargeReached(1)", "ChargeAborted(above_cap)"):
        m = pat.match(wire)
        assert m and m.group("kind") in cmd.REFLEX_VERDICTS_FISH + cmd.REFLEX_VERDICTS_COMBAT + cmd.REFLEX_VERDICTS_CHARGE
        if m.group("arg") and m.group("kind") != "ChargeReached":
            assert m.group("arg") in cmd.REFLEX_REASONS
    assert not pat.match("Aborted()")
    assert set(cmd.REFLEX_RETREAT_REASONS) <= set(cmd.REFLEX_REASONS)
