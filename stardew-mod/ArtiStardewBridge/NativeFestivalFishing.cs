using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Tools;

namespace ArtiStardewBridge;

/// <summary>Internal timed ice-fishing pin. Menus/UsingTool belong to the existing runner;
/// this pin never admits an ordinary tool, movement, or model-supplied event token.</summary>
internal static class NativeFestivalFishing
{
    private static readonly MenuInstanceTokenTracker events = new(), scripts = new(), locations = new(), players = new(), rods = new();
    public static string? Token()
    {
        Event? ev = Game1.CurrentEvent;
        Farmer? p = Game1.player;
        if (!Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen || !Game1.eventUp
            || ev is not { isFestival: true, eventSwitched: true, playerControlSequence: true, festivalTimer: > 0 }
            || ev.id != "festival_winter8" || ev.playerControlSequenceID != "iceFishing"
            || !ReferenceEquals(ev, Game1.currentLocation?.currentEvent) || !ReferenceEquals(p?.currentLocation, Game1.currentLocation)
            || ev.eventCommands is not { Length: > 0 } script || ev.CurrentCommand < 0 || ev.CurrentCommand >= script.Length
            || script[ev.CurrentCommand] != "playerControl iceFishing"
            || p?.TemporaryItem is not FishingRod rod || !ReferenceEquals(p.CurrentTool, rod)
            || Game1.currentMinigame is not null || Game1.farmEvent is not null
            || Game1.paused || Game1.HostPaused || Game1.globalFade || Game1.fadeToBlack || Game1.freezeControls
            || Game1.panMode || Game1.locationRequest is not null || Game1.keyboardDispatcher?.Subscriber is not null)
            return null;
        return "iff:" + UiKeyTargetToken.Hash(System.Text.Json.JsonSerializer.Serialize(new {
            ev.id, ev.CurrentCommand, ev.playerControlSequenceID, raw = script[ev.CurrentCommand],
            ev.isFestival, ev.eventSwitched, timerPositive = true,
            ev = events.Observe(ev), script = scripts.Observe(script), location = locations.Observe(Game1.currentLocation),
            player = players.Observe(p), rod = rods.Observe(rod) }));
    }
}

internal sealed partial class BackgroundMoveRuntime
{
    internal const string FestivalTimerRelease = "FESTIVAL_TIMER_END";

    // A contained policy is no longer Armed, so Busy/empty overrides alone
    // cannot certify festival cleanup. Keep the actual permanent decision.
    internal string? FestivalContainmentFault => this.policy?.Decision?.FailFastMessage;
    internal int FestivalFaultPanics { get; private set; }
    internal string? FestivalLastFaultPanicReason { get; private set; }
    // A phase-change terminal may have an uncertain world effect and still
    // carry independently proved neutral input. Never infer this from an
    // empty press ledger or from RELEASED_BY_GAME alone.
    internal bool FestivalReleaseConfirmed => !this.Busy && this.FestivalContainmentFault is null
        && this.policy?.State is null or BgContainmentState.Disarmed or BgContainmentState.DisarmedVerifiedNeutral
        && (!this.latchEverInstalled || this.latchRestoredExact) && MovementCount() == 0;

    internal bool FestivalFishingValid(in MenuStepCommandRequest req) => this.eventControlEnabled() && this.PadPathOn
        && MenuStepCommandInbox.IsReflexFish(req) && req.EventToken is { } token
        && token == NativeFestivalFishing.Token();

    internal bool FestivalMoveValid(in MenuStepCommandRequest req) => this.eventControlEnabled() && this.PadPathOn
        && IsBackgroundMoveRequest(req) && req.Ticks is >= 1 and <= EventControlMove.MaxTicks
        && req.EventToken is { } token && token == NativeFestivalFishing.Token()
        && Game1.player is { UsingTool: false, CanMove: true } && Game1.activeClickableMenu is null && !Game1.dialogueUp;

    private bool PinnedEventValid(in MenuStepCommandRequest req) => this.EventMoveValid(req)
        || this.FestivalFishingValid(req) || this.FestivalMoveValid(req);
}
