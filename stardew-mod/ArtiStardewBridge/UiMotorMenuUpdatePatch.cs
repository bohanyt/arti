using ArtiStardewBridge.Protocol;
using HarmonyLib;
using Microsoft.Xna.Framework;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// ADR 250 (fix145A): while her UI motor frame is seated in the game update, vanilla <c>Game1.updateActiveMenu</c>
/// takes its own inactive branch, whatever the window focus: the deepest child menu's <c>update(time)</c> when that
/// menu is active, then return (G1:4842-4849, recorded in PROBE_GAMEPAD_2026-10-08.md item 6). Without her frame
/// seated the vanilla method runs unchanged, so the owner's own input in her window between her presses is the game's.
/// No lambdas, no menu knowledge; the decision is <see cref="UiMotorFrontPath.For"/>.
/// </summary>
internal static class UiMotorMenuUpdatePatch
{
    static IUiMotorSeatState? seat;

    internal static bool Installed { get; private set; }

    /// <summary>Menu updates that took the inactive branch for her frame (both states), and those with her window active.</summary>
    internal static long InactiveFrames { get; private set; }
    internal static long FrontFrames { get; private set; }

    internal static void Install(string modId, IUiMotorSeatState motorSeat)
    {
        var method = AccessTools.DeclaredMethod(typeof(Game1), "updateActiveMenu", new[] { typeof(GameTime) })
            ?? throw new MissingMethodException("Game1.updateActiveMenu(GameTime)");
        seat = motorSeat;
        new Harmony(modId + ".UiMotorMenuUpdate").Patch(method,
            prefix: new HarmonyMethod(typeof(UiMotorMenuUpdatePatch), nameof(Prefix)) { priority = Priority.First });
        Installed = true;
    }

    // false = skip the original for this frame only (its inactive branch is reproduced here, nothing else).
    // __0: the original's GameTime by position, whatever vanilla names the parameter.
    static bool Prefix(GameTime __0)
    {
        if (UiMotorFrontPath.For(seat?.Seated == true) != UiMotorFrontPath.MenuUpdate.InactiveBranch)
            return true;
        InactiveFrames++;
        if (Game1.game1?.IsActive == true)
            FrontFrames++; // telemetry for the bench only; the branch is the same in both states
        IClickableMenu? menu = Game1.activeClickableMenu;
        while (menu?.GetChildMenu() is { } child)
            menu = child;
        if (menu is not null && menu.IsActive())
            menu.update(__0);
        return false;
    }
}
