using StardewModdingAPI;
using StardewValley;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    long giftSafetyPublished;
    string? giftSafetyState;
    void PublishHostGiftSafety()
    {
        if (!Context.IsWorldReady || Context.IsMainPlayer || Game1.player is not { } p) return;
        // Ownership covers planning pauses between native inputs as well as movement.
        // An entire armed session is busy: no guessed idle interval grants a gift.
        bool busy = this.actionOwner.IsArmed || this.commandInbox.ExecutionSlot.InputBusy
            || this.ReflexBusy || Game1.activeClickableMenu is not null || Game1.dialogueUp
            || Game1.eventUp || p.UsingTool || Game1.currentLocation is not Farm;
        long now = NowUnixMs();
        string state = busy ? "busy" : "released";
        if (state == this.giftSafetyState && now-this.giftSafetyPublished < 1000) return;
        this.Helper.Multiplayer.SendMessage(new HostGiftSafetyMessage(Game1.uniqueIDForThisGame,
            HostGiftSafety.Format(busy, now)), HostGiftSafety.MessageType,
            modIDs: new[] { HostGiftSafety.HelperModId }, playerIDs: new[] { Game1.MasterPlayer.UniqueMultiplayerID });
        this.giftSafetyState = state; this.giftSafetyPublished = now;
    }
}
