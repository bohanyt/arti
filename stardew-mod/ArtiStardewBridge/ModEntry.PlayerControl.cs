using System.Text.Json;
using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    /// <summary>FIXB37 game-thread read only. No inferred cause, input, recovery, or gate changes.</summary>
    private static PlayerControlDiagnostics CapturePlayerControlDiagnostics()
    {
        Farmer player = Game1.player;
        Event? currentEvent = Game1.CurrentEvent;
        return new PlayerControlDiagnostics
        {
            SampleTick = Game1.ticks,
            SampleUtc = DateTime.UtcNow.ToString("o"),
            WorldReady = Context.IsWorldReady,
            IsPlayerFree = Context.IsPlayerFree,
            CanPlayerMove = Context.CanPlayerMove,
            ActiveMenu = Game1.activeClickableMenu?.GetType().Name,
            EventUp = Game1.eventUp,
            EventId = currentEvent?.id,
            LocationEventId = player.currentLocation?.currentEvent?.id,
            EventCommand = currentEvent?.CurrentCommand,
            EventPlayerControl = currentEvent?.playerControlSequence,
            Festival = Game1.isFestival(),
            FarmEventType = Game1.farmEvent?.GetType().Name,
            MinigameType = Game1.currentMinigame?.GetType().Name,
            DialogueUp = Game1.dialogueUp,
            MessagePause = Game1.messagePause,
            Paused = Game1.paused,
            HostPaused = Game1.HostPaused,
            FreezeControls = Game1.freezeControls,
            IsWarping = Game1.isWarping,
            FadeToBlack = Game1.fadeToBlack,
            GlobalFade = Game1.globalFade,
            LocationRequestPending = Game1.locationRequest is not null,
            PlayerCanMove = player.CanMove,
            UsingTool = player.UsingTool,
            FreezePause = player.freezePause,
            SingleAnimationPause = player.FarmerSprite.pauseForSingleAnimation,
            AnimationActive = player.FarmerSprite.CurrentAnimation is { Count: > 0 },
            IsEating = player.isEating,
            IsInBed = player.isInBed.Value,
            PassingOut = player.FarmerSprite.isPassingOut(),
            SelectedSlot = player.CurrentToolIndex,
            SelectedItem = player.CurrentItem?.QualifiedItemId,
        };
    }

    private void LogPlayerControlTransition(ScreenState screen, PlayerControlDiagnostics facts)
    {
        if (screen.PlayerControlUnavailableReason is not null)
        {
            screen.PlayerControlUnavailableReason = null;
            screen.PlayerControlTransitions.Reset();
        }
        Farmer player = Game1.player;
        if (!screen.PlayerControlTransitions.Observe(player.UniqueMultiplayerID, facts))
            return;
        this.Monitor.Log("PLAYER_CONTROL " + JsonSerializer.Serialize(new
        {
            player_id = player.UniqueMultiplayerID,
            screen_id = Context.ScreenId,
            session_id = this.commandSessionId,
            location = player.currentLocation?.NameOrUniqueName,
            tile = new { x = player.TilePoint.X, y = player.TilePoint.Y },
            pixel = new { x = player.Position.X, y = player.Position.Y },
            clock_local = Game1.timeOfDay,
            clock_world = NativeWorldClock.World,
            clock_fact = NativeWorldClock.Now,
            facts,
        }), LogLevel.Debug);
    }

    private void LogPlayerControlUnavailable(ScreenState screen)
    {
        string reason = !this.config.Enabled ? "observer_disabled" : "world_not_ready";
        if (screen.PlayerControlUnavailableReason == reason) return;
        screen.PlayerControlUnavailableReason = reason;
        this.Monitor.Log("PLAYER_CONTROL_UNAVAILABLE " + JsonSerializer.Serialize(new
        {
            sample_tick = Game1.ticks, sample_utc = DateTime.UtcNow.ToString("o"),
            player_id = Game1.player?.UniqueMultiplayerID, screen_id = Context.ScreenId,
            session_id = this.commandSessionId, reason,
        }), LogLevel.Debug);
    }
}
