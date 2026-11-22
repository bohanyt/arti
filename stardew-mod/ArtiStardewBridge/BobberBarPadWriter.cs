using Microsoft.Xna.Framework.Input;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// Primitives v2 WS-G4 (PLAN §1.5 c): the ONLY production writer of a non-neutral <c>Game1.oldPadState</c>. BobberBar.update
/// reads its input only from <c>Game1.old*</c> (BobberBar.cs:422-423) and, while the farmhand is inactive with a menu open,
/// those are never refreshed (G1:4842-4849, 5119-5121), so the fishing reflex drives the bar by writing a state whose only
/// button is X (bar up). The write is refused unless the deepest active menu is the BobberBar instance the reflex captured,
/// the window is inactive and pad controls are on (the runner checked the lease and its Bar phase through
/// <c>BobberBarWriteGuard</c> first). Neutral writes go through <see cref="PadHistoryHygiene.WriteNeutral"/>. Live PRB-1:
/// H1/H2 confirmed (updButtonPressed equals the written state on every update after the fade-in).
/// </summary>
internal static class BobberBarPadWriter
{
    public static int Writes { get; private set; }

    public static int Refusals { get; private set; }

    public static bool WriteX(BobberBar captured)
    {
        if (!Guard(captured))
        {
            Refusals++;
            return false;
        }
        Game1.oldPadState = new GamePadState(new GamePadThumbSticks(), new GamePadTriggers(), new GamePadButtons(Buttons.X), new GamePadDPad());
        Writes++;
        return true;
    }

    /// <summary>PLAN §1.5 c (ii): deepest menu is the captured BobberBar, the window is inactive, pad controls on.</summary>
    private static bool Guard(BobberBar captured)
    {
        IClickableMenu? deepest = ModEntry.DeepestReceiver(Game1.activeClickableMenu);
        return deepest is BobberBar bar && ReferenceEquals(bar, captured)
            && Game1.game1?.IsActive != true && Game1.options?.gamepadControls == true;
    }
}
