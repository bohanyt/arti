namespace ArtiStardewBridge.Protocol;

/// <summary>One pinned native BeginSitting lerp. Never input authority or release evidence.</summary>
public sealed class FestivalSeatMotion
{
    // Native lerp=150 game ms, freeze=250 game ms (Farmer.cs:4552-4562).
    // Wall-clock cushion is bounded and less than the existing 3000 ms cleanup/ack bounds.
    public const long BoundMs = 2000;
    public const double MaxDisplacement = 128, Tolerance = .5;
    readonly double x, y, endX, endY, distanceSquared;
    long? started;
    double progress;
    bool faulted, observed;
    public FestivalSeatMotion(double x, double y, double endX, double endY)
    {
        this.x = x; this.y = y; this.endX = endX; this.endY = endY;
        this.distanceSquared = (endX - x) * (endX - x) + (endY - y) * (endY - y);
    }
    public bool GeometryValid => double.IsFinite(this.distanceSquared) && this.distanceSquared <= MaxDisplacement * MaxDisplacement;
    public bool Started => this.started.HasValue;
    public bool Faulted => this.faulted;
    public bool Settled { get; private set; }
    public void Begin(long now) { this.started ??= now; }
    public bool Sample(long now, FestivalSeatMotionFacts f)
    {
        if (!this.Started) return false;
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
}

public readonly record struct FestivalSeatMotionFacts(bool Pinned, bool Sitting, bool CanMove,
    int FreezePause, int MovementDirections, double X, double Y);
