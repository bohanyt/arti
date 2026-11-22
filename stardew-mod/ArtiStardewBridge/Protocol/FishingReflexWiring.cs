using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Release proof is distinct from a late post-catch pose divergence.</summary>
public static class FishPressCleanup
{
    public static bool Confirmed(bool cleanup, bool issued, bool released, int delivered,
        FishPhase phase, bool caught, string label) =>
        (cleanup && (!issued || released))
        || (issued && released && delivered > 0 && phase == FishPhase.Done && caught
            && label == "PRECONSUMER_HISTORY_DIVERGENCE:PIN_DRIFT:FACING");
}

// =====================================================================================================
// WS-G4 production wiring, pure parts (no game, no SMAPI): the reflex_fish wire args, cast-landing geometry,
// the water-edge stand search, the admission refusal order, the BobberBar oldPadState write guard and the
// reflex-press relaxation of the G0 pad hold gates. The game-coupled runner (FishingReflexRunner.cs) and
// ModEntry.Reflex.cs sample facts into these and apply the results. Evidence ceiling: UNIT_TESTED.
// Decomp refs (Stardew 1.6.15): FR = StardewValley.Tools/FishingRod.cs, GL = StardewValley/GameLocation.cs.
// =====================================================================================================

/// <summary><c>input_once</c> family <c>world_key</c> primitive <c>reflex_fish</c>: args exactly {max_s, cast_power, treasure, hook_retries}.</summary>
public static class ReflexFishTarget
{
    public const string Primitive = ReflexContracts.RequestFish;

    static readonly HashSet<string> Keys = new(StringComparer.Ordinal)
    {
        FishReflexArgs.ArgMaxSeconds, FishReflexArgs.ArgCastPower, FishReflexArgs.ArgTreasure, FishReflexArgs.ArgHookRetries,
    };

    public static bool TryParse(JsonElement args, out FishReflexArgs parsed)
    {
        parsed = FishReflexArgs.Default;
        if (args.ValueKind != JsonValueKind.Object)
            return false;
        int count = 0;
        foreach (JsonProperty p in args.EnumerateObject())
        {
            if (!Keys.Contains(p.Name))
                return false;
            count++;
        }
        if (count != Keys.Count)
            return false;
        JsonElement maxS = args.GetProperty(FishReflexArgs.ArgMaxSeconds);
        JsonElement power = args.GetProperty(FishReflexArgs.ArgCastPower);
        JsonElement treasure = args.GetProperty(FishReflexArgs.ArgTreasure);
        JsonElement retries = args.GetProperty(FishReflexArgs.ArgHookRetries);
        if (maxS.ValueKind != JsonValueKind.Number || !maxS.TryGetInt32(out int s)
            || power.ValueKind != JsonValueKind.Number || !power.TryGetDouble(out double p2) || double.IsNaN(p2)
            || treasure.ValueKind is not (JsonValueKind.True or JsonValueKind.False)
            || retries.ValueKind != JsonValueKind.Number || !retries.TryGetInt32(out int r))
            return false;
        parsed = new FishReflexArgs(s, p2, treasure.GetBoolean(), r);
        return parsed.Validate() is null;
    }

    /// <summary>input_args echo (the runtime compares it with what it sent).</summary>
    public static Dictionary<string, object> Args(FishReflexArgs a) => new(StringComparer.Ordinal)
    {
        [FishReflexArgs.ArgMaxSeconds] = a.MaxSeconds,
        [FishReflexArgs.ArgCastPower] = a.CastPower,
        [FishReflexArgs.ArgTreasure] = a.Treasure,
        [FishReflexArgs.ArgHookRetries] = a.HookRetries,
    };
}

/// <summary>Where the bobber lands (FR:1924-2019 castingChosenCountdown branch; DoFunction tile = bobber / 64, FR:376-394).</summary>
public static class FishCastGeometry
{
    /// <summary>FishingRod.getAddedDistance (FR:357-375).</summary>
    public static int AddedDistance(int fishingLevel) =>
        fishingLevel >= 15 ? 4 : fishingLevel >= 8 ? 3 : fishingLevel >= 4 ? 2 : fishingLevel >= 1 ? 1 : 0;

    public static (int Dx, int Dy) FacingDelta(int facing) => facing switch
    {
        0 => (0, -1),
        1 => (1, 0),
        2 => (0, 1),
        _ => (-1, 0),
    };

    /// <summary>Landing tile for a cast released at <paramref name="power"/> from the standing pixel (no in-flight steering).</summary>
    public static (int X, int Y) LandingTile(float standingPixelX, float standingPixelY, int facing, float power, int fishingLevel)
    {
        int added = AddedDistance(fishingLevel);
        float x = standingPixelX, y = standingPixelY;
        if (facing is 1 or 3)
        {
            float num = Math.Max(128f, power * (added + 4) * 64f) - 8f;
            x += facing == 3 ? -num : num;
        }
        else
        {
            float num4 = Math.Max(128f, power * (added + 3) * 64f);
            y += facing == 0 ? -num4 : num4;
        }
        return ((int)Math.Floor(x / 64f), (int)Math.Floor(y / 64f));
    }

    /// <summary>
    /// castingPower sweep per update: castingTimerSpeed 0.001/ms (FR:95) x ElapsedGameTime.Milliseconds (FR:2026; 16 at
    /// the fixed 60 Hz step). 1/60 is the conservative upper bound.
    /// </summary>
    public const float SweepStepPerTick = 1f / 60f;

    /// <summary>
    /// The release lands late: the runner releases on the first tick castingPower >= target (up to one step past it)
    /// and the game adds one more sweep step (FR:2026) before startCasting reads the released X (FR:2041). So the cast
    /// flies at a power in [target, target + 2 steps] (0.85 -> 0.85..0.883).
    /// </summary>
    public const int ReleaseOvershootSteps = 2;

    /// <summary>Highest power a cast released for <paramref name="target"/> can fly at (castingPower clamps at 1, FR:2026).</summary>
    public static float LatestReleasePower(float target) => Math.Min(1f, target + ReleaseOvershootSteps * SweepStepPerTick);

    /// <summary>
    /// Landing tiles of the whole release window (target and target + 2 sweep steps; the distance is monotonic in the
    /// power and the window spans far less than a tile, so the two ends cover it). Both must be fishable.
    /// </summary>
    public static ((int X, int Y) Early, (int X, int Y) Late) LandingWindow(float standingPixelX, float standingPixelY, int facing,
        float target, int fishingLevel) =>
        (LandingTile(standingPixelX, standingPixelY, facing, target, fishingLevel),
         LandingTile(standingPixelX, standingPixelY, facing, LatestReleasePower(target), fishingLevel));

    /// <summary>Every tile of the release window is on the map and fishable.</summary>
    public static bool WindowFishable(float standingPixelX, float standingPixelY, int facing, float target, int fishingLevel,
        Func<int, int, bool> fishable)
    {
        var (early, late) = LandingWindow(standingPixelX, standingPixelY, facing, target, fishingLevel);
        return early.X >= 0 && early.Y >= 0 && late.X >= 0 && late.Y >= 0 && fishable(early.X, early.Y) && fishable(late.X, late.Y);
    }

    /// <summary>Standing pixel of a farmer centred on a tile (Character.StandingPixel = bounding-box centre).</summary>
    public static (float X, float Y) TileCentre(int x, int y) => (x * 64f + 32f, y * 64f + 32f);
}

/// <summary>
/// D-B2-4 (live V2B2ACC10a): the ui.context "fishing" fact. Before the fix it was published only while the rod was held,
/// so a "catch one fish" goal with the rod in the backpack saw no water edge and no rod slot. Now: whenever a rod is in
/// the inventory (null = no rod at all: nothing published), with <c>rod_held</c> and <c>rod_slot</c> (first rod slot, so
/// the runtime can offer select_item of exactly that slot). Pure (unit-tested).
/// </summary>
public static class FishingFactsWire
{
    /// <summary>Index of the first item matching <paramref name="isRod"/>, or -1.</summary>
    public static int FirstRodSlot<T>(IEnumerable<T?>? items, Func<T, bool> isRod) where T : class
    {
        if (items is null)
            return -1;
        int i = 0;
        foreach (T? item in items)
        {
            if (item is not null && isRod(item))
                return i;
            i++;
        }
        return -1;
    }

    public static Dictionary<string, object>? Build(bool rodHeld, int rodSlot, bool facesWater, bool landingFishable,
        int landX, int landY, int level, IEnumerable<WaterEdge> edges)
    {
        if (!rodHeld && rodSlot < 0)
            return null;
        var fact = new Dictionary<string, object>
        {
            ["rod_held"] = rodHeld,
            // Without the rod in hand the cast facts describe nothing ARTI can do yet: never claim a fishable facing.
            ["faces_water"] = rodHeld && facesWater,
            ["landing_fishable"] = rodHeld && landingFishable,
            ["landing"] = new[] { landX, landY },
            ["fishing_level"] = level,
            ["water_edges"] = edges.Select(e => (object)e.Wire()).ToArray(),
        };
        if (rodSlot >= 0)
            fact["rod_slot"] = rodSlot;
        return fact;
    }
}

/// <summary>A stand tile beside water: stand at (StandX, StandY), face Facing (the adjacent FaceX/FaceY is water), the cast lands on (LandX, LandY).</summary>
public readonly record struct WaterEdge(int StandX, int StandY, int Facing, int FaceX, int FaceY, int LandX, int LandY)
{
    public Dictionary<string, object> Wire() => new(StringComparer.Ordinal)
    {
        ["stand_x"] = this.StandX, ["stand_y"] = this.StandY, ["facing"] = this.Facing,
        ["face_x"] = this.FaceX, ["face_y"] = this.FaceY, ["land_x"] = this.LandX, ["land_y"] = this.LandY,
    };
}

/// <summary>Bounded search for the nearest water-edge stand tiles (the runtime turns them into approach entries).</summary>
public static class WaterEdgeSearch
{
    public const int Radius = 6;
    public const int MaxEdges = 2;

    /// <summary>
    /// Every standable tile within <see cref="Radius"/> (Chebyshev) and every facing whose adjacent tile is water and
    /// whose landing tiles over the whole release window at <paramref name="power"/> (target .. target + 2 sweep steps,
    /// <see cref="FishCastGeometry.WindowFishable"/>) are fishable; nearest (Manhattan) first, then y, x, facing; at
    /// most one entry per stand tile; at most <see cref="MaxEdges"/>.
    /// </summary>
    public static IReadOnlyList<WaterEdge> Find(int playerX, int playerY, Func<int, int, bool> standable, Func<int, int, bool> water,
        Func<int, int, bool> fishable, int fishingLevel, float power, int radius = Radius, int max = MaxEdges)
    {
        List<(int d, WaterEdge e)> found = new();
        // A standable tile across a wall is not a fishing destination.
        var reached = new HashSet<(int X, int Y)> { (playerX, playerY) };
        var frontier = new List<(int X, int Y)> { (playerX, playerY) };   // BFS by index (no Queue<> in Protocol/: guard)
        int head = 0;
        while (head < frontier.Count)
        {
            var at = frontier[head++];
            for (int f = 0; f < 4; f++)
            {
                var (dx, dy) = FishCastGeometry.FacingDelta(f);
                var next = (X: at.X + dx, Y: at.Y + dy);
                if (Math.Abs(next.X-playerX) > radius || Math.Abs(next.Y-playerY) > radius
                    || reached.Contains(next) || !standable(next.X,next.Y)) continue;
                reached.Add(next); frontier.Add(next);
            }
        }
        for (int y = playerY - radius; y <= playerY + radius; y++)
        for (int x = playerX - radius; x <= playerX + radius; x++)
        {
            if (x < 0 || y < 0 || !reached.Contains((x,y)) || !standable(x, y))
                continue;
            for (int f = 0; f < 4; f++)
            {
                (int dx, int dy) = FishCastGeometry.FacingDelta(f);
                if (!water(x + dx, y + dy))
                    continue;
                (float sx, float sy) = FishCastGeometry.TileCentre(x, y);
                (int lx, int ly) = FishCastGeometry.LandingTile(sx, sy, f, power, fishingLevel);
                if (!FishCastGeometry.WindowFishable(sx, sy, f, power, fishingLevel, fishable))
                    continue;
                found.Add((Math.Abs(x - playerX) + Math.Abs(y - playerY), new WaterEdge(x, y, f, x + dx, y + dy, lx, ly)));
                break; // one facing per stand tile
            }
        }
        return found.OrderBy(t => t.d).ThenBy(t => t.e.StandY).ThenBy(t => t.e.StandX).ThenBy(t => t.e.Facing)
            .Take(max).Select(t => t.e).ToArray();
    }
}

/// <summary>Facts for the reflex_fish admission (sampled read-only by ModEntry.Reflex.cs).</summary>
public sealed record FishAdmissionFacts(
    bool FlagOn,
    bool PadPathConfigured,
    bool WindowActive,
    bool WorldReady,
    bool MenuOpen,
    bool EventUp,
    bool PlayerFree,
    bool RodHeld,
    bool UsingTool,
    bool RuntimeBusy,
    float Stamina,
    int FreeSlots,
    bool FacesWater,
    bool LandingFishable,
    string? PadModeRefusal,
    string? CursorHiddenRefusal);

/// <summary>reflex_fish admission (WS-G4): first failing term, or null. Nothing is pressed or written before it passes.</summary>
public static class FishReflexAdmission
{
    public const string Prefix = "REFLEX_FISH";

    public static string? Refuse(FishAdmissionFacts f)
    {
        if (!f.FlagOn) return Prefix + ":flag_off";
        if (!f.PadPathConfigured) return Prefix + ":pad_path_off";
        if (f.WindowActive) return Prefix + ":window_active";
        if (!f.WorldReady) return Prefix + ":world_not_ready";
        if (f.MenuOpen) return Prefix + ":menu_open";
        if (f.EventUp) return Prefix + ":event";
        if (!f.RodHeld) return Prefix + ":rod_not_held";
        if (f.UsingTool) return Prefix + ":using_tool";
        if (!f.PlayerFree) return Prefix + ":player_not_free";
        if (f.RuntimeBusy) return Prefix + ":runtime_busy";
        if (f.Stamina < ReflexContracts.FishStaminaFloor) return Prefix + ":" + ReflexContracts.ReasonStaminaLow;
        if (!f.FacesWater) return Prefix + ":not_facing_water";
        if (!f.LandingFishable) return Prefix + ":landing_not_fishable";
        if (f.PadModeRefusal is { } mode) return mode;
        if (f.CursorHiddenRefusal is { } cursor) return cursor;
        return null;
    }
}

/// <summary>
/// PLAN §1.5 c (ii)/(v): an X write to <c>Game1.oldPadState</c> only while the deepest active menu is the BobberBar
/// instance the reflex captured, the farmhand is inactive, pad controls are on, the lease is held and the reflex is in
/// its Bar phase. null = allowed. Neutral writes are never refused (they are the safe direction).
/// </summary>
public static class BobberBarWriteGuard
{
    public static string? Refuse(bool deepestIsBobberBar, bool sameInstance, bool windowActive, bool gamepadControls, bool leaseHeld,
        bool reflexInBar)
    {
        if (!reflexInBar) return "reflex_not_in_bar";
        if (!deepestIsBobberBar) return "deepest_not_bobber_bar";
        if (!sameInstance) return "bar_instance_changed";
        if (windowActive) return "window_active";
        if (!gamepadControls) return "gamepad_controls_off";
        if (!leaseHeld) return "lease_lost";
        return null;
    }
}

/// <summary>
/// The reflex press shapes on the G0 pad X hold path: WS-G4 Cast / Tap (rod), WS-G5 Swing (1-frame X with a MeleeWeapon)
/// and Charge (X held until toolPower reaches the target, Hoe / WateringCan with upgradeLevel >= 1).
/// </summary>
public enum BgReflexPressKind { Cast, Tap, Swing, Charge }

/// <summary>
/// WS-G4: a reflex press is a G0 pad X hold with three differences, all bounded here: the tick cap (cast up to
/// <see cref="FishingReflexMachine.CastHoldBudget"/>, tap exactly 1), the rod-in-use gate terms and the settle rule.
/// A TAP (hook / reel-in / dismiss) is pressed while the rod is in use, so UsingTool, Fishing (rod.inUse) and
/// !CanMove are not refusals for it while a FishingRod is held; every other G0 term (menu, event, pad mode,
/// cursor-hidden, pins, movement, binding, lease) stays strict. A CAST is strict before its press (the rod must be
/// idle) and, after the press, tolerates the rod's own timing cast (rod.inUse) besides the existing TOOL relaxations.
/// </summary>
public static class BgReflexPress
{
    public static int MaxTicks(BgReflexPressKind kind) => kind switch
    {
        BgReflexPressKind.Cast => FishingReflexMachine.CastHoldBudget,
        BgReflexPressKind.Charge => ReflexContracts.ChargeMaxTicksMax,   // WS-G5: released by RequestRelease at the target power
        _ => 1,
    };

    public static string? ValidateTicks(BgReflexPressKind kind, int ticks) =>
        ticks >= 1 && ticks <= MaxTicks(kind) && (kind is BgReflexPressKind.Cast or BgReflexPressKind.Charge || ticks == 1)
            ? null : BgHoldReason.InvalidHoldTicks;

    public static BgAdmissionFacts Relax(BgAdmissionFacts f, BgReflexPressKind? kind, bool rodHeld) =>
        kind == BgReflexPressKind.Tap && rodHeld ? f with { UsingTool = false, Fishing = false, CanMove = true } : f;

    public static BgHoldHookFacts Relax(BgHoldHookFacts f, BgReflexPressKind? kind, bool rodHeld, bool pressConsumed) =>
        kind is not { } k || !rodHeld ? f
        : k == BgReflexPressKind.Tap ? f with { UsingTool = false, Fishing = false, CanMove = true }
        : pressConsumed ? f with { Fishing = false }
        : f;
}
