using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

// Primitives v2 WS-G5: the production tool-charge reflex (behind ModConfig.ReflexChargeEnabled, default OFF).
// One reflex_charge request -> ChargeReflexMachine stepped once per UpdateTicking -> ONE pad X hold of the reflex request
// (BgReflexPressKind.Charge, at most max_ticks frames) released by RequestRelease on the first tick toolPower >= target
// -> the tool fires at that power -> the area of the TARGET level (pinned at the start: faced tool tile + facing) is read
// back after the tool animation -> one verdict ChargeReached(n) / ChargeAborted(reason) with tiles_changed, written only
// after the press is released and proved (bounded; else uncertain). Game access through IChargeGame. Evidence: UNIT_TESTED.

/// <summary>Game reads the charge runner needs (game thread only; no writes).</summary>
internal interface IChargeGame
{
    long Tick { get; }
    bool WindowActive { get; }
    bool EventUp { get; }
    bool MenuOpen { get; }
    /// <summary>ChargeToolKind of the current tool.</summary>
    string ToolKind { get; }
    int UpgradeLevel { get; }
    bool Reaching { get; }
    int ToolPower { get; }
    bool UsingTool { get; }
    float Stamina { get; }
    /// <summary>The faced tool tile (GetToolLocation / 64) and the facing, or null.</summary>
    (int X, int Y, int Facing)? ToolTile { get; }
    IReadOnlyList<ChargeTileFacts> SampleArea(IReadOnlyList<(int X, int Y)> tiles);
    void Log(string line, bool warn = false);
}

internal sealed class ChargeReflexRunner
{
    public const int FinishBudgetTicks = 300;
    const string Tag = "REFLEX_CHARGE";

    private readonly IChargeGame game;
    private readonly Func<BackgroundMoveRuntime?> runtime;
    private readonly Func<bool> leaseHeld;
    private ControllerExecution controller = null!;
    private readonly Func<bool> stopLatched;
    private readonly ReflexPressDriver driver;
    private MenuStepCommandRequest? request;
    private ChargeReflexMachine? machine;
    private ChargeReflexArgs args;
    private string toolKind = ChargeToolKind.Other;
    private IReadOnlyList<(int X, int Y)> areaTiles = Array.Empty<(int, int)>();
    private IReadOnlyList<ChargeTileFacts> areaBefore = Array.Empty<ChargeTileFacts>();
    private int panicsAtStart;
    private bool panicSeen;
    private string? verdict;
    private string detail = "";
    private long verdictTick;
    private long startTick;
    private ReflexG5Outcome? outcome;

    public ChargeReflexRunner(IChargeGame game, Func<BackgroundMoveRuntime?> runtime, Func<bool> leaseHeld, Func<bool> stopLatched)
    {
        this.game = game;
        this.runtime = runtime;
        this.leaseHeld = leaseHeld;
        this.stopLatched = stopLatched;
        this.driver = new ReflexPressDriver(runtime, (line, warn) => game.Log(line, warn), Tag);
    }

    public bool Active => this.request is not null;

    public ChargePhase Phase => this.machine?.Phase ?? ChargePhase.Idle;

    public ReflexPressDriver Driver => this.driver;

    public void Start(MenuStepCommandRequest req, ChargeReflexArgs a)
    {
        if (this.Active)
            throw new InvalidOperationException("charge reflex already active");
        var slot = this.runtime()?.ExecutionSlot ?? throw new InvalidOperationException("controller_unadmitted");
        this.controller = slot.BindController(req, "charge", () => "power", this, this.EndController,
            this.BeforeRuntimeTicking, this.AfterRuntimeTicking, () => this.Active && this.outcome is null);
        this.request = req;
        this.args = a;
        this.machine = new ChargeReflexMachine(a);
        this.driver.Reset(req);
        this.panicsAtStart = this.runtime()?.PadPanics ?? 0;
        this.panicSeen = false;
        this.verdict = null;
        this.detail = "";
        this.outcome = null;
        this.startTick = this.game.Tick;
        this.toolKind = this.game.ToolKind;
        this.areaTiles = this.game.ToolTile is { } t ? ChargeArea.Tiles(t.X, t.Y, t.Facing, a.Level) : Array.Empty<(int, int)>();
        this.areaBefore = this.areaTiles.Count > 0 ? this.game.SampleArea(this.areaTiles) : Array.Empty<ChargeTileFacts>();
        this.game.Log($"{Tag} start request_id={req.RequestId} target_level={a.Level} max_ticks={a.HoldBudgetTicks} tool={this.toolKind} "
            + $"upgrade={this.game.UpgradeLevel} reaching={this.game.Reaching} area={this.areaTiles.Count} tick={this.startTick}");
    }

    public void BeforeRuntimeTicking()
    {
        if (!this.Active || this.outcome is not null)
            return;
        if (this.verdict is not null)
        {
            this.TickFinishing();
            return;
        }
        try
        {
            if (!this.controller.Check()) return;
            this.Step();
        }
        catch (Exception ex)
        {
            this.game.Log($"{Tag} fault {ex.GetType().Name}: {ex.Message}", warn: true);
            this.driver.Release("REFLEX_EXCEPTION");
            this.MarkVerdict(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonException), "exception:" + ex.GetType().Name);
            this.TickFinishing();
        }
    }

    public void AfterRuntimeTicking()
    {
        if (!this.Active || this.outcome is not null || this.verdict is not null)
            return;
        try
        {
            if (!this.controller.Check()) return;
            this.driver.ArmDue();
        }
        catch (Exception ex)
        {
            this.game.Log($"{Tag} arm fault {ex.GetType().Name}: {ex.Message}", warn: true);
            this.driver.Release("ARM_EXCEPTION");
        }
    }

    public bool OwnsTerminal(BackgroundMoveTerminal? terminal) =>
        terminal is not null && this.request is { } req && string.Equals(terminal.Request.RequestId, req.RequestId, StringComparison.Ordinal);

    public void OnPressTerminal(BackgroundMoveTerminal terminal) => this.driver.OnTerminal(terminal);

    public ReflexG5Outcome? TakeOutcome()
    {
        ReflexG5Outcome? o = this.outcome;
        if (o is null)
            return null;
        this.outcome = null;
        this.request = null;
        this.machine = null;
        this.driver.Clear();
        return o;
    }

    public void AbortForSave(string evt)
    {
        if (!this.Active || this.outcome is not null)
            return;
        this.driver.ReleaseForSave(evt);
        if (this.verdict is null)
            this.MarkVerdict(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonInterrupted), "save:" + evt);
    }

    public void Abort(string reason)
    {
        if (!this.Active || this.outcome is not null)
            return;
        this.driver.Release("REFLEX_LIFECYCLE");
        if (this.verdict is null)
            this.MarkVerdict(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonInterrupted), "lifecycle:" + reason);
        this.Complete(cleanup: false);
    }

    private void Step()
    {
        ChargeReflexMachine m = this.machine!;
        BackgroundMoveRuntime? rt = this.runtime();
        if (rt is not null && rt.PadPanics != this.panicsAtStart)
            this.panicSeen = true;
        ChargeSnapshot snap = new(
            this.game.Tick,
            this.game.WindowActive,
            this.stopLatched() || this.panicSeen,
            this.leaseHeld(),
            this.game.EventUp,
            this.game.MenuOpen,
            this.game.ToolKind,
            this.game.UpgradeLevel,
            this.game.Reaching,
            this.game.ToolPower,
            this.game.UsingTool,
            this.game.Stamina,
            this.driver.Pending,
            this.driver.Down,
            this.driver.Fault);
        ChargePhase before = m.Phase;
        ChargeDecision d = m.Step(snap);
        switch (d.Pad)
        {
            case PadXIntent.Hold:
                if (before == ChargePhase.Idle)
                    this.driver.RequestHold(BgReflexPressKind.Charge, this.args.HoldBudgetTicks);
                break;
            case PadXIntent.Release:
                this.driver.Release("REFLEX_RELEASE");
                break;
        }
        if (m.Phase != before)
            this.game.Log($"{Tag} phase={m.Phase} tick=+{this.game.Tick - this.startTick} note={d.Note} power={snap.ToolPower} held={m.HeldTicks}");
        if (d.Verdict is { } v)
        {
            this.MarkVerdict(v, d.Note);
            this.TickFinishing();
        }
    }

    private void EndController(string reason)
    {
        if (reason.StartsWith("save:", StringComparison.Ordinal)) { this.AbortForSave(reason[5..]); return; }
        this.driver.Release(reason); this.runtime()?.NeutralizeController(reason);
        if (this.verdict is null) this.MarkVerdict(ReflexContracts.Verdict(ReflexContracts.ChargeAborted, ReflexContracts.ReasonInterrupted), reason);
    }

    private void MarkVerdict(string v, string why)
    {
        this.verdict = v;
        this.detail = why;
        this.verdictTick = this.game.Tick;
        ChargeReflexMachine? m = this.machine;
        this.game.Log($"{Tag} verdict={v} detail={why} tick=+{this.verdictTick - this.startTick} held={m?.HeldTicks ?? 0} "
            + $"power_at_release={m?.PowerAtRelease ?? -1} presses={this.driver.Armed} refused={this.driver.Refused} h={this.driver.HFrames}",
            warn: !v.StartsWith(ReflexContracts.ChargeReached, StringComparison.Ordinal));
    }

    private void TickFinishing()
    {
        this.driver.Release("REFLEX_DONE");
        // The release fired the tool: read the area back only after its animation (UsingTool clears after DoFunction).
        bool toolIdle = !this.game.UsingTool || this.machine?.PressDelivered != true;
        if (this.driver.Settled && toolIdle)
        {
            this.Complete(cleanup: this.driver.CleanupConfirmed);
            return;
        }
        if (this.game.Tick - this.verdictTick > FinishBudgetTicks)
        {
            this.game.Log($"{Tag} finish_unsettled pending={this.driver.Pending} busy={this.runtime()?.Busy} using_tool={this.game.UsingTool}", warn: true);
            this.Complete(cleanup: false);
        }
    }

    private void Complete(bool cleanup)
    {
        ChargeReflexMachine? m = this.machine;
        int changed = 0;
        try
        {
            if (this.areaTiles.Count > 0)
                changed = ChargeAreaGuard.Changed(this.toolKind, this.areaBefore, this.game.SampleArea(this.areaTiles));
        }
        catch (Exception ex)
        {
            this.game.Log($"{Tag} area readback fault {ex.GetType().Name}", warn: true);
        }
        int power = m?.PowerAtRelease ?? -1;
        // A delivered press whose tool is still in use after the bounded settle (no X release edge reached UpdateControlInput,
        // e.g. pad panic + neutral oldPadState under STOP): the world is not settled -> never a confirmed cleanup.
        bool toolSettled = m?.PressDelivered != true || !this.game.UsingTool;
        if (!toolSettled)
        {
            cleanup = false;
            this.game.Log($"{Tag} tool_not_settled using_tool=true power={this.game.ToolPower} (no X release edge reached UpdateControlInput within the settle bound)", warn: true);
        }
        Dictionary<string, object> reflex = new(StringComparer.Ordinal)
        {
            ["validity"] = this.controller.Receipt,
            ["verdict"] = this.verdict ?? "",
            ["detail"] = this.detail,
            ["ticks"] = this.game.Tick - this.startTick,
            ["tool"] = this.toolKind,
            ["target_level"] = this.args.Level,
            ["held_ticks"] = m?.HeldTicks ?? 0,
            ["power_at_release"] = power,
            ["max_power"] = m?.MaxPowerSeen ?? 0,
            ["area_tiles"] = this.areaTiles.Count,
            ["tiles_expected"] = power >= 0 ? ChargeReflexMachine.TilesForPower(power) : 0,
            ["tiles_changed"] = changed,
            ["presses"] = this.driver.Armed,
            ["presses_refused"] = this.driver.Refused,
            ["releases_proved"] = this.driver.ReleasesProved,
            ["tool_settled"] = toolSettled,
        };
        bool reached = this.verdict is { } v && v.StartsWith(ReflexContracts.ChargeReached, StringComparison.Ordinal);
        this.outcome = new ReflexG5Outcome(this.request!.Value.RequestId ?? "", this.verdict!, cleanup, this.driver.HFrames,
            reached && changed > 0, reflex);
        this.game.Log($"{Tag} complete request_id={this.outcome.RequestId} verdict={this.outcome.Verdict} cleanup={cleanup} "
            + $"tiles_changed={changed}/{this.areaTiles.Count} h={this.driver.HFrames}");
    }
}
