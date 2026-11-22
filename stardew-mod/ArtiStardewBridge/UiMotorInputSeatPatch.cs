using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;

namespace ArtiStardewBridge;

/// <summary>
/// Motor-only seat window for the generic UI motor frame. Installed SMAPI 4.5.2 casts
/// <c>Game1.input</c> to its sealed <c>SInputState</c> in <c>SCore.OnPlayerInstanceUpdating</c>
/// (<c>ApplyOverrides</c> after UpdateTicking; SCore.cs:1143) and in every IInputHelper read, so a
/// staged frame may occupy the slot only inside vanilla <c>Game1.Update</c>. A prefix (last, closest
/// to the body) seats it; a finalizer (first, on success and on exception) gives the slot back
/// before any other finalizer or SMAPI code. No observation, no input injection of its own, no
/// skip of the original, no exception replacement.
/// </summary>
internal static class UiMotorInputSeatPatch
{
    static Action<Game1>? enter;
    static Action<Game1, bool>? exit;

    /// <summary>ADR 250 review B4: true once the seat hook is in place (the menu path needs it and the menu hook).</summary>
    internal static bool Installed { get; private set; }

    internal static void Install(string modId, Action<Game1> onEnter, Action<Game1, bool> onExit)
    {
        var method = AccessTools.DeclaredMethod(typeof(Game1), "Update", new[] { typeof(GameTime) })
            ?? throw new MissingMethodException("Game1.Update(GameTime)");
        enter = onEnter; exit = onExit;
        new Harmony(modId + ".UiMotorInputSeat").Patch(method,
            prefix: new HarmonyMethod(typeof(UiMotorInputSeatPatch), nameof(Prefix)) { priority = Priority.Last },
            finalizer: new HarmonyMethod(typeof(UiMotorInputSeatPatch), nameof(Finalizer)) { priority = Priority.First });
        Installed = true;
    }

    // Void: never skips the original.
    static void Prefix(Game1 __instance)
    {
        try { enter?.Invoke(__instance); } catch { /* never block the game update */ }
    }

    // Void: never suppresses or replaces the update's own exception.
    static void Finalizer(Game1 __instance, Exception? __exception)
    {
        try { exit?.Invoke(__instance, __exception is null); } catch { }
    }
}
