namespace ArtiStardewBridge.Protocol;

/// <summary>One native tool cycle, verified from fresh target state. No retries or world setters.</summary>
public sealed class WaterOnceAction
{
    public const int MaxTicks = 180;
    private readonly Action begin;
    private readonly Action release;
    private ulong? firstTick;
    private ulong? lastTick;
    private bool began;
    private bool released;
    public bool IsActive { get; private set; }
    public string Status { get; private set; } = "Idle";

    public WaterOnceAction(Action begin, Action release)
    {
        this.begin = begin;
        this.release = release;
    }

    public string Start(WaterObservation observation)
    {
        if (this.IsActive) return "RejectedBusy";
        if (observation.Rejection is not null) return observation.Rejection;
        if (observation.Watered) return "AlreadyWatered";
        if (observation.UsingTool) return "RejectedUsingTool";
        this.began = this.released = false;
        this.firstTick = this.lastTick = null;
        this.IsActive = true;
        return this.Status = "Queued";
    }

    public void Tick(ulong tick, WaterObservation observation) => this.Tick(tick, () => observation);

    public void Tick(ulong tick, Func<WaterObservation> observe)
    {
        if (!this.IsActive || (this.lastTick.HasValue && tick <= this.lastTick.Value)) return;
        this.lastTick = tick;
        WaterObservation observation = observe();
        if (observation.Rejection is not null) { this.Abort(observation.Rejection); return; }
        if (!this.began)
        {
            if (observation.Watered) { this.Abort("AlreadyWatered"); return; }
            if (observation.UsingTool) { this.Abort("RejectedUsingTool"); return; }
            this.firstTick = tick;
            // A throwing native callback may already have taken effect: never retry it.
            this.began = true;
            this.Status = "AwaitingWorldResult";
            try { this.begin(); }
            catch { this.Abort("UnknownBeginError"); }
            return;
        }
        // A legal EndUsingTool must still run on the bound tick. Classify from
        // a fresh observation after that callback, not from the pre-release
        // snapshot, and not from the fact that release was invoked.
        if (!this.released && observation.UsingTool && observation.CanRelease)
        {
            this.released = true;
            try { this.release(); }
            catch { this.Abort("UnknownReleaseError"); return; }
            // EndUsingTool may apply soil/tool changes on this tick. The
            // pre-release snapshot cannot classify that result; hard bound
            // still closes after this fresh readback — no extra tick, no retry.
            observation = observe();
            if (observation.Rejection is not null) { this.Abort(observation.Rejection); return; }
        }
        if (observation.Watered && !observation.UsingTool)
        {
            this.Abort("ObservedWatered");
            return;
        }
        if (tick - this.firstTick!.Value >= MaxTicks)
        {
            this.Abort("UnknownTimedOut");
            return;
        }
    }

    // Stops this controller. A native animation already begun is not rolled back.
    public void Abort(string reason) { this.Status = reason; this.IsActive = false; }
}

public readonly record struct WaterObservation(string? Rejection, bool Watered, bool UsingTool, bool CanRelease);
