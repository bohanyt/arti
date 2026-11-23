using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;

namespace ArtiHostHelper;

/// <summary>Observes the native touch action without suppressing or altering any game behavior.</summary>
internal static class HostBedTouch
{
    internal static void Install(string modId, Func<GameLocation, Vector2, BedApproach?> begin,
        Action<BedApproach, GameLocation, Vector2> capture)
    {
        var method = AccessTools.Method(typeof(GameLocation), nameof(GameLocation.performTouchAction),
            new[] { typeof(string[]), typeof(Vector2) }) ?? throw new MissingMethodException("GameLocation.performTouchAction");
        new Harmony(modId + ".BedTouch").Patch(method,
            prefix: new HarmonyMethod(typeof(HostBedTouch), nameof(Before)) { priority = Priority.Last },
            postfix: new HarmonyMethod(typeof(HostBedTouch), nameof(After)) { priority = Priority.First });
        Begin = begin;
        Capture = capture;
    }

    static Func<GameLocation, Vector2, BedApproach?>? Begin;
    static Action<BedApproach, GameLocation, Vector2>? Capture;

    internal static void Before(GameLocation __instance, string[] __0, Vector2 __1, out BedApproach? __state)
    {
        __state = null;
        if (__0 is not { Length: 1 } || __0[0] != "Sleep") return;
        try { __state = Begin?.Invoke(__instance, __1); }
        catch { /* An unreadable touch grants no ownership. */ }
    }

    internal static void After(GameLocation __instance, Vector2 __1, BedApproach? __state)
    {
        if (__state is null) return;
        try { Capture?.Invoke(__state, __instance, __1); }
        catch { /* A failed observation leaves the native question alone. */ }
    }
}
