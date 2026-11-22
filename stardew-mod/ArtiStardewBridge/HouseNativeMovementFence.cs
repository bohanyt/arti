using System.Reflection;
using HarmonyLib;
using StardewValley;

namespace ArtiStardewBridge;

/// <summary>Veto only: no input, direction, position or warp writes. The route
/// remains owned until the existing N/R consumer proves release.</summary>
internal static class HouseNativeMovementFence
{
    internal static bool Install(string modId, Func<Farmer,int,float,float,bool> permit)
    {
        if (typeof(Game1).Module.ModuleVersionId != new Guid("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf"))
            throw new InvalidOperationException("house_movement_game_mvid");
        var method = typeof(Farmer).GetMethods(BindingFlags.Instance | BindingFlags.NonPublic)
            .Single(m => m.Name == "MovePositionImpl" && m.DeclaringType == typeof(Farmer)
                && m.ReturnType == typeof(bool) && m.GetParameters().Select(p => p.ParameterType)
                    .SequenceEqual(new[] { typeof(int), typeof(float), typeof(float),
                        typeof(Microsoft.Xna.Framework.GameTime), typeof(xTile.Dimensions.Rectangle) }));
        new Harmony(modId + ".HouseNativeMovementFence").Patch(method,
            prefix: new HarmonyMethod(typeof(HouseNativeMovementFence), nameof(Before)) { priority = Priority.Last });
        permits = permit;
        return true;
    }

    private static Func<Farmer,int,float,float,bool>? permits;

    // Source: Game1.cs:4288,4314,9470; Farmer.cs:7533,7761,8542-8544,
    // 8583-8598. This callback runs AFTER buffs.Update and the temporary-speed
    // reset, and BEFORE nextPosition/warp contact. __1/__2 retain the actual
    // displacement calculated before that reset; both footprints are checked.
    internal static bool Before(Farmer __instance, int __0, float __1, float __2, ref bool __result)
        => Evaluate(permits,__instance,__0,__1,__2,ref __result);

    internal static bool Evaluate(Func<Farmer,int,float,float,bool>? permit, Farmer player,
        int direction, float dx, float dy, ref bool result)
    {
        bool allowed;
        try { allowed = permit?.Invoke(player,direction,dx,dy) != false; }
        catch { allowed = false; }
        if (allowed) return true;
        result = true; // fully handled: no original movement or later direction this call
        return false;
    }
}
