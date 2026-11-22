namespace ArtiStardewBridge.Protocol;

// V4 §5 / C3 containment policy with V4.1 D2 foreground scoping, S1 layer.
//
// Pure decision logic with an injected monotonic millisecond clock. This file
// never terminates anything: it only returns a containment decision. S2's
// BackgroundContainmentWatchdog owns the dedicated thread, the Stopwatch clock
// and the single Environment.FailFast path. Containment is failure containment
// only: never release, completion, PASS or retry.
//
// Stage 2 (2.2 section 2 / N2, dispatch R1/R2): an outstanding SMAPI press
// override for the pinned hold key is treated like held movement. The policy
// arms BEFORE Press (TryArmForPressOutstanding) and the press-outstanding flag
// clears only on OnPressReleaseProved (Suppress issued + IsSuppressed + key
// absent). While it is set, neither RELEASED_BY_GAME nor OnNeutralVerified can
// disarm. STOP / lease / caller loss / deadline never clear it. T3 still applies.

public enum BgContainmentState { Disarmed, Armed, Contained, DisarmedVerifiedNeutral, DisarmedReleasedByGame }

public enum BgArmReason { None, ConsumedH, PreHUnresolved, PressOutstanding }

public enum BgContainmentTrigger
{
    None,
    /// <summary>Background-only uncontrolled motion without a substituted callback.</summary>
    T1UncontrolledMotion,
    /// <summary>Pre-consumer dispatch impossible while movement is outstanding.</summary>
    T2DispatchImpossible,
    /// <summary>No game-thread heartbeat while armed.</summary>
    T3HeartbeatLoss,
    /// <summary>Background legitimate closed gate with movement outstanding.</summary>
    T4GateClosedUnresolved,
    /// <summary>Unresolved pre-H stale motion not stopped by neutral cleanup within its bound.</summary>
    PreHCleanupTimeout,
    /// <summary>
    /// Stage 2 (independent review B1): an owned press override is still not proved
    /// released after its release deadline (first release attempt + PressReleaseBoundMs),
    /// or no owned frame was consumed for PressStallBoundMs while it is outstanding.
    /// Independent of heartbeat and movement.
    /// </summary>
    T5PressReleaseUnresolved,
}

/// <summary>
/// Frozen V4 bounds. PreHCleanupBoundMs has no separate frozen number in V4; it
/// reuses the frozen T4 "movement outstanding unresolved" 10 s bound and is
/// disclosed in the S1 handoff for Primary CT confirmation.
/// </summary>
public sealed record BgContainmentLimits(
    int T1ConsecutiveTicks,
    long T3HeartbeatLossMs,
    long T4GateClosedMs,
    long PreHCleanupBoundMs,
    int StableCapturesToRelease,
    long WatchdogPollMs,
    long PressReleaseBoundMs = 3000,
    long PressStallBoundMs = 6000)
{
    public static readonly BgContainmentLimits FrozenV4 = new(6, 1500, 10_000, 10_000, 3, 50);
}

/// <summary>Per-UpdateTicked facts published by the game thread.</summary>
public readonly record struct BgTickFacts(
    long NowMs,
    bool GameActive,
    bool SubstitutedThisTick,
    string Location,
    double PixelX,
    double PixelY,
    int MovementDirectionsCount,
    bool GateClosed,
    bool ExpectedSeatMotion = false,
    bool ReleasedGamePause = false);

/// <summary>Immutable last snapshot kept in memory for the FailFast message; never written to disk here.</summary>
public sealed record BgContainmentSnapshot(
    string RequestId,
    string RuntimeId,
    BgArmReason ArmReason,
    long ArmedAtMs,
    long LastHeartbeatMs,
    long LastConsumedFrameMs,
    int MovementDirectionsCount,
    string Location,
    double PixelX,
    double PixelY,
    bool GameActive,
    int T1Streak,
    bool DispatchImpossible,
    bool PressOutstanding = false);

public sealed record BgContainmentDecision(BgContainmentTrigger Trigger, string FailFastMessage, BgContainmentSnapshot Snapshot);

public sealed class BackgroundContainmentPolicy
{
    public const string FailFastPrefix = "ARTI_BG_CONTAINMENT:";

    private readonly object gate = new();
    private BgTickFacts? lastTick;
    private BgContainmentTrigger containRequested = BgContainmentTrigger.None;
    private BgContainmentDecision? decision;
    private string requestId = "";
    private string runtimeId = "";
    private int stableCaptures;
    private bool pressOutstanding;
    private long? pressReleaseRequestedMs;

    public BackgroundContainmentPolicy(BgContainmentLimits limits) => this.Limits = limits;

    /// <summary>Stage 2: an owned press override is (or may be) outstanding.</summary>
    public bool PressOutstanding { get { lock (this.gate) return this.pressOutstanding; } }

    /// <summary>Stage 2 B1: monotonic time of the first release attempt for the outstanding press (null: none yet).</summary>
    public long? PressReleaseRequestedMs { get { lock (this.gate) return this.pressReleaseRequestedMs; } }

    public BgContainmentLimits Limits { get; }
    public BgContainmentState State { get; private set; } = BgContainmentState.Disarmed;
    public BgArmReason ArmReason { get; private set; }
    public long ArmedAtMs { get; private set; }
    public long LastHeartbeatMs { get; private set; }
    public long LastConsumedFrameMs { get; private set; }
    public int T1Streak { get; private set; }
    public bool DispatchImpossible { get; private set; }
    public bool ReleaseRequested { get; private set; }
    public IReadOnlyList<string> ReleaseSignals => this.releaseSignals;
    private readonly List<string> releaseSignals = new();

    public bool Armed { get { lock (this.gate) return this.State == BgContainmentState.Armed; } }

    /// <summary>Never "PASS". Containment and game-side release both stay UNCERTAIN.</summary>
    public string ResultLabel
    {
        get
        {
            lock (this.gate)
            {
                return this.State switch
                {
                    BgContainmentState.Contained => "UNCERTAIN/CONTAINED",
                    BgContainmentState.DisarmedReleasedByGame => "UNCERTAIN/RELEASED_BY_GAME",
                    BgContainmentState.DisarmedVerifiedNeutral => "RELEASE_VERIFIED",
                    BgContainmentState.Armed => "HELD_OUTSTANDING",
                    _ => "NOT_ARMED",
                };
            }
        }
    }

    /// <summary>Arm at the first consumed ARTI H frame. Refuses HOST/main-player and split-screen.</summary>
    public bool TryArmForConsumedH(long nowMs, bool isMainPlayer, bool isSplitScreen, string requestId, string runtimeId) =>
        this.TryArm(nowMs, isMainPlayer, isSplitScreen, requestId, runtimeId, BgArmReason.ConsumedH);

    /// <summary>
    /// Stage 2 R2: arm BEFORE the owned Press. A false return means the caller must
    /// not Press. Refuses HOST/main-player, split-screen and an already Contained policy.
    /// </summary>
    public bool TryArmForPressOutstanding(long nowMs, bool isMainPlayer, bool isSplitScreen, string requestId, string runtimeId) =>
        this.TryArm(nowMs, isMainPlayer, isSplitScreen, requestId, runtimeId, BgArmReason.PressOutstanding);

    /// <summary>Arm when N0 leaves stale pre-H motion unresolved and the neutral-cleanup path starts.</summary>
    public bool TryArmForPreHUnresolved(long nowMs, bool isMainPlayer, bool isSplitScreen, string requestId, string runtimeId) =>
        this.TryArm(nowMs, isMainPlayer, isSplitScreen, requestId, runtimeId, BgArmReason.PreHUnresolved);

    private bool TryArm(long nowMs, bool isMainPlayer, bool isSplitScreen, string request, string runtime, BgArmReason reason)
    {
        if (isMainPlayer || isSplitScreen)
            return false;
        lock (this.gate)
        {
            if (this.State == BgContainmentState.Contained)
                return false;
            if (this.State != BgContainmentState.Armed)
            {
                this.State = BgContainmentState.Armed;
                this.ArmReason = reason;
                this.ArmedAtMs = nowMs;
                this.LastHeartbeatMs = nowMs;
                this.T1Streak = 0;
                this.stableCaptures = 0;
                this.DispatchImpossible = false;
                this.containRequested = BgContainmentTrigger.None;
                this.ReleaseRequested = false;
                this.releaseSignals.Clear();
                this.requestId = request;
                this.runtimeId = runtime;
                this.pressOutstanding = false;
                this.pressReleaseRequestedMs = null;
            }
            else if (reason == BgArmReason.ConsumedH)
            {
                this.ArmReason = BgArmReason.ConsumedH;
            }
            if (reason == BgArmReason.PressOutstanding)
            {
                this.pressOutstanding = true;
                this.pressReleaseRequestedMs = null;
            }
            this.LastConsumedFrameMs = nowMs;
            return true;
        }
    }

    /// <summary>Every consumed substituted frame (H, R, N or neutral cleanup) refreshes the T4 reference.</summary>
    public void OnFrameConsumed(long nowMs)
    {
        lock (this.gate)
        {
            if (this.State == BgContainmentState.Armed)
                this.LastConsumedFrameMs = nowMs;
        }
    }

    /// <summary>Game thread, once per UpdateTicked. Decides T1/T2 and RELEASED_BY_GAME.</summary>
    public void OnUpdateTicked(BgTickFacts f)
    {
        lock (this.gate)
        {
            BgTickFacts? previous = this.lastTick;
            this.lastTick = f;
            if (this.State != BgContainmentState.Armed)
                return;
            this.LastHeartbeatMs = f.NowMs;

            bool sameLocation = previous is BgTickFacts p && string.Equals(p.Location, f.Location, StringComparison.Ordinal);
            bool displaced = sameLocation && previous is BgTickFacts q && (q.PixelX != f.PixelX || q.PixelY != f.PixelY);
            bool stablePosition = sameLocation && !displaced;

            // Pause is not release proof. After independently proved Suppress /
            // key absence, a live native pause may suspend only the no-motion
            // timer. Heartbeat loss, an outstanding press, dispatch faults and
            // unexpected displacement still take their ordinary safety paths.
            if (f.ReleasedGamePause && !this.pressOutstanding && !this.DispatchImpossible && stablePosition)
                this.LastConsumedFrameMs = f.NowMs;

            // T1: background/inactive only. Foreground pass-through ticks never count and clear any partial streak.
            if (f.GameActive || f.SubstitutedThisTick || !displaced || f.ExpectedSeatMotion)
                this.T1Streak = 0;
            else
                this.T1Streak++;
            if (this.T1Streak >= this.Limits.T1ConsecutiveTicks)
                this.Request(BgContainmentTrigger.T1UncontrolledMotion);

            // T2: true pre-consumer dispatch-impossible with movement outstanding, on an inactive tick.
            if (this.DispatchImpossible && !f.GameActive && f.MovementDirectionsCount != 0)
                this.Request(BgContainmentTrigger.T2DispatchImpossible);

            // RELEASED_BY_GAME: callbacks absent, no movement, stable position across the required captures.
            // Never while an owned press override may be outstanding (Stage 2 N2).
            if (this.containRequested == BgContainmentTrigger.None && !this.pressOutstanding
                && !f.ExpectedSeatMotion && !f.SubstitutedThisTick && f.MovementDirectionsCount == 0 && stablePosition)
                this.stableCaptures++;
            else
                this.stableCaptures = 0;
            if (this.stableCaptures >= this.Limits.StableCapturesToRelease)
                this.State = BgContainmentState.DisarmedReleasedByGame;
        }
    }

    /// <summary>Recorded by the hook on PRECONSUMER_HISTORY_DIVERGENCE (Parent skipped).</summary>
    public void OnDispatchImpossible(long nowMs, bool gameActive, int movementDirectionsCount)
    {
        lock (this.gate)
        {
            if (this.State != BgContainmentState.Armed)
                return;
            this.DispatchImpossible = true;
            this.LastHeartbeatMs = Math.Max(this.LastHeartbeatMs, nowMs);
            if (!gameActive && movementDirectionsCount != 0)
                this.Request(BgContainmentTrigger.T2DispatchImpossible);
        }
    }

    /// <summary>Disarm path (a): verified N receipt, then movementDirections == 0 and stopped readback.</summary>
    public bool OnNeutralVerified(long nowMs, int movementDirectionsCount, bool stoppedReadback)
    {
        lock (this.gate)
        {
            if (this.State != BgContainmentState.Armed || this.containRequested != BgContainmentTrigger.None)
                return false;
            if (this.pressOutstanding)
                return false;
            if (this.lastTick?.ExpectedSeatMotion == true)
                return false;
            if (movementDirectionsCount != 0 || !stoppedReadback)
                return false;
            this.LastHeartbeatMs = Math.Max(this.LastHeartbeatMs, nowMs);
            this.State = BgContainmentState.DisarmedVerifiedNeutral;
            return true;
        }
    }

    /// <summary>A pinned seat left its exact native envelope. Sticky, including after input release.</summary>
    public void OnUnexpectedSeatMotion()
    {
        lock (this.gate)
            if (this.State == BgContainmentState.Armed) this.Request(BgContainmentTrigger.T1UncontrolledMotion);
    }

    /// <summary>
    /// Stage 2 R1/N2: the outstanding press override is resolved only when Suppress
    /// was issued, SMAPI reports the key suppressed, AND the key is absent from the
    /// effective state (a VERIFY receipt or the effective keyboard read in
    /// UpdateTicked; never SMAPI IsDown while inactive). Containment stays armed
    /// until a later OnNeutralVerified; a Contained policy is never reopened.
    /// </summary>
    public bool OnPressReleaseProved(long nowMs, bool suppressIssued, bool isSuppressed, bool keyAbsent)
    {
        lock (this.gate)
        {
            if (this.State != BgContainmentState.Armed || this.containRequested != BgContainmentTrigger.None)
                return false;
            if (!suppressIssued || !isSuppressed || !keyAbsent)
                return false;
            this.pressOutstanding = false;
            this.pressReleaseRequestedMs = null;
            this.LastHeartbeatMs = Math.Max(this.LastHeartbeatMs, nowMs);
            return true;
        }
    }

    /// <summary>
    /// Stage 2 B1: the press session is about to attempt release (called BEFORE the
    /// Suppress call, so a throwing Suppress still starts the deadline). Only the first
    /// attempt counts; later retries never extend the deadline.
    /// </summary>
    public void OnPressReleaseRequested(long nowMs)
    {
        lock (this.gate)
        {
            if (this.pressOutstanding && this.pressReleaseRequestedMs is null)
                this.pressReleaseRequestedMs = nowMs;
        }
    }

    // STOP, lease expiry, caller loss and request deadline only steer the plan toward R/N.
    // None of them disarms containment.
    public void OnStopLatched() => this.Signal("STOP");
    public void OnLeaseExpired() => this.Signal("LEASE_EXPIRED");
    public void OnCallerLost() => this.Signal("CALLER_LOST");
    public void OnDeadlineExpired() => this.Signal("DEADLINE");

    private void Signal(string signal)
    {
        lock (this.gate)
        {
            if (this.State != BgContainmentState.Armed)
                return;
            this.ReleaseRequested = true;
            this.releaseSignals.Add(signal);
        }
    }

    /// <summary>
    /// Watchdog thread, every poll. Returns at most one decision for the whole armed
    /// period; the caller's only action for it is self-termination.
    /// </summary>
    public BgContainmentDecision? Poll(long nowMs)
    {
        lock (this.gate)
        {
            if (this.State != BgContainmentState.Armed)
                return null;

            BgContainmentTrigger trigger = this.containRequested;
            if (trigger == BgContainmentTrigger.None && nowMs - this.LastHeartbeatMs >= this.Limits.T3HeartbeatLossMs)
                trigger = BgContainmentTrigger.T3HeartbeatLoss;

            // Stage 2 B1: an outstanding owned press has its own deadline, regardless of
            // heartbeat or movement (a Suppress that keeps faulting, or a release never proved).
            if (trigger == BgContainmentTrigger.None && this.pressOutstanding
                && ((this.pressReleaseRequestedMs is long requested && nowMs - requested >= this.Limits.PressReleaseBoundMs)
                    || nowMs - this.LastConsumedFrameMs >= this.Limits.PressStallBoundMs))
                trigger = BgContainmentTrigger.T5PressReleaseUnresolved;

            if (trigger == BgContainmentTrigger.None && this.lastTick is BgTickFacts f && !f.GameActive && f.MovementDirectionsCount != 0)
            {
                // T4 covers both an ARTI-held and an unresolved pre-H movement condition (V4.1 D2).
                if (f.GateClosed)
                {
                    if (nowMs - this.LastConsumedFrameMs >= this.Limits.T4GateClosedMs)
                        trigger = BgContainmentTrigger.T4GateClosedUnresolved;
                }
                else if (this.ArmReason == BgArmReason.PreHUnresolved
                    && nowMs - this.ArmedAtMs >= this.Limits.PreHCleanupBoundMs)
                    trigger = BgContainmentTrigger.PreHCleanupTimeout;
            }

            if (trigger == BgContainmentTrigger.None)
                return null;

            BgContainmentSnapshot snapshot = this.Snapshot();
            this.State = BgContainmentState.Contained;
            this.decision = new BgContainmentDecision(trigger,
                FailFastPrefix + trigger + "|request=" + this.requestId + "|runtime=" + this.runtimeId, snapshot);
            return this.decision;
        }
    }

    public BgContainmentDecision? Decision { get { lock (this.gate) return this.decision; } }

    private void Request(BgContainmentTrigger trigger)
    {
        if (this.containRequested == BgContainmentTrigger.None)
            this.containRequested = trigger;
    }

    private BgContainmentSnapshot Snapshot()
    {
        BgTickFacts f = this.lastTick ?? default;
        return new BgContainmentSnapshot(this.requestId, this.runtimeId, this.ArmReason, this.ArmedAtMs, this.LastHeartbeatMs,
            this.LastConsumedFrameMs, f.MovementDirectionsCount, f.Location ?? "", f.PixelX, f.PixelY, f.GameActive,
            this.T1Streak, this.DispatchImpossible, this.pressOutstanding);
    }
}
