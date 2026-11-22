namespace ArtiHostHelper;

/// <summary>ADR 74: when a reached gift slot may make its one native proposal. "idle" (free play) waits until the
/// peer's synced state is still: same pose, no native movement, no menu, tool or hidden/warp/event for IdleSeconds,
/// so a harness gift never cuts into her trip or action. "moving" keeps the targeted interruption witness
/// (accept/decline during correlated active movement, docs/stardew/free-play-harness.md).</summary>
public sealed class GiftOpportunity
{
    public const string Idle = "idle", Moving = "moving";
    public const double MaxIdleSeconds = 600;
    readonly string mode;
    readonly double idleSeconds;
    string? pose;
    double? stillSince;

    public GiftOpportunity(string mode, double idleSeconds)
    {
        if (mode is not (Idle or Moving) || idleSeconds is < 1 or > MaxIdleSeconds)
            throw new InvalidOperationException("GiftWhen must be idle|moving and GiftIdleSeconds 1..600");
        this.mode = mode;
        this.idleSeconds = idleSeconds;
    }

    /// <summary>Observed peer movement: a pose delta since the last step plus native isMoving.</summary>
    public bool PeerMoving { get; private set; }

    public double StillSeconds(double now) => stillSince is double s ? now - s : 0;

    /// <summary>Call once per helper step, before any early return, so stillness is measured continuously.</summary>
    public bool Observe(string? peerPose, bool nativeMoving, bool busy, double now)
    {
        bool changed = peerPose != pose;
        PeerMoving = peerPose != null && pose != null && changed && nativeMoving;
        pose = peerPose;
        if (peerPose == null || changed || nativeMoving || busy) stillSince = null;
        else stillSince ??= now;
        return mode == Moving ? PeerMoving : stillSince is double since && now - since >= idleSeconds;
    }
}
