namespace ArtiStardewBridge.Protocol;

/// <summary>
/// G2 native configured-use-tool adapter: pin, per-tick Press translation,
/// and application-boundary cleanup debt. SMAPI-free so ProtocolTests can
/// exercise the contract without Stardew.
///
/// Bukan owner/lease/inbox/backend kedua. <see cref="UseToolHoldBackend.Tick"/>
/// tetap mesin niat; adapter hanya mengonsumsi fase/output-nya.
/// Seam in-process <see cref="SetCallerIntent"/> disediakan untuk pemanggil
/// G6/skill kelak — slice ini tidak menemu pemanggil itu.
/// </summary>
public sealed class UseToolNativeAdapterState
{
    UseToolHoldIntent callerIntent = UseToolHoldIntent.Neutral;
    string? pinnedButton;
    string? pinnedFingerprint;
    UseToolHoldBindingFence? pinnedFence;
    bool suppressIssued;
    bool nativeCleanupDebt;
    bool uncertainEffect;
    bool lifecycleFailClosed;
    bool sameHandlerAfterSuppress;
    int applicationCyclesSinceSuppress;
    bool replayLatched;
    // Receipt bookkeeping belongs to this adapter, not a second input owner.
    bool receiptArmed;
    bool receiptInvalid;
    uint receiptSuppressTick;
    uint? receiptLastTick;
    uint receiptTick;
    long receiptGameTick;
    UseToolHoldBindingFence receiptFence;
    bool receiptOpen;
    bool receiptEligible;
    bool receiptAdapterTick;
    int receiptGameUpdates;
    bool receiptGameSucceeded;

    public UseToolHoldIntent CallerIntent => this.callerIntent;
    public string? PinnedButton => this.pinnedButton;
    public string? PinnedFingerprint => this.pinnedFingerprint;
    public bool HasNativeCleanupDebt => this.nativeCleanupDebt;
    public bool UncertainEffect => this.uncertainEffect;
    public bool LifecycleFailClosed => this.lifecycleFailClosed;
    public bool SameHandlerAfterSuppress => this.sameHandlerAfterSuppress;
    public int ApplicationCyclesSinceSuppress => this.applicationCyclesSinceSuppress;
    public bool ReplayLatched => this.replayLatched;

    public bool HasOwnedNativeSurface =>
        this.pinnedButton is not null
        || this.nativeCleanupDebt
        || this.lifecycleFailClosed;

    /// <summary>
    /// Satu-satunya pintu niat in-process. Bukan op inbox, bukan skill
    /// domain, bukan perintah per-frame Python.
    /// </summary>
    public void SetCallerIntent(UseToolHoldIntent intent) =>
        this.callerIntent = intent;

    public static bool TryResolveMappedButton(string? mappedButton, out string button)
    {
        button = "";
        if (string.IsNullOrEmpty(mappedButton))
            return false;
        if (string.Equals(mappedButton, "None", StringComparison.OrdinalIgnoreCase))
            return false;
        button = mappedButton;
        return true;
    }

    public UseToolNativeAdapterPlan Tick(
        UseToolHoldBackend backend,
        MutationAdvice advice,
        bool leaseMatches,
        string? leaseRejectReason,
        UseToolHoldBindingFence currentFence,
        string? mappedButton,
        string? bindingFingerprint,
        bool worldReady,
        bool eligibleUpdateTicking
    )
    {
        if (this.receiptOpen && currentFence == this.receiptFence)
            this.receiptAdapterTick = true;
        this.EnterLaterEligibleTick(eligibleUpdateTicking);

        bool mapped = TryResolveMappedButton(mappedButton, out string currentButton);
        string? currentFingerprint = mapped ? (bindingFingerprint ?? currentButton) : null;
        bool bindingChanged = this.pinnedButton is not null
            && (!string.Equals(this.pinnedButton, currentButton, StringComparison.Ordinal)
                || !string.Equals(this.pinnedFingerprint, currentFingerprint, StringComparison.Ordinal));

        UseToolHoldIntent intent = this.callerIntent;
        bool bindingMapped = mapped;

        if (this.replayLatched || this.lifecycleFailClosed)
            intent = UseToolHoldIntent.Neutral;

        if (!worldReady)
        {
            if (backend.Phase == UseToolHoldPhase.Idle && !backend.HasOwnedInflight)
                intent = UseToolHoldIntent.Neutral;
            else if (backend.HasOwnedInflight)
                intent = UseToolHoldIntent.Neutral;
        }

        if (bindingChanged && backend.HasOwnedInflight)
        {
            intent = UseToolHoldIntent.Neutral;
            bindingMapped = true;
        }

        if (this.uncertainEffect && backend.Phase == UseToolHoldPhase.Holding)
            intent = UseToolHoldIntent.Neutral;

        UseToolHoldTickResult tick = backend.Tick(
            advice,
            leaseMatches,
            intent,
            currentFence,
            bindingMapped,
            press: null,
            suppress: null,
            leaseRejectReason);

        if (tick.Phase == UseToolHoldPhase.Holding
            && tick.Output == UseToolHoldOutput.Press
            && mapped
            && this.pinnedButton is null)
        {
            this.pinnedButton = currentButton;
            this.pinnedFingerprint = currentFingerprint;
            this.pinnedFence = currentFence;
        }

        UseToolNativeAction action = UseToolNativeAction.None;
        string? actionButton = null;
        bool sameHandlerAckForbidden = false;

        bool blocked = tick.Phase == UseToolHoldPhase.ReleaseOwed
            || this.uncertainEffect
            || this.lifecycleFailClosed
            || this.replayLatched;

        if (tick.Phase == UseToolHoldPhase.Holding
            && !blocked
            && this.pinnedButton is not null
            && eligibleUpdateTicking
            && worldReady)
        {
            action = UseToolNativeAction.Press;
            actionButton = this.pinnedButton;
        }
        else if (tick.Phase == UseToolHoldPhase.ReleaseOwed
            && this.pinnedButton is not null
            && !this.suppressIssued)
        {
            action = UseToolNativeAction.Suppress;
            actionButton = this.pinnedButton;
            this.nativeCleanupDebt = true;
            this.sameHandlerAfterSuppress = true;
            this.applicationCyclesSinceSuppress = 0;
            sameHandlerAckForbidden = true;
        }

        return new UseToolNativeAdapterPlan(
            action,
            actionButton,
            tick.Phase,
            tick.Output,
            tick.RequestOwnedRelease,
            tick.Reason,
            sameHandlerAckForbidden);
    }

    public void NoteNativePressIssued()
    {
        // Press sehat tidak menghapus utang dan tidak ACK.
    }

    public void NoteNativeSuppressIssued()
    {
        this.suppressIssued = true;
        this.nativeCleanupDebt = true;
        this.sameHandlerAfterSuppress = true;
        this.applicationCyclesSinceSuppress = 0;
        // Called only after the native Suppress returned. Keep the original
        // Hold fence/pin; a lifecycle call outside Ticking cannot arm a receipt.
        if (!this.receiptArmed && this.receiptOpen && this.receiptAdapterTick
            && this.pinnedFence == this.receiptFence)
        {
            this.receiptArmed = true;
            this.receiptSuppressTick = this.receiptTick;
        }
    }

    public void BeginCleanupUpdate(uint tick, long gameTick, UseToolHoldBindingFence fence, bool eligible)
    {
        if (this.HasOwnedNativeSurface && (this.receiptOpen
            || (this.receiptLastTick is uint last && (last == uint.MaxValue || tick != last + 1))))
            this.receiptInvalid = true;
        if (this.pinnedFence is UseToolHoldBindingFence pin && pin != fence)
            this.receiptInvalid = true;
        this.receiptOpen = true;
        this.receiptTick = tick;
        this.receiptGameTick = gameTick;
        this.receiptFence = fence;
        this.receiptEligible = eligible;
        this.receiptAdapterTick = false;
        this.receiptGameUpdates = 0;
        this.receiptGameSucceeded = false;
    }

    /// <summary>Read-only completion witness from the actual game update,
    /// not the SMAPI event (which is also raised after a caught game exception).</summary>
    public void NoteCleanupGameUpdate(long gameTick, UseToolHoldBindingFence fence, bool eligible, bool succeeded)
    {
        if (!this.receiptOpen)
            return;
        this.receiptGameUpdates++;
        bool sameFence = fence == this.receiptFence && this.pinnedFence == fence;
        this.receiptGameSucceeded = succeeded && this.receiptGameUpdates == 1
            && this.receiptEligible && eligible && sameFence && gameTick > this.receiptGameTick;
        if (this.HasOwnedNativeSurface && (!succeeded || !sameFence || this.receiptGameUpdates != 1
            || (this.receiptEligible && eligible && gameTick <= this.receiptGameTick)))
            this.receiptInvalid = true;
    }

    public void NoteCleanupBoundaryFailure()
    {
        if (this.HasOwnedNativeSurface)
            this.receiptInvalid = true;
    }

    public bool TryCompleteCleanupUpdate(UseToolHoldBackend backend, uint tick,
        UseToolHoldBindingFence fence, bool eligible, string expectedPin, bool? isDown)
    {
        if (!this.receiptOpen || tick != this.receiptTick)
        {
            this.NoteCleanupBoundaryFailure();
            return false;
        }
        this.receiptOpen = false;
        this.receiptLastTick = tick;
        if (this.HasOwnedNativeSurface && (!this.receiptAdapterTick
            || fence != this.receiptFence
            || (this.receiptEligible && eligible && this.receiptGameUpdates != 1)))
            this.receiptInvalid = true;

        if (!this.receiptArmed || this.receiptInvalid || tick <= this.receiptSuppressTick
            || !this.receiptGameSucceeded || !this.receiptEligible || !eligible
            || fence.IsMainPlayer || fence.IsSplitScreen || fence.PlayerId == 0
            || this.pinnedFence != fence || isDown != false)
            return false;

        // Later successful eligible game update + matching original-fence Up.
        // The existing seam retains the uncertainty/lifecycle/debt/rearm rules.
        return this.TryAcknowledgeCleanupAfterApplicationBoundary(
            backend, expectedPin, matchingPin: this.pinnedButton == expectedPin,
            applicationCycleElapsed: true, matchingUpReadback: true, issuingHandler: false);
    }

    public void NoteNativeException()
    {
        this.uncertainEffect = true;
        this.replayLatched = true;
        this.nativeCleanupDebt = this.pinnedButton is not null || this.nativeCleanupDebt;
        this.suppressIssued = false;
    }

    public void NoteLifecycleReset()
    {
        this.lifecycleFailClosed = this.pinnedButton is not null || this.nativeCleanupDebt;
        this.callerIntent = UseToolHoldIntent.Neutral;
        this.replayLatched = true;
        if (this.pinnedButton is not null)
            this.nativeCleanupDebt = true;
    }

    /// <summary>
    /// ACK hanya pada batas aplikasi/readback yang belakangan dan cocok.
    /// Handler yang sama dengan Suppress, <c>UpdateTicked</c> telanjang, atau
    /// IsDown di handler yang sama tidak cukup. Tanpa readback yang jujur,
    /// utang tetap fail-closed.
    /// </summary>
    public bool TryAcknowledgeCleanupAfterApplicationBoundary(
        UseToolHoldBackend backend,
        string expectedPin,
        bool matchingPin,
        bool applicationCycleElapsed,
        bool matchingUpReadback,
        bool issuingHandler
    )
    {
        if (issuingHandler || this.sameHandlerAfterSuppress)
            return false;
        if (!this.nativeCleanupDebt || this.pinnedButton is null)
            return false;
        if (!matchingPin
            || !string.Equals(this.pinnedButton, expectedPin, StringComparison.Ordinal))
        {
            return false;
        }

        if (!applicationCycleElapsed || this.applicationCyclesSinceSuppress < 1)
            return false;
        if (!matchingUpReadback)
            return false;
        if (this.uncertainEffect || this.lifecycleFailClosed)
            return false;
        if (backend.Phase != UseToolHoldPhase.ReleaseOwed)
            return false;

        backend.AcknowledgeCleanup();
        this.ClearAfterHonestAck();
        return true;
    }

        void EnterLaterEligibleTick(bool eligibleUpdateTicking)
    {
        if (!eligibleUpdateTicking)
            return;
        if (this.sameHandlerAfterSuppress)
        {
            // Tick berikutnya adalah handler belakangan, bukan yang mengeluarkan Suppress.
            this.sameHandlerAfterSuppress = false;
            if (this.nativeCleanupDebt && this.suppressIssued)
                this.applicationCyclesSinceSuppress = Math.Max(this.applicationCyclesSinceSuppress, 1);
            return;
        }

        if (this.nativeCleanupDebt && this.suppressIssued)
            this.applicationCyclesSinceSuppress++;
    }

    void ClearAfterHonestAck()
    {
        this.receiptArmed = false;
        this.receiptInvalid = false;
        this.pinnedButton = null;
        this.pinnedFingerprint = null;
        this.pinnedFence = null;
        this.suppressIssued = false;
        this.nativeCleanupDebt = false;
        this.uncertainEffect = false;
        this.lifecycleFailClosed = false;
        this.sameHandlerAfterSuppress = false;
        this.applicationCyclesSinceSuppress = 0;
        this.replayLatched = false;
        this.callerIntent = UseToolHoldIntent.Neutral;
    }
}

public enum UseToolNativeAction
{
    None,
    Press,
    Suppress,
}

public readonly record struct UseToolNativeAdapterPlan(
    UseToolNativeAction Action,
    string? Button,
    UseToolHoldPhase BackendPhase,
    UseToolHoldOutput BackendOutput,
    bool RequestOwnedRelease,
    string? Reason,
    bool SameHandlerAckForbidden
);
