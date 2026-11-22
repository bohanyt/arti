using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;

namespace ArtiStardewBridge;

/// <summary>Read-only farmer collision at the route's tile-centred bounding box.
/// Placement occupancy/isTilePassable are not movement oracles (GameLocation.cs:2493,2898).
/// Never move the farmer to probe a cell; retain native current-overlap exemptions.
/// MAP_INDEPENDENCE_NAV: the probe is the exact call the farmer's own movement makes
/// (Farmer.cs:8600: the six-argument virtual), so every location's override and its
/// extra blockers answer for every map. Its collision side effects are suppressed in
/// GameLocation's body (the only place they live, GameLocation.cs:2493-2860) for the
/// duration of the probe only; no per-location binding or list.</summary>
internal static class NativeRouteCell
{
    [ThreadStatic] private static int probing;
    private static bool installed;

    /// <summary>Patch GameLocation's ten-argument body once: while (and only while) a probe of this class is on
    /// the stack, skipCollisionEffects is forced true. Overrides that drop the flag (FarmHouse.cs:415,
    /// Forest.cs:699, IslandLocation.cs:146, IslandNorth.cs:239, VolcanoDungeon.cs:2065) or call the six-argument
    /// base (Mountain.cs:352, Beach.cs:397, ...) all end in this body. Without the patch the legacy class stays.</summary>
    internal static bool Install(string modId)
    {
        installed = false;
        if (BackgroundMoveRuntime.GameModuleVersionId() != BackgroundMoveRuntime.PinnedGameMvid)
            throw new InvalidOperationException("nav_walkability_game_mvid");
        var method = typeof(GameLocation).GetMethods(System.Reflection.BindingFlags.Instance
                | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.DeclaredOnly)
            .Single(m => m.Name == "isCollidingPosition" && m.GetParameters().Length == 10
                && m.GetParameters()[9].ParameterType == typeof(bool));
        new Harmony(modId + ".NativeRouteCell").Patch(method,
            prefix: new HarmonyMethod(typeof(NativeRouteCell), nameof(QuietProbe)) { priority = Priority.First });
        return installed = true;
    }

    /// <summary>Harmony prefix (argument 9 = skipCollisionEffects). A no-op outside a probe.</summary>
    internal static void QuietProbe(ref bool __9)
    {
        if (probing > 0) __9 = true;
    }

    internal static char NavChar(GameLocation location, Farmer player, int x, int y, char legacy)
        => Protocol.WorldExtras.NativeNavChar(legacy, legacy is 'x' or 'z' ? null : TryWalkable(location, player, x, y));

    internal static string? StandMiss(GameLocation location, Farmer player, int x, int y, string? legacy)
        => Protocol.WorldExtras.NativeStandMiss(legacy,
            legacy == Protocol.WorldExtras.SleepRowMiss ? null : TryWalkable(location, player, x, y));

    internal static bool IsWalkable(GameLocation location, Farmer player, int x, int y)
        => TryWalkable(location, player, x, y) == true;

    private static bool? TryWalkable(GameLocation location, Farmer player, int x, int y)
    {
        try
        {
            if (!ReferenceEquals(player.currentLocation, location) || x < 0 || y < 0
                || !location.isTileOnMap(new Vector2(x, y))) return false;
            if (!installed) return null; // effects could not be suppressed: preserve legacy nav semantics
            Rectangle box = player.GetBoundingBox();
            if (box.Width <= 0 || box.Height <= 0 || box.Width > 64 || box.Height > 64) return false;
            box.X = x * 64 + 32 - box.Width / 2;
            box.Y = y * 64 + 32 - box.Height / 2;
            // Native Event.checkForCollision can open the festival exit dialogue out of bounds.
            // Keep every sampled corner strictly inside the map before invoking it.
            if (!location.isTileOnMap(new Vector2(box.Right / 64, box.Bottom / 64))) return false;
            probing++;
            try
            {
                return !location.isCollidingPosition(box, Game1.viewport, isFarmer: true,
                    damagesFarmer: 0, glider: false, character: player);
            }
            finally { probing--; }
        }
        catch { return false; }
    }
}
