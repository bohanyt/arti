namespace ArtiStardewBridge.Protocol;

/// <summary>Per-operation commit latch and monotonic progress ladder; never writes native state.</summary>
public sealed class TravelProgress
{
    public const long HostWaitMs = 30000, PhaseWaitMs = 8000, TotalWaitMs = 60000;
    public string? CommittedRequest { get; private set; }
    public string? CommittedEpoch { get; private set; }
    public bool Redirected { get; private set; }
    public string Phase { get; private set; } = "input";
    public long CommitMs { get; private set; }
    public long ChangedMs { get; private set; }
    string? lastLocation, lastRequest;
    double? lastAlpha;
    public bool Committed => this.CommittedRequest is not null;

    public void Observe(long now, string? request, string location, bool transitioning, bool free,
        bool modal, bool eventUp, double? alpha, bool ownedInput, string? warpReceipt = null, string? requestEpoch = null)
    {
        if (!this.Committed)
        {
            if (!ownedInput || string.IsNullOrEmpty(request ?? warpReceipt)) return;
            this.CommittedRequest = request ?? warpReceipt;
            this.CommittedEpoch = requestEpoch;
            this.CommitMs = this.ChangedMs = now;
        }
        if (request is not null && (request != this.CommittedRequest
            || requestEpoch is not null && this.CommittedEpoch is not null && requestEpoch != this.CommittedEpoch))
            this.Redirected = true;
        string phase = modal || eventUp ? "arrival_owner"
            : location.Length == 0 ? "waiting_host_location"
            : request is not null ? "request"
            : transitioning ? "fade"
            : free ? "can_move" : "location_changed";
        // Alpha is quantized: floating noise is not indefinite progress.
        double? sampled = alpha is double a && double.IsFinite(a) ? Math.Round(a, 2) : null;
        if (phase != this.Phase || location != this.lastLocation || request != this.lastRequest || sampled != this.lastAlpha)
            this.ChangedMs = now;
        this.Phase = phase;
        this.lastLocation = location;
        this.lastRequest = request;
        this.lastAlpha = sampled;
    }

    public bool Stalled(long now) => this.Committed && (now - this.CommitMs >= TotalWaitMs
        || now - this.ChangedMs >= (this.Phase == "waiting_host_location" ? HostWaitMs : PhaseWaitMs));

    public string Notice(long now) => this.Stalled(now) ? "stalled_phase"
        : now - this.ChangedMs >= 15000 ? "diagnostic"
        : now - this.ChangedMs >= 5000 ? "slow" : "progress";
}
