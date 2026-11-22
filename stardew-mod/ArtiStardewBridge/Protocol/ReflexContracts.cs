namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Primitives v2 reflex vocabulary (WS-G4 fishing, WS-G5 combat + charge). Pure and SMAPI-free.
///
/// A reflex is one model call -> one bounded mod loop -> one verdict (PLAN §1.1 rule 3). This file only
/// defines the request names, the verdict words, the abort reasons and the argument bounds. Nothing here is
/// wired into the live latch yet (that is G4/G5 proper after G0 lands). The Python mirror lives in
/// <c>arti_stardew_command.py</c> (REFLEX_* constants); <c>tests/test_stardew_reflex_contracts.py</c> keeps
/// both sides equal.
///
/// Wire form of a verdict: <c>Kind</c> or <c>Kind(arg)</c>, e.g. <c>FishCaught</c>, <c>Aborted(stamina_low)</c>,
/// <c>Retreated(hp_floor)</c>, <c>ChargeReached(1)</c>, <c>ChargeAborted(above_cap)</c>.
/// </summary>
public static class ReflexContracts
{
    // ---------------------------------------------------------------- requests
    public const string RequestFish = "reflex_fish";
    public const string RequestCombat = "reflex_combat";
    public const string RequestCharge = "reflex_charge";

    public static readonly IReadOnlyList<string> Requests = new[] { RequestFish, RequestCombat, RequestCharge };

    // ---------------------------------------------------------------- verdict kinds
    public const string FishCaught = "FishCaught";
    public const string FishEscaped = "FishEscaped";
    public const string NoBite = "NoBite";
    public const string Aborted = "Aborted";

    public const string MonsterKilled = "MonsterKilled";
    public const string Retreated = "Retreated";
    public const string Downed = "Downed";
    public const string NoTarget = "NoTarget";

    public const string ChargeReached = "ChargeReached";
    public const string ChargeAborted = "ChargeAborted";

    public static readonly IReadOnlyList<string> FishVerdicts = new[] { FishCaught, FishEscaped, NoBite, Aborted };
    public static readonly IReadOnlyList<string> CombatVerdicts = new[] { MonsterKilled, Retreated, Downed, NoTarget, Aborted };
    public static readonly IReadOnlyList<string> ChargeVerdicts = new[] { ChargeReached, ChargeAborted };

    /// <summary>
    /// Verdict kinds that carry an argument: reason for Aborted/Retreated/ChargeAborted, level for ChargeReached, kill count
    /// for MonsterKilled (WS-G5: <c>MonsterKilled(n)</c>, n = 1..<see cref="CombatMaxKillsMax"/>).
    /// </summary>
    public static readonly IReadOnlyList<string> KindsWithArg = new[] { Aborted, Retreated, ChargeReached, ChargeAborted, MonsterKilled };

    // ---------------------------------------------------------------- reasons (Aborted / Retreated / ChargeAborted)
    public const string ReasonStop = "stop";                       // STOP file / B / pad panic
    public const string ReasonLeaseLost = "lease_lost";
    public const string ReasonFocusRegain = "focus_regain";        // owner focused the farmhand window
    public const string ReasonEvent = "event";                     // eventUp / festival / cutscene
    public const string ReasonForeignMenu = "foreign_menu";        // a menu the reflex does not own opened
    public const string ReasonLocationChanged = "location_changed";
    public const string ReasonTimeout = "timeout";                 // max_s / phase budget
    public const string ReasonException = "exception";
    public const string ReasonInterrupted = "interrupted";         // the game reset the action under us (rod reset, player moved)
    public const string ReasonToolChanged = "tool_changed";        // rod / weapon / charge tool no longer held
    public const string ReasonInventoryFull = "inventory_full";
    public const string ReasonStaminaLow = "stamina_low";
    public const string ReasonCastNoEffect = "cast_no_effect";     // no timing cast, or the bobber landed on a non-fishable tile
    public const string ReasonHookMissed = "hook_missed";          // the bite window passed more than hook_retries times
    public const string ReasonHpFloor = "hp_floor";
    public const string ReasonTimeRule = "time_rule";              // 24:00 retreat / 01:00 hard stop
    public const string ReasonStuck = "stuck";                     // no distance progress (approach or retreat)
    public const string ReasonNoExit = "no_exit";                  // retreat wanted but no exit tile known
    public const string ReasonAboveCap = "above_cap";              // charge level > upgradeLevel + Reaching
    public const string ReasonNotChargeable = "not_chargeable";    // Axe/Pickaxe/basic tools never charge (decomp)
    public const string ReasonPadRefused = "pad_refused";          // WS-G4: the G0 pad path refused / never delivered a reflex press
    public const string ReasonHealNeeded = "heal_needed";
    public const string ReasonContact = "contact";                 // ADR 177: a disengage leg (contact + falling HP on a Mines floor)
    public const string ReasonRouteBlocked = "route_blocked";      // FIXB15 D-B13-3: the only route needs a breakable rock (detail rock=x,y)
    public const string ReasonLocationRefused = "location_refused"; // WS-G5 review minor 3: VolcanoDungeon (outside the time rule)          // WS-G5: HP under the hard floor with food in the inventory: the runtime eats (routine_eat), then may re-arm

    public static readonly IReadOnlyList<string> Reasons = new[]
    {
        ReasonStop, ReasonLeaseLost, ReasonFocusRegain, ReasonEvent, ReasonForeignMenu, ReasonLocationChanged,
        ReasonTimeout, ReasonException, ReasonInterrupted, ReasonToolChanged, ReasonInventoryFull, ReasonStaminaLow, ReasonCastNoEffect,
        ReasonHookMissed, ReasonHpFloor, ReasonTimeRule, ReasonStuck, ReasonNoExit, ReasonAboveCap, ReasonNotChargeable,
        ReasonPadRefused, ReasonHealNeeded, ReasonLocationRefused, ReasonRouteBlocked, ReasonContact,
    };

    /// <summary>FIXB15 D-B13-3: the combat objective's monster kind ("slime" | "monster"); null = any monster.</summary>
    public static readonly IReadOnlyList<string> CombatTargetKinds = new[] { "slime", "monster" };

    public static bool KindMatches(string? kind, string? monsterType) =>
        kind is null or "monster" || (monsterType is not null && monsterType.Contains("slime", StringComparison.OrdinalIgnoreCase));

    /// <summary>Reasons that end a combat reflex through the retreat / exit leg (verdict Retreated(reason)).</summary>
    public static readonly IReadOnlyList<string> RetreatReasons = new[] { ReasonHpFloor, ReasonTimeRule, ReasonTimeout };

    // ---------------------------------------------------------------- argument bounds (Python mirror: REFLEX_*)
    public const int FishMaxSecondsMax = 90;
    public const int FishMaxSecondsDefault = 60;
    public const double FishCastPowerMin = 0.30;
    public const double FishCastPowerMax = 0.95;                   // castingPower bounces at 1.0 (FishingRod.cs:2035)
    public const double FishCastPowerDefault = 0.85;
    public const int FishHookRetriesMax = 2;
    public const int FishHookRetriesDefault = 1;
    public const double FishStaminaFloor = 8.0;                    // one cast costs 8 - 0.1 * FishingLevel (FishingRod.cs:408)

    public const int CombatMaxSecondsMax = 60;
    /// <summary>ADR 177: one disengage leg (walk to the up-ladder stand, defend only when cornered) is bounded to this.</summary>
    public const int CombatDisengageMaxSeconds = 30;
    public const int CombatMaxSecondsDefault = 30;
    public const int CombatHpFloorPctMin = 30;
    public const int CombatHpFloorPctDefault = 40;
    public const int CombatHpHealGapPct = 20;                      // hp_heal >= hp_floor + 20
    public const int CombatHpHealPctDefault = 60;
    public const int CombatMaxHeals = 2;
    public const int CombatRadiusTilesMax = 8;
    public const int CombatLatestStartTime = 2000;                 // never start a Mines combat after 20:00
    public const int CombatRetreatTime = 2400;                     // begin the exit leg at 24:00
    public const int CombatHardStopTime = 2500;                    // 01:00: stop everything (pass-out at 02:00)
    public const int CombatSwingCadenceTicksMin = 12;
    public const int CombatSwingCadenceTicksDefault = 24;          // ~390 ms sword swing (MeleeWeapon.cs:1138, 1252)
    // WS-G5 production wiring (review-driven brief §3): wire args {max_s, hp_floor_pct, max_kills, retreat}.
    public const int CombatMaxKillsMax = 5;
    public const int CombatMaxKillsDefault = 1;
    public const int CombatEarliestStartTime = 900;                // Mines / Skull Cavern: start only 09:00 <= t < 20:00
    public const int CombatHardFloorPct = 20;                      // below this (and food held in the inventory): Aborted(heal_needed)

    public const int ChargeLevelMax = 5;                           // Iridium (4) + Reaching (1)
    public const int ChargeMsPerLevel = 600;                       // Game1.cs:13040 (x AnimationSpeedModifier)
    public const int ChargeMaxMsDefault = 3500;
    // WS-G5 production wiring: wire args {target_level, max_ticks}; level 1..4 (Reaching's 5th level stays P2), the press
    // is held at most max_ticks pad frames (level n first reached on held tick ~40n, live PRB-3 H7).
    public const int ChargeTargetLevelMax = 4;
    public const int ChargeMaxTicksMin = 45;
    public const int ChargeMaxTicksMax = 200;
    public const int ChargeMaxTicksDefault = 200;

    public static readonly IReadOnlyList<string> CombatUntil = new[] { "target_dead", "area_clear" };

    // ---------------------------------------------------------------- verdict wire form
    public static string Verdict(string kind, string? arg = null)
    {
        if (!AllVerdictKinds.Contains(kind))
            throw new ArgumentException("unknown verdict kind " + kind, nameof(kind));
        bool wantsArg = KindsWithArg.Contains(kind);
        if (wantsArg && string.IsNullOrEmpty(arg))
            throw new ArgumentException(kind + " needs an argument", nameof(arg));
        if (!wantsArg && !string.IsNullOrEmpty(arg))
            throw new ArgumentException(kind + " takes no argument", nameof(arg));
        if (wantsArg && kind is ChargeReached or MonsterKilled)
        {
            int max = kind == ChargeReached ? ChargeLevelMax : CombatMaxKillsMax;
            if (!int.TryParse(arg, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out int n) || n < 1 || n > max)
                throw new ArgumentException("count out of range " + arg, nameof(arg));
        }
        else if (wantsArg && !Reasons.Contains(arg!))
            throw new ArgumentException("unknown reason " + arg, nameof(arg));
        return wantsArg ? kind + "(" + arg + ")" : kind;
    }

    public static string AbortedVerdict(string reason) => Verdict(Aborted, reason);

    public static string ChargeReachedVerdict(int level) => Verdict(ChargeReached, level.ToString(System.Globalization.CultureInfo.InvariantCulture));

    /// <summary>WS-G5: <c>MonsterKilled(n)</c>, n = verified kills of this reflex (1..<see cref="CombatMaxKillsMax"/>).</summary>
    public static string MonsterKilledVerdict(int kills) => Verdict(MonsterKilled, kills.ToString(System.Globalization.CultureInfo.InvariantCulture));

    public static IReadOnlyList<string> AllVerdictKinds { get; } =
        FishVerdicts.Concat(CombatVerdicts).Concat(ChargeVerdicts).Distinct().ToArray();

    /// <summary>Split <c>Kind(arg)</c>; false for anything outside the vocabulary.</summary>
    public static bool TryParseVerdict(string? wire, out string kind, out string? arg)
    {
        kind = "";
        arg = null;
        if (string.IsNullOrEmpty(wire))
            return false;
        int open = wire.IndexOf('(');
        if (open < 0)
        {
            kind = wire;
            return AllVerdictKinds.Contains(kind) && !KindsWithArg.Contains(kind);
        }
        if (!wire.EndsWith(")", StringComparison.Ordinal) || open == 0)
            return false;
        kind = wire.Substring(0, open);
        arg = wire.Substring(open + 1, wire.Length - open - 2);
        if (!KindsWithArg.Contains(kind) || arg.Length == 0)
            return false;
        if (kind == ChargeReached)
            return int.TryParse(arg, out int n) && n >= 1 && n <= ChargeLevelMax;
        if (kind == MonsterKilled)
            return int.TryParse(arg, out int k) && k >= 1 && k <= CombatMaxKillsMax;
        return Reasons.Contains(arg);
    }

    /// <summary>Which request family a verdict kind belongs to (Aborted is shared by fish and combat).</summary>
    public static bool VerdictAllowedFor(string request, string kind) => request switch
    {
        RequestFish => FishVerdicts.Contains(kind),
        RequestCombat => CombatVerdicts.Contains(kind),
        RequestCharge => ChargeVerdicts.Contains(kind),
        _ => false,
    };
}

/// <summary>
/// Validated arguments of <c>reflex_fish</c>. Wire (input_once family world_key, args): <c>max_s</c> (timeout seconds),
/// <c>cast_power</c> (castingPower release target), <c>treasure</c> (chase a visible treasure after progress 0.6),
/// <c>hook_retries</c> (missed nibbles allowed before the reel-in). One reflex = one cast (no max_casts).
/// </summary>
public readonly record struct FishReflexArgs(int MaxSeconds, double CastPower, bool Treasure, int HookRetries)
{
    public const string ArgMaxSeconds = "max_s";
    public const string ArgCastPower = "cast_power";
    public const string ArgTreasure = "treasure";
    public const string ArgHookRetries = "hook_retries";

    public static FishReflexArgs Default => new(ReflexContracts.FishMaxSecondsDefault, ReflexContracts.FishCastPowerDefault, false,
        ReflexContracts.FishHookRetriesDefault);

    /// <summary>Null when valid, else the refusal code.</summary>
    public string? Validate()
    {
        if (MaxSeconds < 1 || MaxSeconds > ReflexContracts.FishMaxSecondsMax)
            return "max_s_out_of_range";
        if (CastPower < ReflexContracts.FishCastPowerMin || CastPower > ReflexContracts.FishCastPowerMax)
            return "cast_power_out_of_range";
        if (HookRetries < 0 || HookRetries > ReflexContracts.FishHookRetriesMax)
            return "hook_retries_out_of_range";
        return null;
    }
}

/// <summary>Validated arguments of <c>reflex_combat</c>.</summary>
public readonly record struct CombatReflexArgs(string Until, int MaxSeconds, int HpFloorPct, int HpHealPct, int MaxHeals,
    int RadiusTiles, int SwingCadenceTicks, string? TargetId)
{
    /// <summary>WS-G5 wire <c>max_kills</c>: MonsterKilled(n) once n kills are verified (1..5).</summary>
    public int MaxKills { get; init; } = ReflexContracts.CombatMaxKillsDefault;

    /// <summary>WS-G5 wire <c>retreat</c>: under hp_floor (or the 24:00 time rule in the Mines) walk to the up-ladder tile.</summary>
    public bool Retreat { get; init; } = true;
    // ADR 169: explicitly owned by a Mines exit; never the ordinary work fight.
    public bool Escape { get; init; } = false;
    // ADR 177: the body's own escape on a Mines floor: walk the nav-grid path to the up-ladder stand at game tick,
    // swing only at an attacker in contact while no way out exists. Not a fight elsewhere, never a descent.
    public bool Disengage { get; init; } = false;

    /// <summary>FIXB15 D-B13-3 wire <c>target_kind</c> (optional): only monsters of the objective's kind are targets.</summary>
    public string? TargetKind { get; init; } = null;

    public static CombatReflexArgs Default => new("target_dead", ReflexContracts.CombatMaxSecondsDefault,
        ReflexContracts.CombatHpFloorPctDefault, ReflexContracts.CombatHpHealPctDefault, ReflexContracts.CombatMaxHeals,
        ReflexContracts.CombatRadiusTilesMax, ReflexContracts.CombatSwingCadenceTicksDefault, null);

    public string? Validate()
    {
        if (Escape && (Retreat || MaxKills != 1 || MaxSeconds > 4))
            return "escape_requires_one_kill_without_retreat";
        if (Disengage && (Escape || !Retreat || MaxKills != 1 || MaxSeconds > ReflexContracts.CombatDisengageMaxSeconds))
            return "disengage_requires_retreat_one_kill";
        if (!ReflexContracts.CombatUntil.Contains(Until))
            return "until_unknown";
        if (MaxSeconds < 1 || MaxSeconds > ReflexContracts.CombatMaxSecondsMax)
            return "max_s_out_of_range";
        if (HpFloorPct < ReflexContracts.CombatHpFloorPctMin || HpFloorPct > 90)
            return "hp_floor_out_of_range";
        if (HpHealPct != 0 && (HpHealPct < HpFloorPct + ReflexContracts.CombatHpHealGapPct || HpHealPct > 95))
            return "hp_heal_out_of_range";
        if (MaxHeals < 0 || MaxHeals > ReflexContracts.CombatMaxHeals)
            return "max_heals_out_of_range";
        if (RadiusTiles < 1 || RadiusTiles > ReflexContracts.CombatRadiusTilesMax)
            return "radius_out_of_range";
        if (SwingCadenceTicks < ReflexContracts.CombatSwingCadenceTicksMin || SwingCadenceTicks > 120)
            return "cadence_out_of_range";
        if (MaxKills < 1 || MaxKills > ReflexContracts.CombatMaxKillsMax)
            return "max_kills_out_of_range";
        return null;
    }
}

/// <summary>Validated arguments of <c>reflex_charge</c>.</summary>
public readonly record struct ChargeReflexArgs(int Level, int MaxMs)
{
    /// <summary>WS-G5 wire <c>max_ticks</c>: the X press is held at most this many delivered pad frames (0 = MaxMs).</summary>
    public int MaxTicks { get; init; } = 0;

    /// <summary>Held-frame budget of the press: <see cref="MaxTicks"/>, else <see cref="MaxMs"/> at 60 frames/s.</summary>
    public int HoldBudgetTicks => this.MaxTicks > 0 ? this.MaxTicks : (int)Math.Ceiling(this.MaxMs * 60.0 / 1000.0);

    public string? Validate(int upgradeLevel, bool reaching)
    {
        if (Level < 1 || Level > ReflexContracts.ChargeLevelMax)
            return "level_out_of_range";
        if (Level > upgradeLevel + (reaching ? 1 : 0))
            return ReflexContracts.ReasonAboveCap;
        if (MaxMs < 100 || MaxMs > 10000)
            return "max_ms_out_of_range";
        return null;
    }
}
