using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>FIXB37 game-thread facts with bounded diagnostic tokens, never an admission rule or event allowlist.</summary>
public sealed record PlayerControlDiagnostics
{
    // FIXB39: serialized UTF-8 content bytes (JSON escapes included, surrounding quotes excluded).
    // Overlong diagnostic tokens retain a prefix only; they must never authorize input or identify a target.
    public const int SampleUtcMaxUtf8Bytes = 32;
    public const int StringMaxUtf8Bytes = 128;

    private string sampleUtc = "";
    private string? activeMenu, eventId, locationEventId, farmEventType, minigameType, selectedItem;
    private static string? BoundToken(string? value) =>
        value is null ? null : SceneWire.Bound(value, StringMaxUtf8Bytes);

    [JsonPropertyName("sample_tick")] public int SampleTick { get; init; }
    [JsonPropertyName("sample_utc")] public string SampleUtc
    { get => this.sampleUtc; init => this.sampleUtc = SceneWire.Bound(value, SampleUtcMaxUtf8Bytes); }
    [JsonPropertyName("world_ready")] public bool WorldReady { get; init; }
    [JsonPropertyName("is_player_free")] public bool IsPlayerFree { get; init; }
    [JsonPropertyName("can_player_move")] public bool CanPlayerMove { get; init; }
    [JsonPropertyName("active_menu")] public string? ActiveMenu
    { get => this.activeMenu; init => this.activeMenu = BoundToken(value); }
    [JsonPropertyName("event_up")] public bool EventUp { get; init; }
    [JsonPropertyName("event_id")] public string? EventId
    { get => this.eventId; init => this.eventId = BoundToken(value); }
    [JsonPropertyName("location_event_id")] public string? LocationEventId
    { get => this.locationEventId; init => this.locationEventId = BoundToken(value); }
    [JsonPropertyName("event_command")] public int? EventCommand { get; init; }
    [JsonPropertyName("event_player_control")] public bool? EventPlayerControl { get; init; }
    [JsonPropertyName("festival")] public bool Festival { get; init; }
    [JsonPropertyName("farm_event_type")] public string? FarmEventType
    { get => this.farmEventType; init => this.farmEventType = BoundToken(value); }
    [JsonPropertyName("minigame_type")] public string? MinigameType
    { get => this.minigameType; init => this.minigameType = BoundToken(value); }
    [JsonPropertyName("dialogue_up")] public bool DialogueUp { get; init; }
    [JsonPropertyName("message_pause")] public bool MessagePause { get; init; }
    [JsonPropertyName("paused")] public bool Paused { get; init; }
    [JsonPropertyName("host_paused")] public bool HostPaused { get; init; }
    [JsonPropertyName("freeze_controls")] public bool FreezeControls { get; init; }
    [JsonPropertyName("is_warping")] public bool IsWarping { get; init; }
    [JsonPropertyName("fade_to_black")] public bool FadeToBlack { get; init; }
    [JsonPropertyName("global_fade")] public bool GlobalFade { get; init; }
    [JsonPropertyName("location_request_pending")] public bool LocationRequestPending { get; init; }
    [JsonPropertyName("player_can_move")] public bool PlayerCanMove { get; init; }
    [JsonPropertyName("using_tool")] public bool UsingTool { get; init; }
    [JsonPropertyName("freeze_pause")] public int FreezePause { get; init; }
    [JsonPropertyName("single_animation_pause")] public bool SingleAnimationPause { get; init; }
    [JsonPropertyName("animation_active")] public bool AnimationActive { get; init; }
    [JsonPropertyName("is_eating")] public bool IsEating { get; init; }
    [JsonPropertyName("is_in_bed")] public bool IsInBed { get; init; }
    [JsonPropertyName("passing_out")] public bool PassingOut { get; init; }
    [JsonPropertyName("selected_slot")] public int SelectedSlot { get; init; }
    [JsonPropertyName("selected_item")] public string? SelectedItem
    { get => this.selectedItem; init => this.selectedItem = BoundToken(value); }
}

/// <summary>Per-screen log deduplication only; captures continue when the socket has no reader.</summary>
public sealed class PlayerControlTransitionTracker
{
    private (long PlayerId, PlayerControlDiagnostics Facts)? previous;

    public bool Observe(long playerId, PlayerControlDiagnostics facts)
    {
        // Timer countdown/sample timestamps are retained in the emitted facts but aren't new states.
        var key = (playerId, facts with { SampleTick = 0, SampleUtc = "", FreezePause = Math.Sign(facts.FreezePause) });
        if (this.previous is { } old && old == key) return false;
        this.previous = key;
        return true;
    }

    public void Reset() => this.previous = null;
}
