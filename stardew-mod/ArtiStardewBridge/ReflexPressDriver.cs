using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

// Primitives v2 WS-G5: the press bookkeeping shared by the combat and charge runners (the WS-G4 fishing runner keeps its
// own copy of the same rules). One press at a time, always through the PRODUCTION background runtime:
//   * a HOLD press = BackgroundMoveRuntime.TryArmReflexPress (pad X TOOL hold of the reflex request: swing tap / charge);
//   * a STEP press = BackgroundMoveRuntime.TryArmReflexStep (pad D-pad move of the reflex request: face / approach / retreat);
// decided BEFORE the runtime's UpdateTicking, armed AFTER it (the pad override ledger started this tick), deferred while the
// runtime is still settling the previous press. The driver never calls Press / Suppress (HelperPressPort stays the sole
// issuer): a release is RequestRelease on the runtime. A terminal is accounted once: delivered frames, and the release proof
// (hold: the hold facts' ReleaseProved; step: a Completed / NotCompleted move or an early release that the move verified).
// Compiled by the HoldRuntimeTests harness. Evidence ceiling: UNIT_TESTED.
internal sealed class ReflexPressDriver
{
    private readonly Func<BackgroundMoveRuntime?> runtime;
    private readonly Action<string, bool> log;
    private readonly string tag;
    private MenuStepCommandRequest? request;
    private Due? due;
    private Due? deferred;

    private sealed record Due(BgReflexPressKind? Hold, string? StepDirection, int Ticks);

    public ReflexPressDriver(Func<BackgroundMoveRuntime?> runtime, Action<string, bool> log, string tag)
    {
        this.runtime = runtime;
        this.log = log;
        this.tag = tag;
    }

    /// <summary>A press is armed (or deferred) and its terminal has not arrived.</summary>
    public bool Pending { get; private set; }

    /// <summary>The last press ended without delivering a frame (refusal label), or null.</summary>
    public string? Fault { get; private set; }

    public int Armed { get; private set; }
    public int Refused { get; private set; }
    public int HFrames { get; private set; }
    public int Holds { get; private set; }
    public int Steps { get; private set; }
    public bool ReleasesProved { get; private set; } = true;

    /// <summary>The hold press in flight is issued and not Suppressed yet (charge: frames are being delivered).</summary>
    public bool Down => this.Pending && this.runtime()?.ReflexPressDown == true;

    public void Reset(MenuStepCommandRequest req)
    {
        this.request = req;
        this.due = null;
        this.deferred = null;
        this.Pending = false;
        this.Fault = null;
        this.Armed = this.Refused = this.HFrames = this.Holds = this.Steps = 0;
        this.ReleasesProved = true;
    }

    public void Clear() => this.request = null;

    /// <summary>Decide a pad X hold press (armed in <see cref="ArmDue"/>).</summary>
    public void RequestHold(BgReflexPressKind kind, int ticks)
    {
        if (this.Pending || this.due is not null)
            return;
        this.due = new Due(kind, null, ticks);
        this.Pending = true;
        this.Fault = null;
    }

    /// <summary>Decide a pad D-pad step (0 up, 1 right, 2 down, 3 left).</summary>
    public void RequestStep(int facing, int ticks)
    {
        if (this.Pending || this.due is not null)
            return;
        string dir = facing switch { 0 => "up", 1 => "right", 2 => "down", _ => "left" };
        this.due = new Due(null, dir, Math.Max(1, ticks));
        this.Pending = true;
        this.Fault = null;
    }

    /// <summary>UpdateTicking, after the runtime's own ticking: arm the decided (or deferred) press.</summary>
    public void ArmDue()
    {
        BackgroundMoveRuntime? rt = this.runtime();
        Due? next = this.due ?? (this.deferred is not null && rt is not null && !rt.Busy ? this.deferred : null);
        if (next is null || this.request is not { } req)
            return;
        if (ReferenceEquals(next, this.due))
            this.due = null;
        else
            this.deferred = null;
        if (rt is null)
        {
            this.Pending = false;
            this.Fault = BgReflexRuntime.ReasonPadPathOff;
            this.Refused++;
            return;
        }
        if (rt.Busy)
        {
            // The previous press (or its containment) is still settling: arm on the first free tick.
            this.deferred = next;
            return;
        }
        BackgroundMoveTerminal? refused = next.Hold is { } kind
            ? rt.TryArmReflexPress(req, kind, next.Ticks)
            : rt.TryArmReflexStep(req, next.StepDirection!, next.Ticks);
        if (refused is not null)
        {
            this.Pending = false;
            this.Fault = refused.Label;
            this.Refused++;
            this.log($"{this.tag} press_refused {(next.Hold?.ToString() ?? "step:" + next.StepDirection)} label={refused.Label}", true);
            return;
        }
        this.Armed++;
        if (next.Hold is not null)
            this.Holds++;
        else
            this.Steps++;
        this.log($"{this.tag} press_armed {(next.Hold?.ToString() ?? "step:" + next.StepDirection)} ticks={next.Ticks} run={rt.RunId}", false);
    }

    /// <summary>UpdateTicked: the press reached its terminal (delivered or not).</summary>
    public void OnTerminal(BackgroundMoveTerminal terminal)
    {
        this.Pending = false;
        bool hold = terminal.Hold is not null;
        int h = terminal.Hold?.Facts?.PressCount ?? terminal.ConsumedH;
        bool issued = hold ? terminal.Hold!.Facts?.PressIssued == true
            : h > 0 || (terminal.Kind == BackgroundTerminalKind.Uncertain
                && !terminal.Label.StartsWith("RELEASED_BEFORE_LATCH", StringComparison.Ordinal)
                && !terminal.Label.StartsWith("RELEASED_BEFORE_H:", StringComparison.Ordinal));
        bool proved = hold ? terminal.Hold!.Facts?.ReleaseProved == true
            : terminal.Kind is BackgroundTerminalKind.Completed or BackgroundTerminalKind.NotCompleted
              || terminal.Label.StartsWith("NOT_COMPLETED:RELEASED_EARLY", StringComparison.Ordinal);
        this.HFrames += Math.Max(0, h);
        if (issued && !proved)
            this.ReleasesProved = false;
        this.Fault = h > 0 ? null : terminal.Label;
        this.log($"{this.tag} press_terminal kind={terminal.Kind} label={terminal.Label} h={h} issued={issued} release_proved={proved}", h == 0);
    }

    /// <summary>Release the press in flight (never armed: nothing to release).</summary>
    public void Release(string why)
    {
        if (this.due is not null || this.deferred is not null)
        {
            // Decided but never armed (or deferred behind a busy runtime): nothing was pressed.
            this.due = null;
            this.deferred = null;
            this.Pending = false;
            return;
        }
        if (this.Pending)
            this.runtime()?.RequestRelease(why);
    }

    public void ReleaseForSave(string evt)
    {
        try {
            this.Release("REFLEX_SAVE");
            var done = this.runtime()?.SuppressOutstandingPad("REFLEX_SAVE:" + evt);
            this.log($"{this.tag} save_release evt={evt} suppressed=[{string.Join(",", done ?? Array.Empty<string>())}]", true);
        } catch (Exception ex) { this.log($"{this.tag} save release fault {ex.GetType().Name}", true); }
    }

    /// <summary>No press pending and the runtime idle.</summary>
    public bool Settled
    {
        get
        {
            BackgroundMoveRuntime? rt = this.runtime();
            return !this.Pending && (rt is null || !rt.Busy);
        }
    }

    /// <summary>The verdict may be written with cleanup confirmed: every press released and proved, no override outstanding.</summary>
    public bool CleanupConfirmed => this.ReleasesProved && this.runtime()?.PressOverrideOutstanding != true;
}
