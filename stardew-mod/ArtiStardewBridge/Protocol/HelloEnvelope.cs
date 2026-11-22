using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

public sealed record HelloEnvelope
{
    [JsonPropertyName("protocol")]
    public int Protocol { get; init; } = 1;

    [JsonPropertyName("ev")]
    public string Event { get; init; } = "hello";

    [JsonPropertyName("seq")]
    public long Sequence { get; init; }

    [JsonPropertyName("player_id")]
    public long PlayerId { get; init; }

    [JsonPropertyName("screen_id")]
    public int ScreenId { get; init; }

    [JsonPropertyName("player_name")]
    public string PlayerName { get; init; } = "";

    [JsonPropertyName("is_main_player")]
    public bool IsMainPlayer { get; init; }

    [JsonPropertyName("is_split_screen")]
    public bool IsSplitScreen { get; init; }

    public static HelloEnvelope Create(
        long seq,
        long playerId,
        int screenId,
        string playerName,
        bool isMainPlayer,
        bool isSplitScreen
    ) => new()
    {
        Sequence = seq,
        PlayerId = playerId,
        ScreenId = screenId,
        PlayerName = playerName,
        IsMainPlayer = isMainPlayer,
        IsSplitScreen = isSplitScreen,
    };
}
