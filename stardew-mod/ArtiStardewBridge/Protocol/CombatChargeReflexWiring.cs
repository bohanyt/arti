using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// =====================================================================================================
// WS-G5 production wiring, pure parts (no game, no SMAPI): the reflex_combat / reflex_charge wire args, the admission
// refusal orders, the published combat facts (monsters, cap 8), the charge faced-tile / area guard and the area
// readback. The game-coupled runners (CombatReflexRunner.cs, ChargeReflexRunner.cs) and ModEntry.ReflexG5.cs sample
// facts into these and apply the results. Evidence ceiling: UNIT_TESTED.
// =====================================================================================================

/// <summary><c>input_once</c> family <c>world_key</c> primitive <c>reflex_combat</c>: args exactly {max_s, hp_floor_pct, max_kills, retreat}.</summary>
public static class ReflexCombatTarget
{
    public const string Primitive = ReflexContracts.RequestCombat;
    public const string ArgMaxSeconds = "max_s";
    public const string ArgHpFloorPct = "hp_floor_pct";
    public const string ArgMaxKills = "max_kills";
    public const string ArgRetreat = "retreat";
    public const string ArgTargetKind = "target_kind";   // FIXB15 D-B13-3 (optional)

    static readonly HashSet<string> Keys = new(StringComparer.Ordinal) { ArgMaxSeconds, ArgHpFloorPct, ArgMaxKills, ArgRetreat };

    /// <summary>The machine args of a wire request: nearest target, radius 8, cadence 24, no in-reflex heal (handed back).</summary>
    public static CombatReflexArgs ToArgs(int maxSeconds, int hpFloorPct, int maxKills, bool retreat) =>
        new CombatReflexArgs(maxKills == 1 ? "target_dead" : "area_clear", maxSeconds, hpFloorPct, 0, 0,
            ReflexContracts.CombatRadiusTilesMax, ReflexContracts.CombatSwingCadenceTicksDefault, null)
        { MaxKills = maxKills, Retreat = retreat };

    public static CombatReflexArgs Default => ToArgs(ReflexContracts.CombatMaxSecondsDefault, ReflexContracts.CombatHpFloorPctDefault,
        ReflexContracts.CombatMaxKillsDefault, true);

    public static bool TryParse(JsonElement args, out CombatReflexArgs parsed)
    {
        parsed = Default;
        if (args.ValueKind != JsonValueKind.Object)
            return false;
        int count = 0;
        string? kind = null;
        bool escape = false, disengage = false;
        foreach (JsonProperty p in args.EnumerateObject())
        {
            if (p.Name == "disengage")
            {
                if (p.Value.ValueKind != JsonValueKind.True) return false;
                disengage = true;
                continue;
            }
            if (p.Name == "escape")
            {
                if (p.Value.ValueKind != JsonValueKind.True) return false;
                escape = p.Value.GetBoolean();
                continue;
            }
            if (p.Name == ArgTargetKind)
            {
                if (p.Value.ValueKind != JsonValueKind.String || !ReflexContracts.CombatTargetKinds.Contains(p.Value.GetString()))
                    return false;
                kind = p.Value.GetString();
                continue;
            }
            if (!Keys.Contains(p.Name))
                return false;
            count++;
        }
        if (count != Keys.Count)
            return false;
        JsonElement maxS = args.GetProperty(ArgMaxSeconds);
        JsonElement floor = args.GetProperty(ArgHpFloorPct);
        JsonElement kills = args.GetProperty(ArgMaxKills);
        JsonElement retreat = args.GetProperty(ArgRetreat);
        if (maxS.ValueKind != JsonValueKind.Number || !maxS.TryGetInt32(out int s)
            || floor.ValueKind != JsonValueKind.Number || !floor.TryGetInt32(out int f)
            || kills.ValueKind != JsonValueKind.Number || !kills.TryGetInt32(out int k)
            || retreat.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            return false;
        if (s < 1 || s > ReflexContracts.CombatMaxSecondsMax || f < ReflexContracts.CombatHpFloorPctMin || f > 90
            || k < 1 || k > ReflexContracts.CombatMaxKillsMax)
            return false;
        parsed = ToArgs(s, f, k, retreat.GetBoolean()) with { TargetKind = kind, Escape = escape, Disengage = disengage };
        return parsed.Validate() is null;
    }

    /// <summary>input_args echo (the runtime compares it with what it sent).</summary>
    public static Dictionary<string, object> Args(CombatReflexArgs a)
    {
        var d = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            [ArgMaxSeconds] = a.MaxSeconds,
            [ArgHpFloorPct] = a.HpFloorPct,
            [ArgMaxKills] = a.MaxKills,
            [ArgRetreat] = a.Retreat,
        };
        if (a.TargetKind is { } kind)
            d[ArgTargetKind] = kind;
        if (a.Escape) d["escape"] = true;
        if (a.Disengage) d["disengage"] = true;
        return d;
    }
}

/// <summary><c>input_once</c> family <c>world_key</c> primitive <c>reflex_charge</c>: args exactly {target_level, max_ticks}.</summary>
public static class ReflexChargeTarget
{
    public const string Primitive = ReflexContracts.RequestCharge;
    public const string ArgTargetLevel = "target_level";
    public const string ArgMaxTicks = "max_ticks";

    static readonly HashSet<string> Keys = new(StringComparer.Ordinal) { ArgTargetLevel, ArgMaxTicks };

    public static ChargeReflexArgs ToArgs(int level, int maxTicks) =>
        new ChargeReflexArgs(level, (int)Math.Ceiling(maxTicks * 1000.0 / 60.0)) { MaxTicks = maxTicks };

    public static bool TryParse(JsonElement args, out ChargeReflexArgs parsed)
    {
        parsed = ToArgs(1, ReflexContracts.ChargeMaxTicksDefault);
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
        JsonElement level = args.GetProperty(ArgTargetLevel);
        JsonElement ticks = args.GetProperty(ArgMaxTicks);
        if (level.ValueKind != JsonValueKind.Number || !level.TryGetInt32(out int l)
            || ticks.ValueKind != JsonValueKind.Number || !ticks.TryGetInt32(out int t))
            return false;
        if (l < 1 || l > ReflexContracts.ChargeTargetLevelMax || t < ReflexContracts.ChargeMaxTicksMin || t > ReflexContracts.ChargeMaxTicksMax)
            return false;
        parsed = ToArgs(l, t);
        return true;
    }

    public static Dictionary<string, object> Args(ChargeReflexArgs a) => new(StringComparer.Ordinal)
    {
        [ArgTargetLevel] = a.Level,
        [ArgMaxTicks] = a.HoldBudgetTicks,
    };
}

/// <summary>Melee weapon classification by qualified item id. Owner 2026-10-23: the scythes count as a combat weapon
/// (reflex_combat swings any MeleeWeapon); <see cref="IsScythe"/> stays a published fact (ui.context combat.scythe_held).</summary>
public static class CombatWeapon
{
    /// <summary>Scythe (W)47, Golden Scythe (W)53, Iridium Scythe (W)66 (Data/Weapons, MeleeWeapon.isScythe).</summary>
    public static bool IsScythe(string? qualifiedItemId) => qualifiedItemId is "(W)47" or "(W)53" or "(W)66";
}

/// <summary>Facts for the reflex_combat admission (sampled read-only by ModEntry.ReflexG5.cs).</summary>
public sealed record CombatAdmissionFacts(
    bool FlagOn,
    bool PadPathConfigured,
    bool WindowActive,
    bool WorldReady,
    bool MenuOpen,
    bool EventUp,
    bool PlayerFree,
    bool MeleeWeaponHeld,
    bool ScytheHeld,
    bool UsingTool,
    bool RuntimeBusy,
    bool LocationRefused,
    bool InMines,
    int TimeOfDay,
    int Health,
    int MaxHealth,
    int HpFloorPct,
    float NearestMonsterTiles,      // < 0 = no monster in the location
    int RadiusTiles,
    string? PadModeRefusal,
    string? CursorHiddenRefusal);

/// <summary>reflex_combat admission (WS-G5): first failing term, or null. Nothing is pressed before it passes.</summary>
public static class CombatReflexAdmission
{
    public const string Prefix = "REFLEX_COMBAT";

    public static string? Refuse(CombatAdmissionFacts f, bool escape = false, bool disengage = false)
    {
        if (disengage)
            return RefuseDisengage(f);
        if (!f.FlagOn) return Prefix + ":flag_off";
        if (!f.PadPathConfigured) return Prefix + ":pad_path_off";
        if (f.WindowActive) return Prefix + ":window_active";
        if (!f.WorldReady) return Prefix + ":world_not_ready";
        if (f.MenuOpen) return Prefix + ":menu_open";
        if (f.EventUp) return Prefix + ":event";
        if (!f.MeleeWeaponHeld) return Prefix + ":weapon_not_held";
        if (f.UsingTool) return Prefix + ":using_tool";
        if (!f.PlayerFree) return Prefix + ":player_not_free";
        if (f.RuntimeBusy) return Prefix + ":runtime_busy";
        if (f.LocationRefused) return Prefix + ":" + ReflexContracts.ReasonLocationRefused;
        if (escape && !f.InMines) return Prefix + ":escape_requires_mines";
        if (!escape && CombatTimeRule.StartRefusal(f.InMines, f.TimeOfDay) is { } time) return Prefix + ":" + time;
        if (f.Health <= 0 || f.MaxHealth <= 0 || !escape && (int)Math.Floor(100.0 * f.Health / f.MaxHealth) <= f.HpFloorPct) return Prefix + ":" + ReflexContracts.ReasonHpFloor;
        if (f.NearestMonsterTiles < 0 || f.NearestMonsterTiles > f.RadiusTiles) return Prefix + ":no_monster_in_radius";
        if (f.PadModeRefusal is { } mode) return mode;
        if (f.CursorHiddenRefusal is { } cursor) return cursor;
        return null;
    }

    /// <summary>ADR 177: the disengage leg needs no weapon, no time window and no HP floor (it exists for low HP):
    /// the Mines, a live player, a monster near and the same input / pad / cursor terms as every reflex.</summary>
    static string? RefuseDisengage(CombatAdmissionFacts f)
    {
        if (!f.FlagOn) return Prefix + ":flag_off";
        if (!f.PadPathConfigured) return Prefix + ":pad_path_off";
        if (f.WindowActive) return Prefix + ":window_active";
        if (!f.WorldReady) return Prefix + ":world_not_ready";
        if (f.MenuOpen) return Prefix + ":menu_open";
        if (f.EventUp) return Prefix + ":event";
        if (f.UsingTool) return Prefix + ":using_tool";
        if (!f.PlayerFree) return Prefix + ":player_not_free";
        if (f.RuntimeBusy) return Prefix + ":runtime_busy";
        if (f.LocationRefused || !f.InMines) return Prefix + ":disengage_requires_mines";
        if (f.Health <= 0 || f.MaxHealth <= 0) return Prefix + ":" + ReflexContracts.ReasonHpFloor;
        // ADR 179: no monster term: every walk out of a floor is this leg, a monster near or not.
        if (f.PadModeRefusal is { } mode) return mode;
        if (f.CursorHiddenRefusal is { } cursor) return cursor;
        return null;
    }
}

/// <summary>
/// WS-G5 review minor 4: the food handed back with Aborted(heal_needed) is the first toolbar slot the WS3b eat policy admits
/// (BgEatPrompt.CandidateMiss: edible, cheap, not artisan / quest / bundle / placeable / usable), never the first edible item.
/// </summary>
public static class CombatFood
{
    public const int ToolbarSlots = 12;

    public static int FirstEligibleSlot(IReadOnlyList<BgEatCandidateFacts?> inventory)
    {
        for (int i = 0; i < Math.Min(ToolbarSlots, inventory.Count); i++)
            if (inventory[i] is { } f && BgEatPrompt.CandidateMiss(f) is null)
                return i;
        return -1;
    }
}

/// <summary>Published combat facts (ui.context "combat"): monsters nearest first, at most <see cref="MaxMonsters"/>.</summary>
public static class CombatFacts
{
    public const int MaxMonsters = 8;

    public static float Distance(MonsterView m, float playerX, float playerY)
    {
        float dx = m.TileX - playerX, dy = m.TileY - playerY;
        return (float)Math.Sqrt(dx * dx + dy * dy);
    }

    public static float Nearest(IReadOnlyList<MonsterView> monsters, float playerX, float playerY)
    {
        float best = -1f;
        foreach (MonsterView m in monsters)
        {
            if (m.Health <= 0)
                continue;
            float d = Distance(m, playerX, playerY);
            if (best < 0 || d < best)
                best = d;
        }
        return best;
    }

    /// <summary>{id, type, tile [x, y], hp, max_hp, distance (0.1 tile), invincible} for the nearest living monsters.</summary>
    public static object[] MonstersWire(IReadOnlyList<MonsterView> monsters, float playerX, float playerY) =>
        monsters.Where(m => m.Health > 0)
            .Select(m => (m, d: Distance(m, playerX, playerY)))
            .OrderBy(t => t.d).ThenBy(t => t.m.Id, StringComparer.Ordinal)
            .Take(MaxMonsters)
            .Select(t => (object)new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["id"] = t.m.Id, ["type"] = t.m.Type,
                ["tile"] = new[] { (int)Math.Floor(t.m.TileX), (int)Math.Floor(t.m.TileY) },
                ["hp"] = t.m.Health, ["max_hp"] = t.m.MaxHealth,
                ["distance"] = Math.Round(t.d, 1), ["invincible"] = t.m.Invincible,
                ["hidden"] = t.m.Hidden,   // ADR 153: alive but not hittable now (a burrowed Duggy)
            })
            .ToArray();
}

/// <summary>One tile of a charged release's area (read-only probe facts).</summary>
public readonly record struct ChargeTileFacts(int X, int Y, bool OnMap, bool HasObject, bool HasTerrain, bool HoeDirt, bool Crop, bool Watered,
    bool Tillable, bool Villager, bool Refill);

/// <summary>
/// Faced-tile + area guard of reflex_charge (brief §2): the hoe needs a tillable faced tile and an area without objects
/// (Hoe.DoFunction calls performToolAction on objects: a crafted item would be picked up), without crops and without any
/// terrain feature but empty HoeDirt; the can must not face a refill tile (it refills instead of charging, T:646-668) and
/// must hold water; a villager anywhere in the area refuses (talk redirect / NPC). null = admitted.
/// </summary>
public static class ChargeAreaGuard
{
    public static string? Reject(string toolKind, IReadOnlyList<ChargeTileFacts>? area, int? waterLeft)
    {
        if (area is null || area.Count == 0)
            return "area_unreadable";
        ChargeTileFacts faced = area[0];
        if (area.Any(t => t.Villager))
            return "villager_in_area";
        if (toolKind == ChargeToolKind.Hoe)
        {
            if (!faced.Tillable)
                return "faced_tile_not_tillable";
            if (area.Any(t => t.HasObject))
                return "object_in_area";
            if (area.Any(t => t.Crop))
                return "crop_in_area";
            if (area.Any(t => t.HasTerrain && !t.HoeDirt))
                return "terrain_in_area";
            return null;
        }
        if (toolKind == ChargeToolKind.WateringCan)
        {
            if (faced.Refill)
                return "faced_tile_refill";
            if (waterLeft is not int left || left <= 0)
                return "can_empty";
            return null;
        }
        return "not_chargeable";
    }

    /// <summary>Tiles the release changed: newly tilled (hoe) or newly watered (can) tiles of the sampled area.</summary>
    public static int Changed(string toolKind, IReadOnlyList<ChargeTileFacts> before, IReadOnlyList<ChargeTileFacts> after)
    {
        int n = 0;
        foreach (ChargeTileFacts a in after)
        {
            ChargeTileFacts? b = before.Cast<ChargeTileFacts?>().FirstOrDefault(t => t!.Value.X == a.X && t.Value.Y == a.Y);
            if (b is not { } was)
                continue;
            if (toolKind == ChargeToolKind.Hoe ? a.HoeDirt && !was.HoeDirt : a.Watered && !was.Watered)
                n++;
        }
        return n;
    }
}

/// <summary>Facts for the reflex_charge admission.</summary>
public sealed record ChargeAdmissionFacts(
    bool FlagOn,
    bool PadPathConfigured,
    bool WindowActive,
    bool WorldReady,
    bool MenuOpen,
    bool EventUp,
    bool PlayerFree,
    string ToolKind,
    int UpgradeLevel,
    bool Reaching,
    bool UsingTool,
    bool RuntimeBusy,
    float Stamina,
    int TargetLevel,
    string? AreaRefusal,
    string? PadModeRefusal,
    string? CursorHiddenRefusal);

/// <summary>reflex_charge admission (WS-G5): first failing term, or null. Axe / Pickaxe / basic tools are refused (never charge).</summary>
public static class ChargeReflexAdmission
{
    public const string Prefix = "REFLEX_CHARGE";

    public static string? Refuse(ChargeAdmissionFacts f)
    {
        if (!f.FlagOn) return Prefix + ":flag_off";
        if (!f.PadPathConfigured) return Prefix + ":pad_path_off";
        if (f.WindowActive) return Prefix + ":window_active";
        if (!f.WorldReady) return Prefix + ":world_not_ready";
        if (f.MenuOpen) return Prefix + ":menu_open";
        if (f.EventUp) return Prefix + ":event";
        if (!ChargeReflexMachine.IsChargeable(f.ToolKind, f.UpgradeLevel)) return Prefix + ":" + ReflexContracts.ReasonNotChargeable;
        if (f.UsingTool) return Prefix + ":using_tool";
        if (!f.PlayerFree) return Prefix + ":player_not_free";
        if (f.RuntimeBusy) return Prefix + ":runtime_busy";
        if (f.Stamina < 1f) return Prefix + ":" + ReflexContracts.ReasonStaminaLow;
        if (f.TargetLevel < 1 || f.TargetLevel > ChargeReflexMachine.Cap(f.UpgradeLevel, f.Reaching)) return Prefix + ":" + ReflexContracts.ReasonAboveCap;
        if (f.AreaRefusal is { } area) return Prefix + ":" + area;
        if (f.PadModeRefusal is { } mode) return mode;
        if (f.CursorHiddenRefusal is { } cursor) return cursor;
        return null;
    }
}
