using System.Globalization;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

// Primitives v2 WS-G4: the production fishing reflex loop (behind ModConfig.ReflexFishingEnabled, default OFF).
// One reflex_fish request -> FishingReflexMachine stepped once per UpdateTicking (after the background runtime's
// own UpdateTicking, before the PadModeAdapter tick) -> one verdict written through the existing inbox.
//   * every X press is a G0 pad hold of the reflex request (BackgroundMoveRuntime.TryArmReflexPress): the cast is
//     held until castingPower reaches the target (RequestRelease -> Suppress), hook / reel-in / dismiss are 1-tick
//     taps; the runner never calls Press/Suppress itself (HelperPressPort stays the sole issuer);
//   * the BobberBar is driven by the ONE guarded X writer (IFishingGame.WriteBarX -> BobberBarPadWriter) while the
//     captured BobberBar is the deepest menu; every exit writes neutral through PadHistoryHygiene in the same tick;
//   * STOP / lease loss / focus regain / pad panic / exceptions abort: neutral write, Release, then the verdict only
//     after every reflex press is released and proved (bounded), else the verdict is uncertain;
//   * the night save (DayEnding / Saving, no UpdateTicking): AbortForSave writes neutral and releases synchronously;
//   * a catch's essential ItemGrabMenu stays open for generic UI (ADR 186); input release is still proved.
// Game access goes through IFishingGame (ModEntry.Reflex.cs; a scripted fake in HoldRuntimeTests).
// Evidence ceiling: UNIT_TESTED.

/// <summary>
/// Catch facts read from the rod at the catch pose (FishingRod.whichFish / fishQuality / fishSize) plus the item
/// category (-4 = fish; junk / trash pulled without the BobberBar, FR:640-644, carries its own category).
/// </summary>
internal sealed record FishCatchFacts(string? ItemId, int Quality, int Size, bool Treasure, int Category, string? Name = null);

/// <summary>Game reads and the two oldPadState writes the fishing runner needs (game thread only).</summary>
internal interface IFishingGame
{
    long Tick { get; }
    bool WindowActive { get; }
    bool EventUp { get; }
    bool GamepadControls { get; }
    string Location { get; }
    RodView Rod { get; }
    object? Farmer { get; }
    object? Tool { get; }
    bool CanMove { get; }
    float Stamina { get; }
    int FreeSlots { get; }
    /// <summary>Sum of the inventory stacks (bait sits on the rod, not in the inventory).</summary>
    int ItemCount { get; }
    /// <summary>FishMenuKind of the deepest active menu, and the menu object itself.</summary>
    string MenuKind(out object? deepest);
    BobberBarView? ReadBar(object bar);
    FishCatchFacts? CatchFacts();
    /// <summary>PLAN §1.5 c (iii): remove a pressed oldMouseState.LeftButton / use-tool key from oldKBState (removal-only); returns what was removed.</summary>
    string? NeutralizeBarEntry();
    /// <summary>One guarded X write (the writer re-checks the captured BobberBar is deepest, inactive, pad controls on).</summary>
    bool WriteBarX(object bar);
    /// <summary>Neutral oldPadState through the G0 hygiene writer.</summary>
    void WriteNeutral();
    /// <summary>FIXB15 D-B13-6: reel the line in without a catch (vanilla FishingRod.doneFishing); returns a short note.</summary>
    string RetrieveLine();
    /// <summary>Bounded no-catch native exit, including a stalled cast. May close only the supplied owned receiver.</summary>
    string EndCast(object? ownedMenu);
    void Log(string line, bool warn = false);
}

/// <summary>The verdict ready for the inbox (CompleteReflexFish).</summary>
internal sealed record FishReflexOutcome(string RequestId, string Verdict, bool CleanupConfirmed, int PressCount,
    Dictionary<string, object> Reflex);

internal sealed class FishingReflexRunner
{
    public const int FinishBudgetTicks = 300;       // release proof + settle after the verdict (5 s)

    private readonly IFishingGame game;
    private readonly Func<BackgroundMoveRuntime?> runtime;
    private readonly Func<bool> leaseHeld;
    private readonly Func<bool> stopLatched;
    private MenuStepCommandRequest? request;
    private FishingReflexMachine? machine;
    private FishReflexArgs args;
    private object? capturedBar;
    private object? capturedCatchMenu;   // the essential ItemGrabMenu the catch opened (closed only by instance)
    private bool pressPending;
    private bool pressArmedOnce;
    private BgReflexPressKind? deferredKind;   // armed as soon as the runtime is no longer Busy (counts as pending)
    private int deferredTicks;
    private (BgReflexPressKind Kind, int Ticks)? armDue;   // decided before the runtime's ticking, armed after it
    private string? padFault;
    private int pressesArmed, pressesRefused, hFramesDelivered;
    private bool releasesProved = true;
    private bool barDirty;
    private int writeRefusals;
    private string? lastRefusal;
    private int panicsAtStart;
    private bool panicSeen;
    private string? verdict;
    private string detail = "";
    private long verdictTick;
    private long startTick;
    private FishPhase lastPhase = FishPhase.Idle;
    private long barStartTick = -1;
    private FishCatchFacts? catchFacts;
    private string? entryNeutralized;
    private FishReflexOutcome? outcome;
    private object? farmer, tool;
    private string startLocation = "";
    private bool foreignCleanup, retrieveIssued, exitIssued;
    private long cleanupWriteTick = -1;
    private ControllerExecution controller = null!;
    private string SlotPhase => this.game.MenuKind(out _) switch {
        FishMenuKind.BobberBar => "bar", FishMenuKind.ItemGrab => "catch_menu",
        _ => this.Phase switch { FishPhase.WaitBite => "wait", FishPhase.Hook => "hook", FishPhase.ReelIn => "reel",
            FishPhase.Bar or FishPhase.Landing => "landing", FishPhase.CatchMenu or FishPhase.Done => "done", _ => "cast" } };
    private Func<bool>? festivalScope;
    private Func<int>? festivalScore;
    private int startFestivalScore;

    public FishingReflexRunner(IFishingGame game, Func<BackgroundMoveRuntime?> runtime, Func<bool> leaseHeld, Func<bool> stopLatched)
    {
        this.game = game;
        this.runtime = runtime;
        this.leaseHeld = leaseHeld;
        this.stopLatched = stopLatched;
    }

    public bool Active => this.request is not null;

    public string? RequestId => this.request?.RequestId;

    public FishPhase Phase => this.machine?.Phase ?? FishPhase.Idle;

    public int BarWrites { get; private set; }

    public int NeutralWrites { get; private set; }

    /// <summary>Start one reflex for an admitted request (the caller ran FishReflexAdmission + ReflexPadAdmission).</summary>
    public void Start(MenuStepCommandRequest req, FishReflexArgs a, Func<bool>? festivalScope = null, Func<int>? festivalScore = null)
    {
        if (this.Active)
            throw new InvalidOperationException("fishing reflex already active");
        var slot = this.runtime()?.ExecutionSlot ?? throw new InvalidOperationException("controller_unadmitted");
        this.controller = slot.BindController(req, "fish", () => this.SlotPhase, this, this.EndController,
            this.BeforeRuntimeTicking, this.AfterRuntimeTicking, () => this.Active);
        this.request = req;
        this.args = a;
        this.festivalScope = festivalScope;
        this.festivalScore = festivalScore;
        this.startFestivalScore = festivalScore?.Invoke() ?? 0;
        this.machine = new FishingReflexMachine(a, festivalScope is null ? null : BarControllerTuning.Festival);
        this.capturedBar = null;
        this.capturedCatchMenu = null;
        this.pressPending = this.pressArmedOnce = false;
        this.deferredKind = null;
        this.armDue = null;
        this.padFault = null;
        this.pressesArmed = this.pressesRefused = this.hFramesDelivered = 0;
        this.releasesProved = true;
        this.barDirty = false;
        this.writeRefusals = 0;
        this.lastRefusal = null;
        this.panicsAtStart = this.runtime()?.PadPanics ?? 0;
        this.panicSeen = false;
        this.verdict = null;
        this.detail = "";
        this.lineSettled = false;
        this.farmer = this.game.Farmer;
        this.tool = this.game.Tool;
        this.startLocation = this.game.Location;
        this.foreignCleanup = this.retrieveIssued = this.exitIssued = false;
        this.cleanupWriteTick = -1;
        this.startTick = this.game.Tick;
        this.lastPhase = FishPhase.Idle;
        this.barStartTick = -1;
        this.catchFacts = null;
        this.entryNeutralized = null;
        this.outcome = null;
        this.BarWrites = this.NeutralWrites = 0;
        this.controller.Adopted = adoption => this.game.Log("epoch_adopted " + System.Text.Json.JsonSerializer.Serialize(adoption));
        this.game.Log($"REFLEX_FISH start request_id={req.RequestId} max_s={a.MaxSeconds} cast_power={a.CastPower.ToString("0.00", CultureInfo.InvariantCulture)} "
            + $"treasure={a.Treasure} hook_retries={a.HookRetries} tick={this.startTick}");
    }

    /// <summary>
    /// UpdateTicking, BEFORE BackgroundMoveRuntime.OnUpdateTicking: step the machine, write oldPadState, and request the
    /// release of the press in flight so the runtime's own press tick Suppresses it in this same UpdateTicking (the VERIFY
    /// frame of this update is then the expected R frame). New presses are armed in <see cref="AfterRuntimeTicking"/>.
    /// </summary>
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
            this.game.Log($"REFLEX_FISH fault {ex.GetType().Name}: {ex.Message}", warn: true);
            this.ForceNeutral("exception");
            this.ReleasePress("REFLEX_EXCEPTION");
            this.BeginFinish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonException), "exception:" + ex.GetType().Name);
        }
    }

    /// <summary>
    /// UpdateTicking, AFTER BackgroundMoveRuntime.OnUpdateTicking (the pad override ledger started this tick) and before the
    /// PadModeAdapter tick: arm the press the machine asked for, or a press deferred while the runtime was busy.
    /// </summary>
    public void AfterRuntimeTicking()
    {
        if (!this.Active || this.outcome is not null || this.verdict is not null)
        {
            this.armDue = null;
            return;
        }
        try
        {
            if (!this.controller.Check()) return;
            BackgroundMoveRuntime? rt = this.runtime();
            if (this.armDue is { } due)
            {
                this.armDue = null;
                this.Arm(due.Kind, due.Ticks);
            }
            else if (this.deferredKind is { } deferred && rt is not null && !rt.Busy)
            {
                this.deferredKind = null;
                this.pressPending = false;
                this.Arm(deferred, this.deferredTicks);
            }
        }
        catch (Exception ex)
        {
            this.game.Log($"REFLEX_FISH arm fault {ex.GetType().Name}: {ex.Message}", warn: true);
            this.padFault = "ARM_EXCEPTION:" + ex.GetType().Name;
            this.pressPending = false;
            this.deferredKind = null;
        }
    }

    /// <summary>The background terminal belongs to a reflex press of the in-flight reflex request.</summary>
    public bool OwnsTerminal(BackgroundMoveTerminal? terminal) =>
        terminal is not null && this.request is { } req
        && string.Equals(terminal.Request.RequestId, req.RequestId, StringComparison.Ordinal);

    /// <summary>UpdateTicked: one reflex press reached its terminal (delivered or not).</summary>
    public void OnPressTerminal(BackgroundMoveTerminal terminal)
    {
        this.pressPending = false;
        int h = terminal.Hold?.Facts?.PressCount ?? terminal.ConsumedH;
        bool issued = terminal.Hold?.Facts?.PressIssued == true;
        this.hFramesDelivered += Math.Max(0, h);
        bool cleanup = FishPressCleanup.Confirmed(terminal.Hold?.Outcome.CleanupConfirmed == true,
            issued, terminal.Hold?.Facts?.ReleaseProved == true, h, this.Phase,
            this.verdict == ReflexContracts.FishCaught, terminal.Label);
        this.releasesProved &= cleanup;
        this.padFault = h > 0 && cleanup ? null : terminal.Label;
        this.game.Log($"REFLEX_FISH press_terminal kind={terminal.Kind} label={terminal.Label} h={h} issued={issued} "
            + $"release_proved={terminal.Hold?.Facts?.ReleaseProved == true} phase={this.Phase}", warn: h == 0);
    }

    /// <summary>The verdict once it is ready (taken once).</summary>
    public FishReflexOutcome? TakeOutcome()
    {
        FishReflexOutcome? o = this.outcome;
        if (o is null)
            return null;
        this.outcome = null;
        this.request = null;
        this.machine = null;
        this.capturedBar = null;
        this.capturedCatchMenu = null;
        return o;
    }

    /// <summary>
    /// GameLoop.DayEnding / Saving (live D-B1b-1: SMAPI raises no UpdateTicking while saving), BEFORE the pad-mode
    /// adapter's synchronous restore: neutral oldPadState now, release every reflex press now (RequestRelease + Suppress
    /// of every outstanding pad press through the runtime), and end the reflex with Aborted(interrupted) "save:{evt}".
    /// The machine is never stepped again; the release proof and the verdict follow on the next ticks (bounded by
    /// <see cref="FinishBudgetTicks"/>), so the inbox slot is closed by the normal verdict path. Never throws.
    /// </summary>
    public void AbortForSave(string evt)
    {
        if (!this.Active || this.outcome is not null)
            return;
        try
        {
            this.WriteNeutral();
            this.game.Log("REFLEX_FISH neutral_on_exit save:" + evt, warn: true);
        }
        catch (Exception ex)
        {
            this.game.Log($"REFLEX_FISH save neutral fault {ex.GetType().Name}", warn: true);
        }
        try
        {
            this.ReleasePress("REFLEX_SAVE");
            IReadOnlyList<string>? done = this.runtime()?.SuppressOutstandingPad("REFLEX_SAVE:" + evt);
            this.game.Log($"REFLEX_FISH save_release evt={evt} suppressed=[{string.Join(",", done ?? Array.Empty<string>())}]", warn: true);
        }
        catch (Exception ex)
        {
            this.game.Log($"REFLEX_FISH save release fault {ex.GetType().Name}", warn: true);
        }
        try
        {
            // FIXB15 D-B13-6: no ticks follow a save; a line in the water is retrieved now (a cast in flight cannot be).
            if (FishLineRetrieve.Decide(this.game.Rod, this.game.MenuKind(out _)) == FishLineRetrieve.Retrieve)
                this.game.Log("REFLEX_FISH line_retrieved note=" + this.game.RetrieveLine() + " save:" + evt, warn: true);
        }
        catch (Exception ex)
        {
            this.game.Log($"REFLEX_FISH save retrieve fault {ex.GetType().Name}", warn: true);
        }
        if (this.verdict is not null)
            return;
        if (this.lastPhase == FishPhase.Bar)
            this.LogBarStop();
        this.verdict = ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted);
        this.detail = "save:" + evt;
        this.verdictTick = this.game.Tick;
        this.game.Log($"REFLEX_FISH verdict={this.verdict} detail={this.detail} tick=+{this.verdictTick - this.startTick} presses={this.pressesArmed} "
            + $"refused={this.pressesRefused} h={this.hFramesDelivered}", warn: true);
    }

    /// <summary>ReturnedToTitle / dispose: no more game ticks; release now and report uncertain.</summary>
    public void Abort(string reason)
    {
        if (!this.Active || this.outcome is not null)
            return;
        try
        {
            this.ForceNeutral("lifecycle");
        }
        catch
        {
            // The world is gone; nothing else to write.
        }
        this.ReleasePress("REFLEX_LIFECYCLE");
        this.verdict ??= ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted);
        this.detail = this.detail.Length == 0 ? "lifecycle:" + reason : this.detail;
        this.Complete(cleanup: false);
    }

    // ------------------------------------------------------------------ the tick

    private void EndController(string reason)
    {
        if (reason.StartsWith("save:", StringComparison.Ordinal)) { this.AbortForSave(reason[5..]); return; }
        this.runtime()?.NeutralizeController(reason);
        this.ForceNeutral(reason);
        // ADR 111: losing input authority never cancels the obligation to clean up our cast.
        // Foreign receivers/bindings still authorize no native mutation.
        this.foreignCleanup = reason == "foreign_epoch";
        if (this.verdict is null) this.BeginFinish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted), reason);
    }

    private void Step()
    {
        FishingReflexMachine m = this.machine!;
        BackgroundMoveRuntime? rt = this.runtime();
        if (!this.releasesProved && this.festivalScope is not null)
        {
            this.ForceNeutral("festival_cleanup_fault");
            this.ReleasePress("FESTIVAL_CLEANUP_FAULT");
            this.BeginFinish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted), "festival_cleanup_fault");
            return;
        }
        if (rt is not null && rt.PadPanics != this.panicsAtStart)
            this.panicSeen = true;
        string menuKind = this.game.MenuKind(out object? deepest);
        bool barIsOurs = false;
        if (menuKind == FishMenuKind.BobberBar && deepest is not null)
        {
            if (this.capturedBar is null && m.Phase is FishPhase.WaitBite or FishPhase.Hook or FishPhase.ReelIn)
            {
                this.capturedBar = deepest;
                this.entryNeutralized = this.game.NeutralizeBarEntry();
                if (this.entryNeutralized is not null)
                    this.game.Log("REFLEX_FISH bar_entry_neutralized " + this.entryNeutralized, warn: true);
            }
            barIsOurs = ReferenceEquals(deepest, this.capturedBar);
        }
        // The catch's ItemGrabMenu (treasure / inventory full) appears while landing: the reflex owns that instance only.
        if (menuKind == FishMenuKind.ItemGrab && deepest is not null && this.capturedCatchMenu is null && m.Phase == FishPhase.Landing)
            this.capturedCatchMenu = deepest;
        RodView rod = this.game.Rod;
        bool festivalValid = this.festivalScope?.Invoke() == true;
        int score = this.festivalScore?.Invoke() ?? this.startFestivalScore;
        if (this.festivalScore is not null && (score < this.startFestivalScore || score > this.startFestivalScore + 1))
            festivalValid = false;
        FishingSnapshot snap = new(
            this.game.Tick,
            this.game.WindowActive,
            this.stopLatched() || this.panicSeen,
            this.leaseHeld(),
            this.game.EventUp && !festivalValid,
            menuKind,
            barIsOurs,
            rod,
            barIsOurs && deepest is not null ? this.game.ReadBar(deepest) : null,
            this.game.Stamina,
            this.festivalScore is not null ? 1 : this.game.FreeSlots,
            this.game.ItemCount,
            this.game.Location,
            this.pressPending,
            this.pressPending && rt?.ReflexPressDown == true,
            this.padFault,
            festivalValid && this.festivalScore is not null && score == this.startFestivalScore + 1
                && this.catchFacts is { Size: > 0 });
        if (rod.FishCaught || m.Phase == FishPhase.Landing)
            this.catchFacts = this.game.CatchFacts() ?? this.catchFacts;

        FishingDecision d = m.Step(snap);

        // oldPadState first (same tick, before any pad frame of this tick: PLAN §1.5 c iv)
        switch (d.Bar)
        {
            case BarPadWrite.X:
                string? refused = BobberBarWriteGuard.Refuse(menuKind == FishMenuKind.BobberBar,
                    deepest is not null && ReferenceEquals(deepest, this.capturedBar), snap.WindowActive, this.game.GamepadControls,
                    snap.LeaseHeld, m.Phase == FishPhase.Bar);
                if (refused is null && this.capturedBar is not null && this.game.WriteBarX(this.capturedBar))
                {
                    this.barDirty = true;
                    this.BarWrites++;
                }
                else
                {
                    this.writeRefusals++;
                    if (refused != this.lastRefusal)
                        this.game.Log("REFLEX_FISH bar_write_refused " + (refused ?? "writer_guard"), warn: true);
                    this.lastRefusal = refused;
                    this.WriteNeutral();
                }
                break;
            case BarPadWrite.Neutral:
                this.WriteNeutral();
                break;
        }

        switch (d.Pad)
        {
            case PadXIntent.Hold:
                if (!this.pressPending && !this.pressArmedOnce)
                    this.armDue = (BgReflexPressKind.Cast, FishingReflexMachine.CastHoldBudget);
                this.pressArmedOnce = true;
                break;
            case PadXIntent.Tap:
                this.armDue = (BgReflexPressKind.Tap, 1);
                break;
            case PadXIntent.Release:
                this.ReleasePress("REFLEX_RELEASE");
                break;
        }

        if (m.Phase != this.lastPhase)
        {
            if (m.Phase == FishPhase.Bar)
            {
                this.barStartTick = this.game.Tick;
                this.game.Log($"BAR_DRIVE start request_id={this.request!.Value.RequestId} tick={this.barStartTick}");
            }
            else if (this.lastPhase == FishPhase.Bar)
            {
                this.LogBarStop();
            }
            this.game.Log($"REFLEX_FISH phase={m.Phase} tick=+{this.game.Tick - this.startTick} note={d.Note}");
            this.lastPhase = m.Phase;
        }

        if (d.Verdict is { } v)
            this.BeginFinish(v, m.Detail);
    }

    private void Arm(BgReflexPressKind kind, int ticks)
    {
        BackgroundMoveRuntime? rt = this.runtime();
        if (rt is null)
        {
            this.padFault = BgReflexRuntime.ReasonPadPathOff;
            this.pressesRefused++;
            return;
        }
        if (rt.Busy)
        {
            // The previous press (or an approach step's containment) is still settling: arm on the first free tick.
            this.deferredKind = kind;
            this.deferredTicks = ticks;
            this.pressPending = true;
            this.padFault = null;
            return;
        }
        BackgroundMoveTerminal? refused = rt.TryArmReflexPress(this.request!.Value, kind, ticks);
        if (refused is not null)
        {
            this.padFault = refused.Label;
            this.pressesRefused++;
            this.game.Log($"REFLEX_FISH press_refused kind={kind} label={refused.Label} phase={this.Phase}", warn: true);
            return;
        }
        this.pressPending = true;
        this.padFault = null;
        this.pressesArmed++;
        this.game.Log($"REFLEX_FISH press_armed kind={kind} ticks={ticks} run={rt.RunId} phase={this.Phase}");
    }

    private void ReleasePress(string why)
    {
        this.armDue = null;
        if (this.deferredKind is not null)
        {
            this.deferredKind = null;   // never armed: nothing to release
            this.pressPending = false;
            return;
        }
        if (this.pressPending)
            this.runtime()?.RequestRelease(why);
    }

    private void WriteNeutral()
    {
        this.game.WriteNeutral();
        this.barDirty = false;
        this.NeutralWrites++;
    }

    /// <summary>Any abort path: never leave an X in oldPadState.</summary>
    private void ForceNeutral(string why)
    {
        if (!this.barDirty)
            return;
        this.WriteNeutral();
        this.game.Log("REFLEX_FISH neutral_on_exit " + why);
    }

    private void LogBarStop()
    {
        FishBarSummary b = this.machine!.BarSummary;
        this.game.Log($"BAR_DRIVE stop request_id={this.request!.Value.RequestId} ticks={this.game.Tick - this.barStartTick} updates={b.Updates} "
            + $"x={b.XWrites} neutral={b.NeutralWrites} exit_neutral={b.ExitNeutralWrites} distance={b.LastDistance.ToString("0.00", CultureInfo.InvariantCulture)} "
            + $"refused={this.writeRefusals}");
    }

    private void BeginFinish(string v, string why)
    {
        this.ForceNeutral("verdict");
        if (this.lastPhase == FishPhase.Bar)
            this.LogBarStop();
        this.verdict = v;
        this.detail = why;
        this.verdictTick = this.game.Tick;
        this.ReleasePress("REFLEX_DONE");
        this.game.Log($"REFLEX_FISH verdict={v} detail={why} tick=+{this.verdictTick - this.startTick} presses={this.pressesArmed} "
            + $"refused={this.pressesRefused} h={this.hFramesDelivered}");
        this.TickFinishing();
    }

    private bool lineSettled;
    private bool SameCleanupBinding => this.farmer is not null && this.tool is not null
        && ReferenceEquals(this.farmer, this.game.Farmer) && ReferenceEquals(this.tool, this.game.Tool)
        && this.startLocation == this.game.Location
        && (!this.game.EventUp || this.festivalScope?.Invoke() == true);
    private bool CatchMenuPending => this.SameCleanupBinding && this.capturedCatchMenu is not null
        && this.game.MenuKind(out object? menu) == FishMenuKind.ItemGrab
        && ReferenceEquals(menu, this.capturedCatchMenu);
    private bool FarmerUsable => !this.game.Rod.UsingTool && this.game.CanMove
        && this.game.MenuKind(out _) == FishMenuKind.None;

    /// <summary>FIXB15 D-B13-6: an aborted reflex never ends with the line in the water (bounded by the finish budget).</summary>
    private bool LineSettled()
    {
        if (this.CatchMenuPending) return true; // modal owns movement; cleanup proves released input, not a closed menu
        if (this.foreignCleanup || !this.SameCleanupBinding)
            return true; // report unconfirmed; never touch a foreign receiver/farmer/tool
        if (this.game.Tick <= this.cleanupWriteTick) return false;
        if (this.lineSettled)
            return true;
        if (!FishLineRetrieve.AppliesTo(this.verdict))
            return this.FarmerUsable;
        string menuKind = this.game.MenuKind(out _);
        string decision = FishLineRetrieve.Decide(this.game.Rod, menuKind);
        if (menuKind == FishMenuKind.None && decision == FishLineRetrieve.Retrieve && !this.retrieveIssued)
        {
            this.retrieveIssued = true;
            this.cleanupWriteTick = this.game.Tick;
            string note = this.game.RetrieveLine();
            this.game.Log($"REFLEX_FISH line_retrieved note={note} verdict={this.verdict}", warn: true);
            this.detail += (this.detail.Length > 0 ? ";" : "") + "line_retrieved";
            return false; // read back next tick
        }
        if (decision != FishLineRetrieve.Idle || !this.FarmerUsable)
            return false;
        this.lineSettled = true;
        return true;
    }

    private void TickFinishing()
    {
        this.ForceNeutral("finishing");
        BackgroundMoveRuntime? rt = this.runtime();
        this.ReleasePress("REFLEX_DONE");
        bool line;
        try
        {
            line = this.LineSettled();
        }
        catch (Exception ex)
        {
            // A throw is no native release witness. Try the bounded native exit below, never repeat the cast.
            line = false;
            this.game.Log($"REFLEX_FISH line_retrieve fault {ex.GetType().Name}: {ex.Message}", warn: true);
            this.detail += (this.detail.Length > 0 ? ";" : "") + "line_retrieve_fault";
        }
        bool settled = line && !this.pressPending && (rt is null || !rt.Busy);
        if (settled)
        {
            this.Complete(cleanup: this.releasesProved && rt?.PressOverrideOutstanding != true
                && this.SameCleanupBinding && (this.FarmerUsable || this.CatchMenuPending) && !this.foreignCleanup);
            return;
        }
        if (this.game.Tick - this.verdictTick > FinishBudgetTicks)
        {
            // One safe native exit after the ordinary animation/retrieval budget, followed by a fresh readback.
            // No input, reward, question response, or mutation replay is authorized here.
            if (!this.exitIssued && !this.pressPending && rt?.Busy != true && this.TryEndCast())
                return;
            if (this.exitIssued && this.game.Tick - this.verdictTick <= FinishBudgetTicks + 60)
                return;
            this.game.Log($"REFLEX_FISH finish_unsettled pending={this.pressPending} busy={rt?.Busy} line_settled={line}", warn: true);
            this.Complete(cleanup: false);
        }
    }

    private bool TryEndCast()
    {
        if (this.foreignCleanup || !this.SameCleanupBinding) return false;
        string menu = this.game.MenuKind(out object? deepest);
        if (menu != FishMenuKind.None && !(deepest is not null
                && (ReferenceEquals(deepest, this.capturedBar) || ReferenceEquals(deepest, this.capturedCatchMenu))))
            return false;
        this.exitIssued = true;
        this.cleanupWriteTick = this.game.Tick;
        try
        {
            if (deepest is not null && ReferenceEquals(deepest, this.capturedCatchMenu))
            {
                return false; // never discard a pending catch, including on the abort/timeout cleanup path
            }
            if (!this.SameCleanupBinding) return true;
            this.game.Log("REFLEX_FISH native_exit note=" + this.game.EndCast(deepest), warn: true);
            this.detail += ";native_exit";
        }
        catch (Exception ex) { this.game.Log("REFLEX_FISH native_exit fault " + ex.GetType().Name, warn: true); }
        return true;
    }

    private void Complete(bool cleanup)
    {
        cleanup &= this.releasesProved;
        FishingReflexMachine? m = this.machine;
        FishBarSummary? bar = m?.BarSummary;
        Dictionary<string, object> reflex = new(StringComparer.Ordinal)
        {
            ["validity"] = this.controller.Receipt,
            ["verdict"] = this.verdict ?? "",
            ["detail"] = this.detail,
            ["phase_ticks"] = m is null ? new Dictionary<string, object>()
                : m.PhaseTicks.ToDictionary(kv => kv.Key, kv => (object)kv.Value, StringComparer.Ordinal),
            ["ticks"] = this.game.Tick - this.startTick,
            ["presses"] = this.pressesArmed,
            ["presses_refused"] = this.pressesRefused,
            ["taps"] = m?.Taps ?? 0,
            ["hook_misses"] = m?.HookMisses ?? 0,
            ["cast_power"] = Math.Round(m?.CastPowerAtRelease ?? -1f, 3),
            ["bar_write_refusals"] = this.writeRefusals,
            ["releases_proved"] = this.releasesProved,
        };
        if (bar is not null && bar.Updates > 0)
        {
            reflex["bar"] = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["updates"] = bar.Updates, ["in_bar"] = bar.InBarUpdates, ["x_writes"] = bar.XWrites,
                ["neutral_writes"] = bar.NeutralWrites, ["exit_neutral_writes"] = bar.ExitNeutralWrites,
                ["start_distance"] = Math.Round(bar.StartDistance, 3), ["min_distance"] = Math.Round(bar.MinDistance, 3),
                ["max_distance"] = Math.Round(bar.MaxDistance, 3), ["last_distance"] = Math.Round(bar.LastDistance, 3),
                ["treasure_seen"] = bar.TreasureSeen, ["treasure_caught"] = bar.TreasureCaught,
                ["treasure_chase_updates"] = bar.TreasureChaseUpdates,
            };
        }
        if (this.entryNeutralized is not null)
            reflex["bar_entry_neutralized"] = this.entryNeutralized;
        if (this.catchFacts is { } c && (this.verdict == ReflexContracts.FishCaught
                || this.verdict == ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInventoryFull)))
        {
            reflex["fish"] = new Dictionary<string, object>(StringComparer.Ordinal)
            {
                ["item"] = c.ItemId ?? "", ["quality"] = c.Quality, ["size"] = c.Size, ["treasure"] = c.Treasure,
                ["category"] = c.Category,   // -4 = fish; the runtime counts only a fish as the objective's catch
            };
            if (!string.IsNullOrWhiteSpace(c.Name))
                ((Dictionary<string, object>)reflex["fish"])["name"] = c.Name;
        }
        if (m?.CatchMenu is { } menu)
            reflex["catch_menu"] = menu.Wire();
        this.outcome = new FishReflexOutcome(this.request!.Value.RequestId ?? "", this.verdict!, cleanup, this.hFramesDelivered, reflex);
        this.game.Log($"REFLEX_FISH complete request_id={this.outcome.RequestId} verdict={this.outcome.Verdict} cleanup={cleanup} "
            + $"h={this.hFramesDelivered} bar_writes={this.BarWrites} neutral_writes={this.NeutralWrites}");
    }
}
