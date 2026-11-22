using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// WS3b (2026-10-07): the bed the local farmhand can sleep in (BedFurniture, not a child bed) in
/// its own home. <see cref="SleepTile"/> is where TouchAction Sleep fires (bed row Y+1, x =
/// bed X + current bedTileOffset; a single bed's offset follows the farmer's side, so the
/// published value is a snapshot). <see cref="EntryTiles"/> are the two tiles just outside the bed on
/// its sleep row: from either one the sleep tile is two tiles sideways (the offset always picks the
/// far half), which is where move_through kind "bed" starts. Carried in the
/// building-door bag of scene.current_location (no new snapshot batch; cap shared, beds are
/// indoors where there are no building doors).
/// </summary>
public sealed record SceneBedFact
{
    public const string KindName = "bed";

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = KindName;

    [JsonPropertyName("tile")]
    public TileXY Tile { get; init; } = new();

    [JsonPropertyName("sleep_tile")]
    public TileXY SleepTile { get; init; } = new();

    [JsonPropertyName("width")]
    public int Width { get; init; }

    [JsonPropertyName("entry_tiles")]
    public IReadOnlyList<TileXY> EntryTiles { get; init; } = Array.Empty<TileXY>();

    [JsonPropertyName("owned_by_actor")]
    public bool OwnedByActor { get; init; }

    public static string FactId(string locationId, int x, int y) =>
        $"bed:{SceneIdentity.Token(locationId)}:{x},{y}";

    /// <summary>The sleep-row tiles just left and right of the bed (never a bed tile).</summary>
    public static IReadOnlyList<TileXY> Entry(int bedX, int sleepY, int width) =>
        new[] { bedX - 1, bedX + width }.Where(x => x >= 0).Select(x => new TileXY { X = x, Y = sleepY }).ToArray();
}
