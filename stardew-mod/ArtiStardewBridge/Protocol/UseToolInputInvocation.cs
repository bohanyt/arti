namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Finite command caller for the existing configured-use-tool adapter.
/// Owns duration/result bookkeeping only: no native input, lease, button pin,
/// cleanup ACK, transport or world-state mutation lives here.
/// </summary>
public sealed class UseToolInputInvocation
{
    public const int MaxTicks = 60;
    public const int CleanupTimeoutMs = 3000;
    readonly UseToolHoldBackend backend;
    readonly UseToolNativeAdapterState adapter;
    string? requestId;
    UseToolInputTarget target;
    UseToolHoldBindingFence fence;
    int requestedTicks;
    int presses;
    long deadline;
    long lastNow;
    uint? lastTick;
    uint? lastPressTick;
    bool releaseRequested;
    bool cycleObserved;
    string? interruption;

    public UseToolInputInvocation(UseToolHoldBackend backend, UseToolNativeAdapterState adapter)
    {
        this.backend = backend ?? throw new ArgumentNullException(nameof(backend));
        this.adapter = adapter ?? throw new ArgumentNullException(nameof(adapter));
    }

    public bool Active => this.requestId is not null && this.Verdict is null;
    public UseToolInputVerdict? Verdict { get; private set; }

    public bool TryStart(string requestId, int ticks, UseToolInputTarget target,
        UseToolHoldBindingFence fence, long nowUnixMs)
    {
        if (this.Active || this.backend.HasOwnedInflight || this.adapter.HasOwnedNativeSurface
            || this.adapter.UncertainEffect || this.adapter.ReplayLatched
            || this.adapter.CallerIntent != UseToolHoldIntent.Neutral
            || string.IsNullOrWhiteSpace(requestId) || requestId.Length > 128
            || requestId == this.requestId || ticks < 1 || ticks > MaxTicks || !target.IsValid
            || fence.IsMainPlayer || fence.IsSplitScreen || fence.PlayerId == 0 || fence.ScreenId < 0
            || string.IsNullOrWhiteSpace(fence.RuntimeInstanceId) || string.IsNullOrWhiteSpace(fence.SessionId)
            || string.IsNullOrWhiteSpace(fence.LocationId) || fence.ContextGeneration < 1
            || nowUnixMs < 0 || nowUnixMs > long.MaxValue - CleanupTimeoutMs - 1000)
            return false;
        this.requestId = requestId;
        this.target = target;
        this.fence = fence;
        this.requestedTicks = ticks;
        this.presses = 0;
        this.deadline = nowUnixMs + CleanupTimeoutMs + (ticks * 1000 + 59) / 60;
        this.lastNow = nowUnixMs;
        this.lastTick = this.lastPressTick = null;
        this.releaseRequested = false;
        this.cycleObserved = false;
        this.interruption = null;
        this.Verdict = null;
        // Admission itself never emits or queues a Hold. Recheck at native tick.
        return true;
    }

    public void PrepareTick(uint tick, long nowUnixMs, UseToolInputTarget currentTool,
        UseToolHoldBindingFence currentFence, bool enabled, bool eligible, bool leaseMatches)
    {
        if (!this.Active) return;
        if (this.lastTick is uint previous && tick <= previous || nowUnixMs < this.lastNow)
            this.Interrupt("NonAdvancingUpdate");
        this.lastTick = tick;
        this.lastNow = nowUnixMs;
        if (!enabled) this.Interrupt("CapabilityDisabled");
        if (!eligible) this.Interrupt("IneligibleUpdate");
        if (!leaseMatches) this.Interrupt("LeaseLost");
        if (currentTool != this.target) this.Interrupt("EquippedToolOrPoseChanged");
        if (currentFence != this.fence) this.Interrupt("ContextChanged");
        if (this.adapter.UncertainEffect || this.adapter.LifecycleFailClosed || this.adapter.ReplayLatched)
            this.Interrupt("NativeInputUncertain");
        if (nowUnixMs >= this.deadline) this.Interrupt("InputTimeout");
        if (this.presses >= this.requestedTicks) this.releaseRequested = true;
        this.adapter.SetCallerIntent(this.releaseRequested ? UseToolHoldIntent.Release : UseToolHoldIntent.Hold);
    }

    /// <summary>Called only after the existing native Press returned.</summary>
    public void NoteNativePressIssued(uint tick)
    {
        if (!this.Active) return;
        this.presses++;
        if (this.releaseRequested || this.lastTick != tick || this.lastPressTick == tick
            || this.presses > this.requestedTicks)
        {
            this.adapter.NoteNativeException();
            this.Interrupt("UnexpectedPressReceipt");
        }
        this.lastPressTick = tick;
        if (this.presses >= this.requestedTicks)
        {
            this.releaseRequested = true;
            this.adapter.SetCallerIntent(UseToolHoldIntent.Release);
        }
    }

    public void Interrupt(string reason)
    {
        if (!this.Active) return;
        this.interruption ??= reason;
        this.releaseRequested = true;
        this.adapter.SetCallerIntent(UseToolHoldIntent.Release);
    }

    /// <summary>
    /// Observe only after the existing adapter's real cleanup-receipt boundary.
    /// Elapsed duration, a Suppress plan or an Up value passed here cannot ACK it.
    /// </summary>
    public void ObserveAfterUpdate(long nowUnixMs)
    {
        if (!this.Active) return;
        if (this.backend.Phase == UseToolHoldPhase.ReleaseOwed && !this.releaseRequested)
            this.Interrupt("NativeInputInterrupted");
        if (this.adapter.UncertainEffect || this.adapter.LifecycleFailClosed || this.adapter.ReplayLatched)
            this.Interrupt("NativeInputUncertain");
        bool clean = !this.adapter.HasOwnedNativeSurface && !this.backend.HasOwnedInflight
            && !this.adapter.UncertainEffect && !this.adapter.ReplayLatched;
        if (this.releaseRequested && clean)
        {
            bool completed = this.interruption is null && this.presses == this.requestedTicks;
            this.Verdict = new(this.requestId!, completed ? "InputCompleted" : "InputInterrupted",
                this.presses, true, this.interruption, this.cycleObserved);
            this.adapter.SetCallerIntent(UseToolHoldIntent.Neutral);
        }
        else if (nowUnixMs >= this.deadline)
        {
            this.Interrupt("InputTimeout");
            this.Verdict = new(this.requestId!, "InputUncertain", this.presses, false, this.interruption);
            // Keep Release/debt in the original adapter. No ACK or rearm here.
        }
    }

    // Read-only animation cue from the actual player after a native game update.
    // It never changes input/cleanup decisions or claims target contact/damage.
    public void ObserveUseCycle(bool usingTool)
    {
        if (this.Active && this.presses > 0 && usingTool) this.cycleObserved = true;
    }
}

public readonly record struct UseToolInputTarget(int Slot, string QualifiedItemId, int TileX, int TileY, int Facing)
{
    public bool IsValid => Slot is >= 0 and <= 255
        && !string.IsNullOrWhiteSpace(QualifiedItemId) && QualifiedItemId.Length <= 128
        && TileX is >= 0 and <= 10000 && TileY is >= 0 and <= 10000 && Facing is >= 0 and <= 3;
}

/// <summary>Input lifecycle only. No world-effect success field.</summary>
public sealed record UseToolInputVerdict(string RequestId, string Outcome, int PressCount,
    bool CleanupConfirmed, string? Reason, bool CycleObserved = false);
