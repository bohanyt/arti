namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Satu pemilik penulisan input selama UpdateTicking. Fake-able: tes
/// Protocol memakai <see cref="IStickHoldInput"/> tanpa SMAPI.
///
/// Utang Suppress milik DRIVER, bukan cuma buku model. Lunas hanya setelah
/// <see cref="IStickHoldInput.Suppress"/> kembali tanpa lempar.
/// TickTicked tidak pernah Press/Suppress.
/// </summary>
public interface IStickHoldInput
{
    void Press(string button);
    void Suppress(string button);
}

public sealed class ControllerStickHoldDriver
{
    private readonly IStickHoldInput input;
    private bool suppressOwed;
    private string? owedButton;
    private uint lastTickingTick;
    private bool haveLastTickingTick;
    private string cleanupReason = "";

    public ControllerStickHoldDriver(ControllerStickHoldProbe probe, IStickHoldInput input)
    {
        this.Probe = probe ?? throw new ArgumentNullException(nameof(probe));
        this.input = input ?? throw new ArgumentNullException(nameof(input));
    }

    public ControllerStickHoldProbe Probe { get; }

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
                StickHoldTickingKind.Idle,
                StickHoldInputOp.None,
                "",
                ReleaseIntent: false,
                SuppressIssued: false,
                this.Probe.RunId
            );
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
                intent.Kind,
                StickHoldInputOp.None,
                "",
                false,
                false,
                this.Probe.RunId
            );
        }
        catch
        {
            try
            {
                this.IssueSuppress(StickHoldTickingKind.CleanupRelease);
            }
            catch
            {
                // utang tetap hidup kalau Suppress juga gagal
            }

            this.Probe.RequestCleanup("exception");
            throw;
        }
    }

    public StickHoldObserveResult TickTicked(uint updateTick) =>
        this.Probe.ConsumeTicked(updateTick);

    /// <summary>
    /// Cleanup segera (ReturnedToTitle / disable keras) tanpa menunggu
    /// UpdateTicking berikutnya — event bisa berhenti setelah title.
    /// </summary>
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
            StickHoldTickingKind.Idle,
            StickHoldInputOp.None,
            "",
            false,
            false,
            this.Probe.RunId
        );
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
