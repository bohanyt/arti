namespace ArtiStardewBridge.Protocol;

/// <summary>One pinned native BeginSitting lerp, or one StopSitting lerp off the seat. Never input authority
/// or release evidence.</summary>
public sealed class FestivalSeatMotion
{
    // Native lerp=150 game ms, freeze=250 game ms (Farmer.cs:4552-4562).
    // Wall-clock cushion is bounded and less than the existing 3000 ms cleanup/ack bounds.
    public const long BoundMs = 2000;
    public const double MaxDisplacement = 128, Tolerance = .5;
    readonly double x, y, endX, endY, distanceSquared;
    readonly HashSet<(int X, int Y)>? seatTiles; // stand only; null = a sit
    readonly double minX, minY, maxX, maxY;      // stand only: the ring around the seat, in pixels
    long? started;
    double progress;
    bool faulted, observed;
    public FestivalSeatMotion(double x, double y, double endX, double endY)
    {
        this.x = x; this.y = y; this.endX = endX; this.endY = endY;
        this.distanceSquared = (endX - x) * (endX - x) + (endY - y) * (endY - y);
    }

    /// <summary>Farmer.StopSitting() (Farmer.cs:4565-4664) lerps her 150 ms from the seat tile to the first free
    /// tile beside it or on the ring around the seat bounds, then Farmer.Update clears isSitting (:7626-7635).
    /// <paramref name="x"/>, <paramref name="y"/>: the sitting pose she holds until the lerp starts.</summary>
    public static FestivalSeatMotion Stand(double x, double y, IReadOnlyCollection<(int X, int Y)> seatTiles) => new(x, y, seatTiles);

    FestivalSeatMotion(double x, double y, IReadOnlyCollection<(int X, int Y)> seatTiles) : this(x, y, x, y)
    {
        this.seatTiles = new HashSet<(int X, int Y)>(seatTiles);
        if (seatTiles.Count is 0 or > 64) { this.distanceSquared = double.NaN; return; }
        this.minX = (seatTiles.Min(t => t.X) - 1) * 64d; this.maxX = (seatTiles.Max(t => t.X) + 1) * 64d;
        this.minY = (seatTiles.Min(t => t.Y) - 1) * 64d; this.maxY = (seatTiles.Max(t => t.Y) + 1) * 64d;
        if (this.maxX - this.minX > 66 * 64 || this.maxY - this.minY > 66 * 64) this.distanceSquared = double.NaN;
    }

    public bool GeometryValid => double.IsFinite(this.distanceSquared)
        && (this.seatTiles is not null || this.distanceSquared <= MaxDisplacement * MaxDisplacement);
    public bool Started => this.started.HasValue;
    public bool Faulted => this.faulted;
    public bool Settled { get; private set; }
    /// <summary>The game stood her up in place (no free exit tile): motion over, never a verified stand.</summary>
    public bool Ended { get; private set; }
    public void Begin(long now) { this.started ??= now; }
    public bool Sample(long now, FestivalSeatMotionFacts f)
    {
        if (!this.Started) return false;
        if (this.seatTiles is not null) return this.SampleStand(now, f);
        bool moving = f.X != this.x || f.Y != this.y;
        if (!f.Pinned || !this.GeometryValid || !double.IsFinite(f.X) || !double.IsFinite(f.Y))
            this.faulted |= this.observed || moving;
        if (!f.Sitting)
        {
            this.faulted |= this.observed || moving;
            return false; // no native effect: no motion allowance and no successful seat effect
        }
        this.observed = true;
        double dx = this.endX - this.x, dy = this.endY - this.y;
        double t = this.distanceSquared == 0 ? 1 : ((f.X - this.x) * dx + (f.Y - this.y) * dy) / this.distanceSquared;
        bool onSegment = t >= -1e-5 && t <= 1 + 1e-5 && t + 1e-5 >= this.progress
            && Math.Abs(f.X - (this.x + t * dx)) <= Tolerance
            && Math.Abs(f.Y - (this.y + t * dy)) <= Tolerance;
        bool atEnd = Math.Abs(f.X - this.endX) <= Tolerance && Math.Abs(f.Y - this.endY) <= Tolerance;
        this.faulted |= !onSegment || f.MovementDirections != 0
            || f.FreezePause > 250
            || now - this.started!.Value > BoundMs && !this.Settled;
        this.progress = Math.Max(this.progress, t);
        this.Settled = !this.faulted && f.Pinned && atEnd && f.CanMove && f.FreezePause <= 0;
        return !this.faulted && f.Pinned && !this.Settled;
    }

    // Settled = standing, free to move, on a whole tile of the ring box that is not a seat tile. Leaving the box (or the
    // sitting pose), any movement input, a longer freeze or the bound is a sticky fault, as for a sit.
    bool SampleStand(long now, FestivalSeatMotionFacts f)
    {
        bool finite = double.IsFinite(f.X) && double.IsFinite(f.Y);
        bool inBox = finite && f.X >= this.minX - Tolerance && f.X <= this.maxX + Tolerance
            && f.Y >= this.minY - Tolerance && f.Y <= this.maxY + Tolerance;
        bool atPose = finite && Math.Abs(f.X - this.x) <= Tolerance && Math.Abs(f.Y - this.y) <= Tolerance;
        (int X, int Y) tile = finite ? ((int)Math.Round(f.X / 64), (int)Math.Round(f.Y / 64)) : (0, 0);
        bool offSeat = inBox && Math.Abs(f.X - tile.X * 64) <= Tolerance && Math.Abs(f.Y - tile.Y * 64) <= Tolerance
            && !this.seatTiles!.Contains(tile);
        // The envelope (pin, geometry, no movement input, freeze, bound) is checked on every sample,
        // the one that classifies an in-place unseat as Ended too. A motion that has ended is not held to the bound.
        this.faulted |= !f.Pinned || !this.GeometryValid || f.MovementDirections != 0 || f.FreezePause > 250
            || now - this.started!.Value > BoundMs && !this.Settled && !this.Ended;
        // No free exit tile: the game stands her in place (no lerp). No motion allowance and no verified stand.
        if (!f.Sitting && atPose && !this.observed) { this.Ended = !this.faulted; return false; }
        this.observed |= !atPose;
        this.faulted |= !(inBox || atPose) || !f.Sitting && !offSeat;
        this.Settled = !this.faulted && !f.Sitting && f.CanMove && f.FreezePause <= 0;
        return !this.faulted && !this.Settled;
    }
}

public readonly record struct FestivalSeatMotionFacts(bool Pinned, bool Sitting, bool CanMove,
    int FreezePause, int MovementDirections, double X, double Y);
