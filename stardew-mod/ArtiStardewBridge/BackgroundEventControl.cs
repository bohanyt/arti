using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

/// <summary>Game-thread native event pins shared by publication, admission and every frame gate.</summary>
internal static class NativeEventControl
{
    internal static bool GenericControlEnabled;
    private static readonly MenuInstanceTokenTracker instances = new();
    private static readonly MenuInstanceTokenTracker locations = new();
    private static readonly MenuInstanceTokenTracker scripts = new();

    // 1.6.15 Event.cs:11602-11615 increments past spring13's final playerControl eggHunt.
    // ParseCommands (:11728-11732) adds no sentinel; native dispatch (:5302) clamps to last.
    // Only that exact reviewed terminal state may use the effective final raw command.
    internal static string? EffectiveRaw(Event? ev)
    {
        if (ev?.eventCommands is not { Length: > 0 } commands || ev.CurrentCommand < 0) return null;
        if (ev.CurrentCommand < commands.Length) return commands[ev.CurrentCommand];
        string raw = commands[^1];
        return ev.isFestival && ev.playerControlSequence
            && EventControlMove.FestivalReviewed(ev.id, ev.playerControlSequenceID, raw,
                ev.eventSwitched, ev.festivalTimer, ev.CurrentCommand, commands.Length) ? raw : null;
    }

    public static string? Token() => Compute(seated: false);

    /// <summary>The same free-roam token while she sits on a seat of this festival map before its main
    /// event. It authorizes only the festival_stand control; <see cref="Token"/> stays null while she sits.</summary>
    public static string? SeatedToken() => Compute(seated: true);

    private static string? Compute(bool seated)
    {
        Event? ev = Game1.CurrentEvent as Event;
        string? instance = instances.Observe(ev);
        string? locationInstance = locations.Observe(Game1.currentLocation);
        string? raw = EffectiveRaw(ev);
        string? command = raw?.Split(' ')[0];
        if (raw is null || !Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen || Game1.player?.CanMove != true
            || Game1.player.IsSitting() != seated
            || seated && !(ev is { isFestival: true, eventSwitched: false } && Game1.player.sittingFurniture is MapSeat seat
                && !Game1.player.isStopSitting && Game1.currentLocation?.mapSeats.Contains(seat) == true)
            || Game1.paused || Game1.HostPaused || Game1.globalFade || Game1.fadeToBlack || Game1.freezeControls
            || Game1.panMode || Game1.locationRequest is not null
            || !(EventControlMove.Reviewed(Game1.eventUp,
                ev is not null && ReferenceEquals(ev, Game1.currentLocation?.currentEvent)
                    && ReferenceEquals(Game1.player?.currentLocation, Game1.currentLocation),
                ev?.isFestival == true, ev?.playerControlSequence == true, ev?.playerControlSequenceID, command,
                Game1.activeClickableMenu is not null || Game1.dialogueUp,
                Game1.currentMinigame is not null, Game1.farmEvent is not null,
                Game1.keyboardDispatcher?.Subscriber is not null)
                || GenericControlEnabled && Game1.eventUp && ev is { playerControlSequence: true }
                    && ReferenceEquals(ev, Game1.currentLocation?.currentEvent)
                    && ReferenceEquals(Game1.player?.currentLocation, Game1.currentLocation)
                    && Game1.activeClickableMenu is null && !Game1.dialogueUp && Game1.currentMinigame is null
                    && Game1.farmEvent is null && Game1.keyboardDispatcher?.Subscriber is null
                || Game1.eventUp && ev is { isFestival: true, playerControlSequence: true }
                    && ReferenceEquals(ev, Game1.currentLocation?.currentEvent)
                    && ReferenceEquals(Game1.player?.currentLocation, Game1.currentLocation)
                    && EventControlMove.FestivalReviewed(ev.id, ev.playerControlSequenceID, raw, ev.eventSwitched, ev.festivalTimer,
                        ev.CurrentCommand, ev.eventCommands.Length)
                    && Game1.activeClickableMenu is null && !Game1.dialogueUp && Game1.currentMinigame is null
                    && Game1.farmEvent is null && Game1.keyboardDispatcher?.Subscriber is null))
            return null;
        return "ecm:" + UiKeyTargetToken.Hash(System.Text.Json.JsonSerializer.Serialize(
            new { instance, locationInstance, location = Game1.currentLocation?.NameOrUniqueName,
                ev!.id, ev.CurrentCommand, ev.playerControlSequenceID, raw, ev.isFestival, ev.eventSwitched,
                script = scripts.Observe(ev.eventCommands), count = ev.eventCommands.Length,
                timerPositive = ev.festivalTimer > 0, targetTile = new[] { ev.playerControlTargetTile.X, ev.playerControlTargetTile.Y } }));
    }
}

internal sealed partial class BackgroundMoveRuntime
{
    private readonly Func<bool> eventControlEnabled;

    private BgActionTarget? eventAdmissionTarget;
    internal bool EventActionValid(BgActionTarget? target = null, bool? consumed = null)
    {
        try { return this.eventControlEnabled() && this.PadPathOn
            && (target ?? this.eventAdmissionTarget ?? (this.request is not null ? this.holdTarget : null))
                ?.EventScopeValid?.Invoke(consumed ?? (this.eventAdmissionTarget is null && this.holdPlan?.ConsumedH > 0)) == true; }
        catch { return false; }
    }

    // A composite route can withdraw its exact target between UpdateTicking and
    // the native consumer hook. This adds a guard to the existing single writer.
    internal Func<MenuStepCommandRequest,bool>? EventRouteAuthority { get; set; }
    internal Func<MenuStepCommandRequest,bool>? NativeRouteAuthority { get; set; }
    // Read-only seam: do not project another movement on an N/R consumer. A
    // not-yet-armed step is checked at admission, then again at every actual H.
    internal bool NativeRouteMovementConsumer => this.request is null || this.plan is null
        || this.plan.EffectiveNext(this.releaseReason is not null) == BgFrameKind.H;
    // CommitMoving/CommitPadMoving run after the admitted parent callback, before
    // Farmer.Update. N0 has no H; R/N1 have consumed R. Reset each UpdateTicking.
    // A skipped/faulted callback or a later runtime withdrawal grants no motion
    // from retained movementDirections, even if the house's own pins still match.
    internal bool NativeRouteHasH(string requestId) => this.request is { } req && req.RequestId==requestId
        && this.phase==Phase.Moving && this.substitutedThisTick && !this.dispatchImpossible
        && this.releaseReason is null && this.plan is { ConsumedH:>0, RConsumed:false };
    private bool NativeRouteValid(MenuStepCommandRequest req)
    { try { return this.NativeRouteAuthority?.Invoke(req) != false; } catch { return false; } }
    private bool EventRouteValid(in MenuStepCommandRequest req)
    {
        try { return this.EventRouteAuthority?.Invoke(req) != false; } catch { return false; }
    }

    // A request pin alone grants nothing: re-read the actual event, instance, command and sequence.
    internal bool EventMoveValid(in MenuStepCommandRequest req) => this.eventControlEnabled() && this.PadPathOn
        && IsBackgroundMoveRequest(req) && req.Ticks >= 1
        && req.Ticks <= (this.EventRouteAuthority is not null ? EventControlAction.SegmentTicks : EventControlMove.MaxTicks)
        && EventControlMove.TokenShape(req.EventToken) && req.EventToken == NativeEventControl.Token()
        && this.EventRouteValid(req);

    private BgAdmissionFacts EventMoveFacts(BgAdmissionFacts facts, MenuStepCommandRequest? req = null)
    {
        MenuStepCommandRequest? pin = req ?? this.request;
        if (pin is { } fishing && (this.FestivalFishingValid(fishing) || this.FestivalMoveValid(fishing)))
            return facts with { EventOrMinigame = false };
        if (this.EventActionValid())
            return facts with { EventOrMinigame = false };
        return pin is { EventToken: not null } scoped
            ? facts with { EventOrMinigame = !this.EventMoveValid(scoped) } : facts;
    }

    internal bool EventMoveInputPending => this.plan is { } move && move.ConsumedH < move.TargetHFrames;
}
