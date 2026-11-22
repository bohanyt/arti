namespace ArtiStardewBridge.Protocol;

public enum SceneDoorRefreshResult
{
    NoBaseline,
    Unchanged,
    Upserted,
    RequiresSnapshot,
}

/// <summary>
/// FINAL Part C3 (2026-10-20, J L958-L973): a door's <c>enterable</c> was computed only in a full scene snapshot,
/// so Town entered at 07:40 still listed both SeedShop doors (and Hospital, JojaMart, Blacksmith) as locked at 09:10.
/// The native rule (<c>GameLocation.lockedDoorWarp</c>, mirrored read-only by <c>NativeB1Offers.ResolveDoor</c>) reads
/// <c>Game1.timeOfDay</c>, the weekday, festival, key and friendship state, so the mod re-resolves the current
/// location's doors once per native clock value and sends the changed facts through the ordinary delta stream.
/// </summary>
public sealed class SceneDoorClock
{
    string? key;

    /// <summary>True once per (location, day, native clock): the cadence at which the door rules can change.</summary>
    public bool Due(string locationId, int dayOfMonth, int timeOfDay)
    {
        string next = $"{locationId}|{dayOfMonth}|{timeOfDay}";
        if (string.Equals(next, this.key, StringComparison.Ordinal))
            return false;
        this.key = next;
        return true;
    }
}

public static class SceneDoorRefresh
{
    /// <summary>The one door DTO builder: the full snapshot and the clock refresh emit the identical wire shape.</summary>
    public static ScenePreparedFact Fact(
        string locationId,
        int x,
        int y,
        string target,
        string? action,
        bool enterable,
        int? targetX,
        int? targetY
    ) => new(
        SceneIdentity.DoorFactId(locationId, x, y, target),
        new SceneDoorFact
        {
            Tile = new TileXY { X = x, Y = y },
            TargetLocation = SceneIdentity.Token(target),
            Action = action,
            Enterable = enterable,
            TargetTile = targetX is int tx && targetY is int ty ? new TileXY { X = tx, Y = ty } : null,
        }
    );

    /// <summary>
    /// Re-applies freshly resolved door facts of the current location to the delta stream. Only doors already in the
    /// working baseline are refreshed (a door capped out of the snapshot stays out, as its truncation says); a changed
    /// door identity at the same tile needs a full snapshot. Unchanged facts coalesce to no operation.
    /// </summary>
    public static SceneDoorRefreshResult Apply(
        SceneDeltaAccumulator deltas,
        string locationId,
        IReadOnlyList<ScenePreparedFact> fresh
    )
    {
        if (!deltas.TryGetWorkingFacts(out IReadOnlyDictionary<string, object> working))
            return SceneDoorRefreshResult.NoBaseline;
        if (deltas.RequiresSnapshot)
            return SceneDoorRefreshResult.RequiresSnapshot;
        string locationPrefix = $"door:{SceneIdentity.Token(locationId)}:";
        bool upserted = false;
        foreach (ScenePreparedFact fact in fresh)
        {
            if (fact.Dto is not SceneDoorFact door || !fact.Id.StartsWith(locationPrefix, StringComparison.Ordinal))
                continue;
            if (!working.TryGetValue(fact.Id, out object? original))
            {
                string tilePrefix = $"{locationPrefix}{door.Tile.X},{door.Tile.Y}:";
                foreach (string id in working.Keys)
                {
                    if (id.StartsWith(tilePrefix, StringComparison.Ordinal))
                        return SceneDoorRefreshResult.RequiresSnapshot;
                }
                continue;
            }
            bool changed = !SceneFactCanonical.Equal(original, SceneFactCanonical.Normalize(fact.Dto));
            if (deltas.Upsert(fact) == SceneDeltaAccumulateResult.OverflowRequiresSnapshot)
                return SceneDoorRefreshResult.RequiresSnapshot;
            upserted |= changed;
        }
        return upserted ? SceneDoorRefreshResult.Upserted : SceneDoorRefreshResult.Unchanged;
    }
}
