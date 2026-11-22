using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// fix89D / ADR 158: one held direction per straight segment.
//
// Pure, SMAPI-free. A move_cardinal input_once may name the segment's last tile
// ("segment_stop": [x, y]). The hold then runs up to MaxTicks H frames, and this
// guard checks the actor's position after every consumed H frame:
//   - SEGMENT_STOP_REACHED  the bounding-box centre is within half a frame step of the stop tile centre;
//   - SEGMENT_NO_PROGRESS   NoProgressFrames consumed H frames in a row without along-axis movement
//                           (an obstacle or a character in the way);
//   - SEGMENT_LEFT_CORRIDOR the actor drifted more than MaxPerpendicularPx off the straight line
//                           (the game's corner slide), or passed the stop tile;
//   - SEGMENT_LOCATION_CHANGED a warp / map edge (not a settle reason: released early, as today).
// A guard reason only moves the existing BgMoveFramePlan toward R/N (RequestRelease); release
// verification, STOP, lease, containment and the stuck-input hard stop are unchanged. The guard
// never commits an action: it only ends a hold early.
public readonly record struct MoveSegmentStop(int X, int Y)
{
    public const string ArgName = "segment_stop";

    /// <summary>Absent → (true, null). Present → exactly [x, y] plain non-negative integers within the tile range.</summary>
    public static bool TryParse(JsonElement args, out MoveSegmentStop? stop)
    {
        stop = null;
        if (args.ValueKind != JsonValueKind.Object || !args.TryGetProperty(ArgName, out JsonElement raw))
            return true;
        if (raw.ValueKind != JsonValueKind.Array || raw.GetArrayLength() != 2)
            return false;
        JsonElement x = raw[0], y = raw[1];
        if (x.ValueKind != JsonValueKind.Number || y.ValueKind != JsonValueKind.Number
            || !x.TryGetInt32(out int tx) || !y.TryGetInt32(out int ty)
            || tx < 0 || ty < 0 || tx > MoveSegmentGuard.TileMax || ty > MoveSegmentGuard.TileMax)
            return false;
        stop = new MoveSegmentStop(tx, ty);
        return true;
    }
}

public sealed class MoveSegmentGuard
{
    public const int MaxTicks = 120;            // 2 s at 60 Hz; inside the 4 s native lease cap
    public const int MaxTiles = 10;             // ~640 px; at the measured 5.3 px/frame ~121 frames
    public const int TileMax = 10000;           // same bound as the Python command client (TILE_MAX)
    public const int NoProgressFrames = 6;
    public const float MaxPerpendicularPx = 8f; // same bound as the Python segment feedback
    public const int TilePx = 64;
    public const float CentreOffsetX = 32f;     // Farmer.Position → TilePoint centre (+32, +16)
    public const float CentreOffsetY = 16f;

    public const string ReasonReached = "SEGMENT_STOP_REACHED";
    public const string ReasonNoProgress = "SEGMENT_NO_PROGRESS";
    public const string ReasonLeftCorridor = "SEGMENT_LEFT_CORRIDOR";
    public const string ReasonLocationChanged = "SEGMENT_LOCATION_CHANGED";
    public const string ReasonInvalid = "SEGMENT_INVALID";

    private readonly int dx, dy;
    private readonly float stopCentreAxis;
    private readonly float perpendicularStart;
    private readonly int stopAxisTile;
    private readonly string location;
    private float lastAxis;
    private int lastConsumedH;
    private int stillFrames;
    private float lastStep;

    public string Direction { get; }
    public MoveSegmentStop Stop { get; }
    public int Tiles { get; }
    public string? Reason { get; private set; }

    private MoveSegmentGuard(string direction, int dx, int dy, MoveSegmentStop stop, int tiles, string location,
        float startX, float startY)
    {
        this.Direction = direction;
        this.dx = dx;
        this.dy = dy;
        this.Stop = stop;
        this.Tiles = tiles;
        this.location = location;
        float cx = startX + CentreOffsetX, cy = startY + CentreOffsetY;
        this.lastAxis = dx != 0 ? cx : cy;
        this.perpendicularStart = dx != 0 ? cy : cx;
        this.stopAxisTile = dx != 0 ? stop.X : stop.Y;
        this.stopCentreAxis = this.stopAxisTile * TilePx + TilePx / 2f;
    }

    /// <summary>Validate the stop against the actor's start pose: same row/column, 1..MaxTiles ahead, consistent geometry.</summary>
    public static bool TryCreate(string direction, int ticks, MoveSegmentStop stop, string location,
        float startX, float startY, int tileX, int tileY, out MoveSegmentGuard? guard, out string? reason)
    {
        guard = null;
        reason = ReasonInvalid;
        (int dx, int dy) = direction switch
        {
            "left" => (-1, 0), "right" => (1, 0), "up" => (0, -1), "down" => (0, 1), _ => (0, 0),
        };
        if ((dx == 0 && dy == 0) || ticks < 1 || ticks > MaxTicks || string.IsNullOrEmpty(location)
            || !float.IsFinite(startX) || !float.IsFinite(startY))
            return false;
        // Inconsistent geometry cannot size a segment (the same rule the Python controller applies).
        if ((int)Math.Floor((startX + CentreOffsetX) / TilePx) != tileX
            || (int)Math.Floor((startY + CentreOffsetY) / TilePx) != tileY)
            return false;
        int tiles = dx != 0 ? (stop.X - tileX) * dx : (stop.Y - tileY) * dy;
        bool sameLine = dx != 0 ? stop.Y == tileY : stop.X == tileX;
        if (!sameLine || tiles < 1 || tiles > MaxTiles)
            return false;
        guard = new MoveSegmentGuard(direction, dx, dy, stop, tiles, location, startX, startY);
        reason = null;
        return true;
    }

    /// <summary>
    /// Called after every game update while the hold plan's next frame is H. Returns the first
    /// release reason (sticky), or null to keep holding. Only consumed H frames count as motion attempts.
    /// </summary>
    public string? Observe(string currentLocation, float x, float y, int consumedH)
    {
        if (this.Reason is not null)
            return this.Reason;
        if (!string.Equals(currentLocation, this.location, StringComparison.Ordinal))
            return this.Reason = ReasonLocationChanged;
        if (!float.IsFinite(x) || !float.IsFinite(y))
            return this.Reason = ReasonLeftCorridor;
        float cx = x + CentreOffsetX, cy = y + CentreOffsetY;
        float axis = this.dx != 0 ? cx : cy;
        float perpendicular = this.dx != 0 ? cy : cx;
        int sign = this.dx != 0 ? this.dx : this.dy;
        if (Math.Abs(perpendicular - this.perpendicularStart) > MaxPerpendicularPx)
            return this.Reason = ReasonLeftCorridor;
        int axisTile = (int)Math.Floor(axis / TilePx);
        if ((axisTile - this.stopAxisTile) * sign > 0)
            return this.Reason = ReasonLeftCorridor; // past the stop tile: never expected, never a settle
        if (consumedH > this.lastConsumedH)
        {
            float step = (axis - this.lastAxis) * sign;
            if (step > 0.01f)
            {
                this.lastStep = step;
                this.stillFrames = 0;
            }
            else
            {
                this.stillFrames += consumedH - this.lastConsumedH;
            }
            this.lastConsumedH = consumedH;
            this.lastAxis = axis;
        }
        float remaining = (this.stopCentreAxis - axis) * sign;
        // Stop as close to the centre as one frame step allows: never more than half a step past it.
        if (axisTile == this.stopAxisTile && remaining <= this.lastStep / 2f)
            return this.Reason = ReasonReached;
        if (this.stillFrames >= NoProgressFrames)
            return this.Reason = ReasonNoProgress;
        return null;
    }

    /// <summary>
    /// Guard reasons that settle the press like a fully consumed one: readback stays the arrival oracle,
    /// exactly as for a 60-frame press that bumped into an obstacle today. A location change is not one.
    /// </summary>
    public static bool SettlesPress(string? reason) =>
        reason is ReasonReached or ReasonNoProgress or ReasonLeftCorridor
            // fix92A / ADR 162: a path's own interrupts end H the same way (readback stays the oracle).
            or MovePath.ReasonDamage or MovePath.ReasonEvent;
}

// fix92A / ADR 162: a walk follows a guarded PATH (a chain of segments with turns) inside one request.
//
// Pure, SMAPI-free. A move_cardinal with segment_stop may also carry "path_next": [[x, y], ...], the stop
// tiles of the following segments on the same map. Each stop lies on the row or column of the previous
// stop and turns by 90 degrees (no straight continuation, no reversal), 1..MoveSegmentGuard.MaxTiles tiles
// per segment, at most MaxSegments segments and MaxTiles tiles in all, and the hold may then run up to
// MaxTicks frames in all. The runtime walks segment k with an ordinary MoveSegmentGuard. Only on
// SEGMENT_STOP_REACHED, after the old pin's release is PROVED (one R frame, the existing BgPressSession
// proof), it turns: a fresh guard from the real pose, the same frame plan re-opened for H with the new pin,
// and a new single-Press session armed before its Press. Anything else ends the path through the plan's
// ordinary R -> N -> release verification; Python re-observes and re-plans.
public readonly record struct MovePathStops(IReadOnlyList<MoveSegmentStop> Stops)
{
    public const string ArgName = "path_next";

    /// <summary>Absent → (true, null). Present → 1..MovePath.MaxSegments-1 plain [x, y] stops.</summary>
    public static bool TryParse(JsonElement args, out MovePathStops? path)
    {
        path = null;
        if (args.ValueKind != JsonValueKind.Object || !args.TryGetProperty(ArgName, out JsonElement raw))
            return true;
        if (raw.ValueKind != JsonValueKind.Array || raw.GetArrayLength() < 1 || raw.GetArrayLength() > MovePath.MaxSegments - 1)
            return false;
        List<MoveSegmentStop> stops = new();
        foreach (JsonElement item in raw.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Array || item.GetArrayLength() != 2)
                return false;
            JsonElement x = item[0], y = item[1];
            if (x.ValueKind != JsonValueKind.Number || y.ValueKind != JsonValueKind.Number
                || !x.TryGetInt32(out int tx) || !y.TryGetInt32(out int ty)
                || tx < 0 || ty < 0 || tx > MoveSegmentGuard.TileMax || ty > MoveSegmentGuard.TileMax)
                return false;
            stops.Add(new MoveSegmentStop(tx, ty));
        }
        path = new MovePathStops(stops);
        return true;
    }

    /// <summary>Canonical digest text ("x,y;x,y"), mirrored by the Python command client.</summary>
    public string Canonical() => string.Join(";", this.Stops.Select(s => s.X + "," + s.Y));
}

public static class MovePath
{
    public const int MaxSegments = 8;
    public const int MaxTiles = 30;
    /// <summary>3 s at 60 Hz: the 4 s native lease minus its 0.5 s admission margin and fix89D's 0.5 s deadline margin.</summary>
    public const int MaxTicks = 180;

    public const string ReasonComplete = "PATH_COMPLETE";
    public const string ReasonStopped = "PATH_STOPPED";
    public const string ReasonTurnUnproved = "PATH_TURN_RELEASE_UNPROVED";
    public const string ReasonNextInvalid = "PATH_NEXT_SEGMENT_INVALID";
    public const string ReasonFramesSpent = "PATH_FRAMES_SPENT";
    public const string ReasonDamage = "PATH_DAMAGE_TAKEN";
    public const string ReasonEvent = "PATH_EVENT_OR_CANNOT_MOVE";

    /// <summary>Cardinal direction from one stop to the next on the same row / column; null when not aligned.</summary>
    public static string? DirectionBetween(MoveSegmentStop from, MoveSegmentStop to)
    {
        if (from.X == to.X && from.Y != to.Y)
            return to.Y > from.Y ? "down" : "up";
        if (from.Y == to.Y && from.X != to.X)
            return to.X > from.X ? "right" : "left";
        return null;
    }

    private static bool Perpendicular(string a, string b) =>
        (a is "left" or "right") != (b is "left" or "right");

    /// <summary>
    /// The whole polyline from the first stop: every next stop aligned with the previous one, a 90-degree turn
    /// at every stop, 1..MoveSegmentGuard.MaxTiles tiles per segment, MaxTiles in all (the first segment's
    /// tiles included), 2..MaxSegments segments. The first segment itself is validated by MoveSegmentGuard
    /// against the real pose. Pure: never touches input.
    /// </summary>
    public static bool Validate(string firstDirection, int firstTiles, MoveSegmentStop firstStop, MovePathStops path, int ticks,
        out string? reason)
    {
        reason = MoveSegmentGuard.ReasonInvalid;
        if (path.Stops is null || path.Stops.Count < 1 || path.Stops.Count > MaxSegments - 1
            || ticks < 1 || ticks > MaxTicks || firstTiles < 1 || firstTiles > MoveSegmentGuard.MaxTiles)
            return false;
        int total = firstTiles;
        string previousDirection = firstDirection;
        MoveSegmentStop previous = firstStop;
        foreach (MoveSegmentStop stop in path.Stops)
        {
            if (DirectionBetween(previous, stop) is not { } dir || !Perpendicular(previousDirection, dir))
                return false;
            int tiles = Math.Abs(stop.X - previous.X) + Math.Abs(stop.Y - previous.Y);
            if (tiles > MoveSegmentGuard.MaxTiles)
                return false;
            total += tiles;
            if (total > MaxTiles)
                return false;
            previousDirection = dir;
            previous = stop;
        }
        reason = null;
        return true;
    }
}

/// <summary>fix92A / ADR 162: how far a path got and why it ended (result field "path"; diagnostics, never an oracle).</summary>
public sealed record MovePathReport(
    [property: System.Text.Json.Serialization.JsonPropertyName("segments")] int Segments,
    [property: System.Text.Json.Serialization.JsonPropertyName("segments_done")] int SegmentsDone,
    [property: System.Text.Json.Serialization.JsonPropertyName("turns")] int Turns,
    [property: System.Text.Json.Serialization.JsonPropertyName("end")] string End);
