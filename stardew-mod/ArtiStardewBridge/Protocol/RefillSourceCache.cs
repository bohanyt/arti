namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Game-thread cache of native refillable tiles for the current location/map. Only the
/// refill predicate is cached: characters and shoreline access are sampled at selection.
/// </summary>
public sealed class RefillSourceCache
{
    object? location;
    object? map;
    int width, height;
    long revision;
    IReadOnlyList<TileXY>? sources;

    // Bind even when the can is full, so leaving and returning rebuilds the cache.
    public void Bind(object currentLocation, object? currentMap, int mapWidth, int mapHeight, long mapRevision)
    {
        if (ReferenceEquals(this.location, currentLocation) && ReferenceEquals(this.map, currentMap)
            && this.width == mapWidth && this.height == mapHeight && this.revision == mapRevision)
            return;
        this.location = currentLocation;
        this.map = currentMap;
        this.width = mapWidth;
        this.height = mapHeight;
        this.revision = mapRevision;
        this.sources = null;
    }

    public IReadOnlyList<TileXY> Find(int x, int y, Func<int, int, bool> refill,
        Func<int, int, bool> standable, Func<int, int, bool> sourceClear)
    {
        if (this.sources is null)
        {
            var tiles = new List<TileXY>();
            for (int ty = 0; ty < this.height; ty++)
            for (int tx = 0; tx < this.width; tx++)
                if (refill(tx, ty)) tiles.Add(new TileXY { X = tx, Y = ty });
            // Publish only after the entire scan succeeds; failed captures retry next time.
            this.sources = tiles;
        }
        return GatherFeedback.FindRefillSources(x, y, this.sources, standable, sourceClear);
    }
}
