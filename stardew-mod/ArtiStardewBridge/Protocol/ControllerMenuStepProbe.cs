using System.Globalization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Kandidat aksi P5-R3C: <c>menu_step(direction)</c>.
/// Baca native snap awal, tahan analog, hentikan input pada UpdateTicking
/// BERIKUTNYA setelah UpdateTicked melihat snap berubah. Timeout keras.
/// Sukses HANYA jika snap akhir <em>setelah Game.Update tick pelepasan</em>
/// = derived neighbor — bukan karena Press/Suppress terkirim.
/// Unmoved / tujuan salah / overshoot / timeout / cleanup / snap hilang
/// dilaporkan apa adanya.
///
/// Disabled-by-default. Bukan motor production, bukan A/B/Start, bukan
/// Harmony. Hold-duration (<see cref="ControllerStickHoldProbe"/>) hanya
/// diagnostik pendukung.
///
/// Urutan event SMAPI: UpdateTicking (Press) → Game.Update → UpdateTicked
/// (baca snap). Jadi deteksi di Ticked tick T dilepas di Ticking tick T+1,
/// bukan di callback observasi.
/// </summary>
public sealed class ControllerMenuStepProbe
{
    public const string CommandName = "arti_menu_step";
    public const int DefaultTimeoutTicks = 60;
    public const int MinTimeoutTicks = 1;
    public const int MaxTimeoutTicks = 60;
    public const int MinDelayTicks = 0;
    public const int MaxDelayTicks = 1800;

    private static int nextRunId = 1;

    private StickHoldPhase phase = StickHoldPhase.Idle;
    private int remainingDelayTicks;
    private int remainingTimeoutTicks;
    private int timeoutTicks;
    private int pressCount;
    private int releaseIssuedCount;
    private bool releaseOwed;
    private string? direction;
    private int runId;
    private string? armedMenuType;
    private string? armedPageType;
    private int armedScreenId;
    private uint lastTickingTick;
    private bool haveLastTickingTick;
    private bool terminalCleanup;
    private bool changeSeen;
    private bool timeoutReached;
    private bool awaitingEndSnap;
    private int? startSnapId;
    private int? expectedNeighborId;
    private int? lastObservedSnapId;
    private int? firstChangedSnapId;
    private MenuStepOutcome outcome = MenuStepOutcome.InProgress;

    public StickHoldPhase Phase => this.phase;
    public bool IsActive => this.phase != StickHoldPhase.Idle;
    public string? ActiveDirection => this.direction;
    public int RemainingDelayTicks => this.remainingDelayTicks;
    public int RemainingTimeoutTicks => this.remainingTimeoutTicks;
    public int TimeoutTicks => this.timeoutTicks;
    public int PressCount => this.pressCount;
    public int ReleaseIssuedCount => this.releaseIssuedCount;
    public bool ReleaseOwed => this.releaseOwed;
    public int RunId => this.runId;
    public bool ChangeSeen => this.changeSeen;
    public bool TimeoutReached => this.timeoutReached;
    public bool AwaitingEndSnap => this.awaitingEndSnap;
    public int? StartSnapId => this.startSnapId;
    public int? ExpectedNeighborId => this.expectedNeighborId;
    public int? EndSnapId => this.lastObservedSnapId;
    public MenuStepOutcome Outcome => this.outcome;

    public bool IsSuccess => this.outcome == MenuStepOutcome.Stepped;

    public static bool TryParseCommand(
        string[]? args,
        out string direction,
        out int timeoutTicks,
        out int delayTicks,
        out StickHoldStatus error
    )
    {
        direction = "";
        timeoutTicks = DefaultTimeoutTicks;
        delayTicks = 0;
        if (args is null || args.Length is < 1 or > 3
            || !ControllerStickHoldProbe.TryNormalizeDirection(args[0], out direction))
        {
            error = StickHoldStatus.RejectedInvalidDirection;
            direction = "";
            timeoutTicks = 0;
            return false;
        }

        if (args.Length >= 2
            && (!int.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out timeoutTicks)
                || timeoutTicks < MinTimeoutTicks
                || timeoutTicks > MaxTimeoutTicks))
        {
            error = StickHoldStatus.RejectedInvalidHold;
            direction = "";
            timeoutTicks = 0;
            delayTicks = 0;
            return false;
        }

        if (args.Length == 3
            && (!int.TryParse(args[2], NumberStyles.None, CultureInfo.InvariantCulture, out delayTicks)
                || delayTicks < MinDelayTicks
                || delayTicks > MaxDelayTicks))
        {
            error = StickHoldStatus.RejectedInvalidDelay;
            direction = "";
            timeoutTicks = 0;
            delayTicks = 0;
            return false;
        }

        error = StickHoldStatus.Started;
        return true;
    }

    public StickHoldStatus TryArm(
        string direction,
        int timeoutTicks,
        int delayTicks,
        StickHoldRuntimeContext context,
        int? startSnapId,
        int? expectedNeighborId
    )
    {
        if (!ControllerStickHoldProbe.TryNormalizeDirection(direction, out string key))
            return StickHoldStatus.RejectedInvalidDirection;
        if (timeoutTicks < MinTimeoutTicks || timeoutTicks > MaxTimeoutTicks)
            return StickHoldStatus.RejectedInvalidHold;
        if (delayTicks < MinDelayTicks || delayTicks > MaxDelayTicks)
            return StickHoldStatus.RejectedInvalidDelay;
        if (!context.Enabled)
            return StickHoldStatus.RejectedDisabled;
        if (!context.World.IsWorldReady)
            return StickHoldStatus.RejectedWorldNotReady;
        if (!context.World.IsMultiplayer)
            return StickHoldStatus.RejectedNotMultiplayer;
        if (context.World.IsMainPlayer)
            return StickHoldStatus.RejectedHost;
        if (context.World.IsSplitScreen)
            return StickHoldStatus.RejectedSplitScreen;
        if (!context.MenuOpen)
            return StickHoldStatus.RejectedNoMenu;
        if (!context.ExistingSnapPresent || !startSnapId.HasValue)
            return StickHoldStatus.RejectedNoExistingSnap;
        if (context.OtherProbeBusy || this.IsActive || this.releaseOwed)
            return StickHoldStatus.RejectedAlreadyActive;

        this.direction = key;
        this.timeoutTicks = timeoutTicks;
        this.remainingTimeoutTicks = timeoutTicks;
        this.remainingDelayTicks = delayTicks;
        this.phase = StickHoldPhase.Waiting;
        this.pressCount = 0;
        this.releaseIssuedCount = 0;
        this.releaseOwed = false;
        this.runId = nextRunId++;
        this.armedMenuType = context.MenuType;
        this.armedPageType = context.PageType;
        this.armedScreenId = context.ScreenId;
        this.haveLastTickingTick = false;
        this.terminalCleanup = false;
        this.changeSeen = false;
        this.timeoutReached = false;
        this.awaitingEndSnap = false;
        this.startSnapId = startSnapId;
        this.expectedNeighborId = expectedNeighborId is >= 0 ? expectedNeighborId : null;
        this.lastObservedSnapId = startSnapId;
        this.firstChangedSnapId = null;
        this.outcome = MenuStepOutcome.InProgress;
        return StickHoldStatus.Started;
    }

    /// <summary>
    /// Observasi saja. Dipanggil dari UpdateTicked. Tidak menulis input.
    /// Snap ≠ awal saat hold → minta rilis pada ConsumeTicking berikutnya.
    /// Setelah Suppress, readback di sini yang mengunci outcome dari snap
    /// akhir yang fresh — bukan dari niat rilis di UpdateTicking.
    /// </summary>
    public void NoteObservedSnap(int? snapId, StickHoldRuntimeContext? context = null)
    {
        if (this.phase == StickHoldPhase.Idle && !this.releaseOwed && !this.awaitingEndSnap)
            return;

        if (this.awaitingEndSnap)
        {
            this.lastObservedSnapId = snapId;
            if (snapId is not null
                && this.startSnapId.HasValue
                && snapId.Value != this.startSnapId.Value)
            {
                this.changeSeen = true;
                this.firstChangedSnapId ??= snapId;
            }

            this.FinalizeAfterRelease(context);
            return;
        }

        if (snapId is null)
            return;

        this.lastObservedSnapId = snapId;
        if (this.phase is not (StickHoldPhase.Holding or StickHoldPhase.Waiting))
            return;
        if (!this.startSnapId.HasValue || snapId.Value == this.startSnapId.Value)
            return;

        this.changeSeen = true;
        this.firstChangedSnapId ??= snapId;

        if (this.phase == StickHoldPhase.Waiting && this.pressCount == 0 && !this.releaseOwed)
            this.AbortBeforeFirstPress();
    }

    public StickHoldTickingResult ConsumeTicking(uint updateTick, StickHoldRuntimeContext context)
    {
        if (this.haveLastTickingTick && this.lastTickingTick == updateTick)
        {
            return new StickHoldTickingResult(StickHoldTickingKind.Idle, this.remainingDelayTicks, this.direction);
        }

        if (this.phase == StickHoldPhase.Idle && !this.releaseOwed)
        {
            return new StickHoldTickingResult(StickHoldTickingKind.Idle, 0, null);
        }

        this.lastTickingTick = updateTick;
        this.haveLastTickingTick = true;

        if (this.releaseOwed && this.phase is StickHoldPhase.Idle or StickHoldPhase.Releasing)
        {
            this.phase = StickHoldPhase.Releasing;
            return new StickHoldTickingResult(StickHoldTickingKind.CleanupRelease, 0, this.direction);
        }

        if (this.phase is StickHoldPhase.Waiting or StickHoldPhase.Holding)
        {
            if (!context.Enabled
                || !context.World.IsWorldReady
                || !context.World.IsMultiplayer
                || context.World.IsMainPlayer
                || context.World.IsSplitScreen)
            {
                return this.AbortForContext();
            }

            bool contextChanged = context.MenuType != this.armedMenuType
                || context.PageType != this.armedPageType
                || context.ScreenId != this.armedScreenId
                || !context.MenuOpen
                || !context.ExistingSnapPresent;

            if (contextChanged)
            {
                if (this.pressCount > 0 || this.releaseOwed)
                    return this.AbortForContext();
                this.ResetBookkeeping(keepOwedRelease: false);
                this.outcome = MenuStepOutcome.Cleanup;
                return new StickHoldTickingResult(StickHoldTickingKind.Rejected, 0, null);
            }
        }

        if (this.phase == StickHoldPhase.Waiting)
        {
            if (this.changeSeen && this.pressCount == 0 && !this.releaseOwed)
                return this.AbortBeforeFirstPressResult();

            if (this.remainingDelayTicks > 0)
            {
                this.remainingDelayTicks--;
                return new StickHoldTickingResult(
                    StickHoldTickingKind.Waiting, this.remainingDelayTicks, this.direction);
            }

            this.phase = StickHoldPhase.Holding;
        }

        if (this.phase == StickHoldPhase.Holding)
        {
            if (this.changeSeen && this.pressCount == 0 && !this.releaseOwed)
                return this.AbortBeforeFirstPressResult();

            if (this.changeSeen || this.remainingTimeoutTicks <= 0)
            {
                if (!this.changeSeen)
                    this.timeoutReached = true;
                this.phase = StickHoldPhase.Releasing;
                return new StickHoldTickingResult(StickHoldTickingKind.Release, 0, this.direction);
            }

            return new StickHoldTickingResult(StickHoldTickingKind.Press, 0, this.direction);
        }

        if (this.phase == StickHoldPhase.Releasing)
        {
            return new StickHoldTickingResult(
                this.releaseOwed ? StickHoldTickingKind.Release : StickHoldTickingKind.Idle,
                0,
                this.direction
            );
        }

        return new StickHoldTickingResult(StickHoldTickingKind.Idle, 0, this.direction);
    }

    public void AcknowledgePress()
    {
        if (this.phase != StickHoldPhase.Holding || this.remainingTimeoutTicks <= 0)
            return;
        this.pressCount++;
        this.remainingTimeoutTicks--;
        this.releaseOwed = true;
    }

    public void AcknowledgeRelease()
    {
        if (!this.releaseOwed)
            return;
        this.releaseOwed = false;
        this.releaseIssuedCount++;
        if (this.terminalCleanup)
        {
            this.ResetBookkeeping(keepOwedRelease: false);
            this.terminalCleanup = false;
            return;
        }

        this.awaitingEndSnap = true;
        this.phase = StickHoldPhase.Releasing;
    }

    public StickHoldObserveResult ConsumeTicked(uint updateTick)
    {
        _ = updateTick;
        if (this.phase == StickHoldPhase.Idle && !this.releaseOwed)
            return StickHoldObserveResult.Skip() with { RunId = this.runId };

        bool dense = this.phase is StickHoldPhase.Holding or StickHoldPhase.Releasing;
        return new StickHoldObserveResult(
            ShouldLog: dense || this.phase == StickHoldPhase.Waiting,
            Dense: dense,
            Offset: this.pressCount,
            PhaseLabel: this.phase == StickHoldPhase.Releasing
                ? ControllerStickHoldLabels.ReleaseIntent
                : this.phase == StickHoldPhase.Waiting
                    ? ControllerStickHoldLabels.Waiting
                    : ControllerStickHoldLabels.Holding,
            ReleaseIntent: this.phase == StickHoldPhase.Releasing || this.changeSeen,
            SuppressAlreadyIssued: this.releaseIssuedCount > 0,
            RunId: this.runId,
            RequestedHoldTicks: this.timeoutTicks,
            RemainingHoldTicks: this.remainingTimeoutTicks,
            PressCount: this.pressCount,
            ReleaseIssuedCount: this.releaseIssuedCount
        );
    }

    public void RequestCleanup(string reason)
    {
        _ = reason;
        // Putusan terminal setelah rilis + readback itu sticky. ReturnedToTitle
        // / ForceCleanup tidak boleh menimpa Stepped (atau Timeout/Overshoot/
        // Cleanup yang sudah terkunci) dan tidak boleh menghidupkan utang
        // rilis dari Press hold yang di-ack berulang: satu Suppress fisik
        // melunasi seluruh hold. pressCount > releaseIssuedCount BUKAN utang.
        if (this.HasLockedTerminalVerdict())
            return;

        this.terminalCleanup = true;
        this.outcome = MenuStepOutcome.Cleanup;
        bool keep = this.releaseOwed;
        this.ResetBookkeeping(keepOwedRelease: keep);
        if (keep)
            this.phase = StickHoldPhase.Releasing;
    }

    private bool HasLockedTerminalVerdict() =>
        this.phase == StickHoldPhase.Idle
        && !this.releaseOwed
        && !this.awaitingEndSnap
        && this.outcome != MenuStepOutcome.InProgress;

    public MenuStepVerdict Verdict() => new(
        this.runId,
        this.startSnapId,
        this.lastObservedSnapId,
        this.expectedNeighborId,
        this.firstChangedSnapId,
        this.pressCount,
        this.changeSeen,
        this.timeoutReached,
        this.outcome,
        Moved: this.startSnapId.HasValue
            && this.lastObservedSnapId.HasValue
            && this.startSnapId != this.lastObservedSnapId,
        NeighborMatched: this.expectedNeighborId.HasValue
            && this.lastObservedSnapId == this.expectedNeighborId,
        this.IsSuccess
    );

    public static MenuStepOutcome Classify(
        bool cleanup,
        bool timeoutReached,
        int? startSnapId,
        int? endSnapId,
        int? expectedNeighborId
    )
    {
        if (cleanup)
            return MenuStepOutcome.Cleanup;
        bool moved = startSnapId.HasValue && endSnapId.HasValue && startSnapId != endSnapId;
        if (!moved)
            return timeoutReached ? MenuStepOutcome.Timeout : MenuStepOutcome.Unmoved;
        if (!expectedNeighborId.HasValue)
            return MenuStepOutcome.Moved;
        if (endSnapId == expectedNeighborId)
            return MenuStepOutcome.Stepped;
        return MenuStepOutcome.Overshoot;
    }

    private void ClassifyLocked()
    {
        if (this.outcome == MenuStepOutcome.Cleanup)
            return;
        if (this.pressCount == 0)
        {
            this.outcome = MenuStepOutcome.Cleanup;
            return;
        }

        this.outcome = Classify(
            cleanup: this.terminalCleanup,
            this.timeoutReached,
            this.startSnapId,
            this.lastObservedSnapId,
            this.expectedNeighborId
        );
    }

    private void FinalizeAfterRelease(StickHoldRuntimeContext? context)
    {
        this.awaitingEndSnap = false;
        bool lost = this.lastObservedSnapId is null;
        if (context is { } ctx)
        {
            lost = lost
                || !ctx.Enabled
                || !ctx.World.IsWorldReady
                || !ctx.World.IsMultiplayer
                || ctx.World.IsMainPlayer
                || ctx.World.IsSplitScreen
                || !ctx.MenuOpen
                || !ctx.ExistingSnapPresent
                || ctx.MenuType != this.armedMenuType
                || ctx.PageType != this.armedPageType
                || ctx.ScreenId != this.armedScreenId;
        }

        if (lost)
            this.outcome = MenuStepOutcome.Cleanup;
        else
            this.ClassifyLocked();

        this.phase = StickHoldPhase.Idle;
        this.remainingDelayTicks = 0;
        this.remainingTimeoutTicks = 0;
        this.direction = null;
    }

    private void AbortBeforeFirstPress()
    {
        this.outcome = MenuStepOutcome.Cleanup;
        this.awaitingEndSnap = false;
        this.ResetBookkeeping(keepOwedRelease: false);
    }

    private StickHoldTickingResult AbortBeforeFirstPressResult()
    {
        this.AbortBeforeFirstPress();
        return new StickHoldTickingResult(StickHoldTickingKind.Rejected, 0, null);
    }

    private StickHoldTickingResult AbortForContext()
    {
        this.terminalCleanup = true;
        this.outcome = MenuStepOutcome.Cleanup;
        bool keep = this.releaseOwed || this.pressCount > 0;
        this.ResetBookkeeping(keepOwedRelease: keep);
        if (keep)
        {
            this.phase = StickHoldPhase.Releasing;
            return new StickHoldTickingResult(StickHoldTickingKind.CleanupRelease, 0, this.direction);
        }

        return new StickHoldTickingResult(StickHoldTickingKind.Rejected, 0, null);
    }

    private void ResetBookkeeping(bool keepOwedRelease)
    {
        string? keepDir = keepOwedRelease ? this.direction : null;
        this.phase = StickHoldPhase.Idle;
        this.remainingDelayTicks = 0;
        this.remainingTimeoutTicks = 0;
        if (!keepOwedRelease)
            this.direction = null;
        else
            this.direction = keepDir;
        this.releaseOwed = keepOwedRelease;
        this.haveLastTickingTick = false;
        this.awaitingEndSnap = false;
    }
}

public enum MenuStepOutcome
{
    InProgress,
    Stepped,
    Overshoot,
    Moved,
    Unmoved,
    Timeout,
    Cleanup,
}

public readonly record struct MenuStepVerdict(
    int RunId,
    int? StartSnapId,
    int? EndSnapId,
    int? ExpectedNeighborId,
    int? FirstChangedSnapId,
    int PressCount,
    bool ChangeSeen,
    bool TimeoutReached,
    MenuStepOutcome Outcome,
    bool Moved,
    bool NeighborMatched,
    bool Success
);

public sealed class ControllerMenuStepDriver
{
    private readonly IStickHoldInput input;
    private bool suppressOwed;
    private string? owedButton;
    private uint lastTickingTick;
    private bool haveLastTickingTick;
    private string cleanupReason = "";

    public ControllerMenuStepDriver(ControllerMenuStepProbe probe, IStickHoldInput input)
    {
        this.Probe = probe ?? throw new ArgumentNullException(nameof(probe));
        this.input = input ?? throw new ArgumentNullException(nameof(input));
    }

    public ControllerMenuStepProbe Probe { get; }

    public void RequestCleanup(string reason)
    {
        this.cleanupReason = reason;
        this.Probe.RequestCleanup(reason);
    }

    public StickHoldDriverReport TickTicking(
        uint updateTick,
        StickHoldRuntimeContext context,
        string mappedButton
    )
    {
        if (this.haveLastTickingTick && this.lastTickingTick == updateTick)
        {
            return new StickHoldDriverReport(
                StickHoldTickingKind.Idle, StickHoldInputOp.None, "", false, false, this.Probe.RunId);
        }

        this.lastTickingTick = updateTick;
        this.haveLastTickingTick = true;

        try
        {
            if (this.cleanupReason.Length > 0 || (!context.Enabled && this.Probe.IsActive))
            {
                if (!context.Enabled)
                    this.Probe.RequestCleanup("disable");
                return this.PayOwedOrIdle(StickHoldTickingKind.CleanupRelease);
            }

            StickHoldTickingResult intent = this.Probe.ConsumeTicking(updateTick, context);
            if (intent.Kind is StickHoldTickingKind.CleanupRelease or StickHoldTickingKind.Release)
            {
                if (this.owedButton is null && !string.IsNullOrEmpty(mappedButton))
                    this.owedButton = mappedButton;
                return this.IssueSuppress(intent.Kind);
            }

            if (intent.Kind == StickHoldTickingKind.Press)
            {
                if (string.IsNullOrEmpty(mappedButton))
                {
                    this.Probe.RequestCleanup("unmapped");
                    return this.PayOwedOrIdle(StickHoldTickingKind.Rejected);
                }

                this.owedButton = mappedButton;
                this.suppressOwed = true;
                this.input.Press(mappedButton);
                this.Probe.AcknowledgePress();
                return new StickHoldDriverReport(
                    StickHoldTickingKind.Press,
                    StickHoldInputOp.Press,
                    ControllerStickHoldLabels.Holding,
                    ReleaseIntent: false,
                    SuppressIssued: false,
                    this.Probe.RunId
                );
            }

            if (intent.Kind == StickHoldTickingKind.Waiting)
            {
                return new StickHoldDriverReport(
                    StickHoldTickingKind.Waiting,
                    StickHoldInputOp.None,
                    ControllerStickHoldLabels.Waiting,
                    false,
                    false,
                    this.Probe.RunId
                );
            }

            return new StickHoldDriverReport(
                intent.Kind, StickHoldInputOp.None, "", false, false, this.Probe.RunId);
        }
        catch
        {
            try
            {
                this.IssueSuppress(StickHoldTickingKind.CleanupRelease);
            }
            catch
            {
            }

            this.Probe.RequestCleanup("exception");
            throw;
        }
    }

    public StickHoldObserveResult TickTicked(uint updateTick) =>
        this.Probe.ConsumeTicked(updateTick);

    public StickHoldDriverReport ForceCleanup(string reason, string mappedButton)
    {
        if (this.owedButton is null && !string.IsNullOrEmpty(mappedButton))
            this.owedButton = mappedButton;
        this.RequestCleanup(reason);
        this.haveLastTickingTick = false;
        return this.PayOwedOrIdle(StickHoldTickingKind.CleanupRelease);
    }

    private StickHoldDriverReport PayOwedOrIdle(StickHoldTickingKind kind)
    {
        if (this.suppressOwed && this.owedButton is not null)
            return this.IssueSuppress(kind);
        this.cleanupReason = "";
        this.haveLastTickingTick = false;
        return new StickHoldDriverReport(
            StickHoldTickingKind.Idle, StickHoldInputOp.None, "", false, false, this.Probe.RunId);
    }

    private StickHoldDriverReport IssueSuppress(StickHoldTickingKind kind)
    {
        string button = this.owedButton ?? "";
        if (this.suppressOwed && button.Length > 0)
        {
            this.input.Suppress(button);
            this.suppressOwed = false;
            this.owedButton = null;
            if (this.Probe.ReleaseOwed)
                this.Probe.AcknowledgeRelease();
            else
                this.Probe.RequestCleanup("suppress-without-model-debt");
            this.cleanupReason = "";
            return new StickHoldDriverReport(
                kind,
                StickHoldInputOp.Suppress,
                ControllerStickHoldLabels.SuppressIssued,
                ReleaseIntent: true,
                SuppressIssued: true,
                this.Probe.RunId
            );
        }

        if (this.Probe.ReleaseOwed)
            this.Probe.AcknowledgeRelease();
        this.cleanupReason = "";
        return new StickHoldDriverReport(
            kind,
            StickHoldInputOp.None,
            this.Probe.ReleaseIssuedCount > 0
                ? ControllerStickHoldLabels.AfterSuppress
                : ControllerStickHoldLabels.ReleaseIntent,
            ReleaseIntent: true,
            SuppressIssued: false,
            this.Probe.RunId
        );
    }
}
