using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    /// <summary>Operator console only. No inbox primitive, catalog or agent path.</summary>
    private void OnStageEventCommand(string command, string[] args)
    {
        string? refusal = args.Length != 1 ? "usage: arti_stage_event question|control|festival-start" : this.StageEvent(args[0]);
        if (refusal is not null)
            this.Monitor.Log($"{command} REFUSED {refusal}", LogLevel.Warn);
    }

    private void PollOperatorEventStaging(uint ticks)
    {
        if (ticks % 60 != 0)
            return;
        try
        {
            var receipt = OperatorEventStaging.Poll(
                Path.Combine(this.Helper.DirectoryPath, OperatorEventStaging.DirectoryName),
                this.config.OperatorEventStagingEnabled,
                Context.IsWorldReady, Context.IsMultiplayer, Context.IsMainPlayer, Context.IsSplitScreen,
                this.StageEvent, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
            receipt ??= OperatorEventStaging.Poll(
                Path.Combine(this.Helper.DirectoryPath, OperatorEventStaging.FestivalDirectoryName),
                this.config.OperatorEventStagingEnabled,
                Context.IsWorldReady, Context.IsMultiplayer, Context.IsMainPlayer, Context.IsSplitScreen,
                this.StageEvent, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), festivalStart: true,
                festivalFreeRoam: FestivalStagingFreeRoam());
            if (receipt is not null)
                this.Monitor.Log($"STAGING file id={receipt.id} success={receipt.success} reason={receipt.reason} consumed={receipt.consumed_file}",
                    receipt.success ? LogLevel.Info : LogLevel.Warn);
        }
        catch (Exception ex)
        { this.Monitor.Log("STAGING file fault (no automatic retry): " + ex, LogLevel.Error); }
    }

    // Console and operator file share EVERY native/lease/STOP gate. Success means startup, not witness proof.
    private string? StageEvent(string verb)
    {
        if (verb == "festival-start") return this.StageFestivalStart();
        const string command = "arti_stage_event";
        string? refusal = !this.config.OperatorEventStagingEnabled ? "staging_gate_off"
            : !OperatorEventStaging.VerbAllowed(verb) ? "usage: arti_stage_event question|control"
            : OperatorEventStaging.IdentityRefusal(Context.IsWorldReady, Context.IsMultiplayer, Context.IsMainPlayer, Context.IsSplitScreen) is { } identity ? identity
            : !Context.IsPlayerFree || !Context.CanPlayerMove || Game1.eventUp || Game1.CurrentEvent is not null
                || Game1.currentLocation?.currentEvent is not null || Game1.eventOver ? "not_free"
            : Game1.activeClickableMenu is not null || Game1.dialogueUp || Game1.currentMinigame is not null
                || Game1.farmEvent is not null || Game1.keyboardDispatcher?.Subscriber is not null
                || Game1.currentLocation?.afterQuestion is not null ? "receiver_busy"
            : this.MutationInflight || this.backgroundMove?.Busy == true || this.backgroundMove?.PressOverrideOutstanding == true
                || this.padAdapter?.Owner.RestorePending == true || this.padAdapter?.CleanupFault is { Length: > 0 } ? "input_busy"
            : Game1.globalFade || Game1.fadeToBlack || Game1.freezeControls || Game1.panMode || Game1.paused || Game1.HostPaused
                || Game1.locationRequest is not null ? "transition"
            : verb == "control" && Game1.currentLocation?.Name != "Beach" ? "control_needs_Beach"
            : null;
        if (refusal is not null)
            return refusal;
        if (this.RejectUnleasedMutation(command, out CommandRejectReason ownerReason))
            return "unleased_mutation_refused:" + ownerReason;
        string script = verb == "question"
            ? $"none/follow/farmer {(int)Game1.player.Tile.X} {(int)Game1.player.Tile.Y} 2/question fork1 \"Choose a branch#First#Second\"/end"
            : "none/follow/farmer 50 8 1/playerControl haleyBeach/end";
        // Native startEvent, same entry as DebugCommands.EventScript's same-location branch; no direct UI fabrication.
        Game1.currentLocation!.startEvent(new Event(script));
        this.Monitor.Log($"STAGING {command} {verb} player={Game1.player.UniqueMultiplayerID} "
            + $"location={Game1.currentLocation.NameOrUniqueName} script={script} (not witness evidence)", LogLevel.Info);
        return null;
    }

    private static bool FestivalStagingFreeRoam()
    {
        Event? ev = Game1.CurrentEvent;
        string? raw = ev?.eventCommands is { } commands && ev.CurrentCommand >= 0 && ev.CurrentCommand < commands.Length
            ? commands[ev.CurrentCommand] : null;
        return Context.IsWorldReady && Context.IsMultiplayer && Context.IsMainPlayer && !Context.IsSplitScreen
            && Game1.IsServer && !Game1.HasDedicatedHost && Game1.player is { CanMove: true, UsingTool: false }
            && Game1.eventUp && ev is { isFestival: true, playerControlSequence: true }
            && ReferenceEquals(Game1.player.currentLocation, Game1.currentLocation)
            && ReferenceEquals(ev, Game1.currentLocation?.currentEvent)
            && ev.id is "festival_spring13" or "festival_winter8"
            && EventControlMove.FestivalReviewed(ev.id, ev.playerControlSequenceID, raw?.Split(' ')[0], ev.eventSwitched, ev.festivalTimer)
            && !ev.eventSwitched && ev.festivalTimer <= 0;
    }

    private string? StageFestivalStart()
    {
        if (!this.config.OperatorEventStagingEnabled) return "staging_gate_off";
        if (!FestivalStagingFreeRoam()) return "host_at_free_roam_festival_required";
        if (Game1.activeClickableMenu is not null || Game1.dialogueUp || Game1.currentMinigame is not null
            || Game1.keyboardDispatcher?.Subscriber is not null || Game1.paused || Game1.HostPaused
            || Game1.globalFade || Game1.fadeToBlack || Game1.freezeControls || Game1.locationRequest is not null)
            return "receiver_or_transition_busy";
        if (this.MutationInflight || this.backgroundMove?.Busy == true || this.backgroundMove?.PressOverrideOutstanding == true
            || this.padAdapter?.Owner.RestorePending == true || this.padAdapter?.CleanupFault is { Length: > 0 }
            || this.actionOwner.IsArmed || this.actionOwner.IsReleasing || this.actionOwner.ReleaseRequested
            || this.actionOwner.StopLatched || this.actionOwner.RecordFaulted)
            return "owner_or_input_busy";
        Event ev = Game1.CurrentEvent;
        // Native HOST path broadcasts festivalEvent to clients (Event.cs:11685-11715).
        ev.forceFestivalContinue();
        this.Monitor.Log($"STAGING festival-start HOST player={Game1.player.UniqueMultiplayerID} festival={ev.id} (not witness evidence)", LogLevel.Info);
        return null;
    }

    private bool EventMoveBindingReady(MenuStepCommandRequest req) => this.backgroundMove?.EventMoveValid(req) == true;

    private void EnrichEventMovementContext(Dictionary<string, object> snapshot)
    {
        if (!this.UiG3On || !this.PadPathConfigured || !this.config.WorldPressEnabled || !this.config.MenuStepCommandInboxEnabled
            || Game1.player?.CanMove != true || Game1.player.UsingTool
            || Game1.globalFade || Game1.fadeToBlack || Game1.freezeControls || Game1.panMode || Game1.paused || Game1.HostPaused
            || Game1.locationRequest is not null || NativeEventControl.Token() is not { } token)
            return;
        snapshot["event_movement"] = new Dictionary<string, object>
        {
            ["available"] = true, ["event_token"] = token, ["max_ticks"] = EventControlMove.MaxTicks,
            ["generic"] = NativeEventControl.GenericControlEnabled, ["player_control"] = Game1.CurrentEvent.playerControlSequence,
            ["sequence"] = Game1.CurrentEvent.playerControlSequenceID, ["command_index"] = Game1.CurrentEvent.CurrentCommand,
            ["festival_id"] = Game1.CurrentEvent.id, ["is_festival"] = Game1.CurrentEvent.isFestival,
            ["main_event_running"] = Game1.CurrentEvent.eventSwitched, ["timer_positive"] = Game1.CurrentEvent.festivalTimer > 0,
            ["target_tile"] = new[] { Game1.CurrentEvent.playerControlTargetTile.X, Game1.CurrentEvent.playerControlTargetTile.Y },
            ["nearby"] = this.EventNearbyTargets(),
        };
        if (snapshot.TryGetValue("capabilities", out object? raw) && raw is Dictionary<string, object> caps)
            caps["world_key"] = new Dictionary<string, object>
            {
                ["available"] = true, ["primitives"] = new[] { "move_cardinal" }, ["scope"] = "event_movement",
            };
    }
}
