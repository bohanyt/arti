namespace ArtiStardewBridge.Protocol;

// =====================================================================================================
// WS-G4 fishing reflex: pure decision layer (no game, no SMAPI). The live adapter (FishingReflexRunner,
// behind ModConfig.ReflexFishingEnabled) reads a FishingSnapshot each UpdateTicking, calls
// FishingReflexMachine.Step, and executes the decision:
//   * Pad      -> world pad X through the G0 pad hold path (BackgroundMoveRuntime.TryArmReflexPress):
//                 Hold = the cast press held until Release (RequestRelease -> Suppress), Tap = one 1-tick press
//   * Bar      -> the ONLY Game1.oldPadState X writer (BobberBarPadWriter) while the reflex's BobberBar is the
//                 deepest menu (PLAN §1.5 c): X = a GamePadState whose only button is X, Neutral = the G0
//                 hygiene writer (PadHistoryHygiene.WriteNeutral)
//   * Verdict  -> one reflex verdict (ReflexContracts) and the loop ends.
// Decomp facts behind every rule: docs/stardew/primitives-v2/PRB0_REFLEX_FACTS.md (§F); live PRB-1 (H1-H4, H6).
// =====================================================================================================

/// <summary>What the BobberBar shows this tick (fields of <c>StardewValley.Menus.BobberBar</c>, read-only).</summary>
public readonly record struct BobberBarView(
    float BobberPosition,   // fish, 0 (top) .. 532
    float BobberSpeed,      // fish px/update (+ = down)
    float BarPos,           // bar top, 0 .. 568 - BarHeight
    float BarSpeed,         // bobberBarSpeed (+ = down / falling)
    int BarHeight,          // bobberBarHeight
    float Distance,         // distanceFromCatching 0..1
    bool InBar,             // bobberInBar
    bool TreasureVisible,   // treasure && treasureAppearTimer <= 0
    bool TreasureCaught,
    float TreasurePosition,
    bool FadeIn,
    bool FadeOut);

/// <summary>Controller tuning. Defaults were tuned in an offline simulator that is not in this repository.</summary>
public sealed record BarControllerTuning(
    float FishLeadUpdates = 3f,
    float Deadband = 4f,
    float BrakeVelocity = 0.6f,
    float TreasureChaseMin = 0.6f,
    float TreasureAbandonBelow = 0.4f)
{
    public static BarControllerTuning Default { get; } = new();
    public static BarControllerTuning Festival { get; } = new(FishLeadUpdates: 5f, Deadband: 8f);
}

/// <summary>
/// BobberBar control law: bang-bang with a stopping-distance switching curve, a deadband with velocity damping
/// and hysteresis. Coordinates are the fish's: the catch window is [BarPos - 16, BarPos + BarHeight - 44]
/// (BobberBar.cs:417), so its centre is BarPos + BarHeight/2 - 30. Pressed = accelerate up.
/// </summary>
public static class BobberBarController
{
    public const float GravityOut = 0.25f;  // BobberBar.cs:428
    public const float GravityIn = 0.15f;   // x0.6 while the fish is in the bar (:433-435)

    public static float WindowCentre(in BobberBarView v) => v.BarPos + v.BarHeight / 2f - 30f;

    /// <summary>True = hold (oldPadState X) for the next update.</summary>
    public static bool Decide(in BobberBarView v, bool prevPressed, BarControllerTuning tuning, bool chaseTreasure, out bool chasingTreasure)
    {
        chasingTreasure = false;
        if (v.FadeIn || v.FadeOut)
            return false;
        float h = v.BarHeight;
        float bottom = 568 /* bar track height, px */ - h;
        float fish = Clamp(v.BobberPosition + v.BobberSpeed * tuning.FishLeadUpdates, 0f, 532f);
        float target = fish;
        // Chase hysteresis (start >= TreasureChaseMin, stop < TreasureAbandonBelow) lives in the caller's flag.
        if (chaseTreasure && v.TreasureVisible && !v.TreasureCaught)
        {
            target = v.TreasurePosition;
            chasingTreasure = true;
        }
        float vb = v.BarSpeed;
        float g = v.InBar ? GravityIn : GravityOut;
        float centre = WindowCentre(v) + vb;            // one update ahead: the write lands on the coming update
        float err = target - centre;                     // > 0: target below the window centre -> fall (release)
        float stopping = vb * Math.Abs(vb) / (2f * g);   // signed distance to stop under full opposite thrust
        float s = err - stopping;
        bool press;
        if (s > tuning.Deadband)
            press = false;
        else if (s < -tuning.Deadband)
            press = true;
        else if (vb > tuning.BrakeVelocity)
            press = true;
        else if (vb < -tuning.BrakeVelocity)
            press = false;
        else
            press = prevPressed;
        if (v.BarPos >= bottom - 0.5f && err > 0f)
            press = false;                               // resting on the bottom with the fish below: stay
        if (v.BarPos <= 0.5f && err < 0f)
            press = true;                                // pinned to the top: pressing keeps speed 0 (:429-432)
        return press;
    }

    private static float Clamp(float x, float lo, float hi) => x < lo ? lo : x > hi ? hi : x;
}

/// <summary>Fishing-rod state read from <c>StardewValley.Tools.FishingRod</c> (all public fields).</summary>
public readonly record struct RodView(
    bool Held,
    bool UsingTool,
    bool IsTimingCast,
    float CastingPower,
    bool IsCasting,
    bool BobberInAir,
    bool IsFishing,
    bool IsNibbling,
    bool Hit,
    bool IsReeling,
    bool PullingOutOfWater,
    bool FishCaught,
    bool ShowingTreasure);

/// <summary>
/// FIXB15 D-B13-6 (live V2B13ACC10b: STOP mid-cast -> Aborted(stop) with the line left in the water; the operator reeled it
/// in with the keyboard): what an aborted reflex does with the rod before its verdict. A line in the water (waiting /
/// nibbling) is retrieved (vanilla doneFishing, no catch); a cast still charging / in flight is waited for; the BobberBar /
/// landing / treasure are not this rule's (their own exits run); no rod or nothing cast = idle.
/// </summary>
public static class FishLineRetrieve
{
    public const string Idle = "idle", Wait = "wait", Retrieve = "retrieve", NotOurs = "not_ours";

    public static string Decide(RodView r, string menuKind) =>
        !r.Held ? Idle
        : menuKind == FishMenuKind.BobberBar || r.Hit || r.IsReeling || r.PullingOutOfWater || r.FishCaught || r.ShowingTreasure ? NotOurs
        : r.IsTimingCast || r.IsCasting || r.BobberInAir ? Wait
        : r.IsFishing || r.IsNibbling ? Retrieve
        : Idle;

    /// <summary>Only an abort verdict retrieves the line (a catch / a clean miss leaves the rod as the game left it).</summary>
    public static bool AppliesTo(string? verdict) => verdict is not null && verdict.StartsWith("Aborted(", StringComparison.Ordinal);
}

public static class FishMenuKind
{
    public const string None = "none";
    public const string BobberBar = "bobber_bar";
    public const string ItemGrab = "item_grab";      // inventory-full catch or the treasure chest (FishingRod.cs:2348, :2543+)
    public const string Other = "other";
}

/// <summary>
/// One tick of observation for the fishing reflex. The pad fields describe the reflex's own X press on the G0 pad
/// path (WS-G4 wiring): <paramref name="PadPending"/> = a press was armed and its hold terminal has not been
/// received yet (N-R .. settle); <paramref name="PadDown"/> = the press is issued and not yet Suppressed;
/// <paramref name="PadFault"/> = the last press ended without delivering its H frame (refused / uncertain before
/// the press), with the runtime's label.
/// </summary>
public readonly record struct FishingSnapshot(
    long Tick,
    bool WindowActive,
    bool StopRequested,
    bool LeaseHeld,
    bool EventUp,
    string MenuKind,
    bool BarIsOurs,
    RodView Rod,
    BobberBarView? Bar,
    float Stamina,
    int FreeSlots,
    int ItemCount,
    string Location,
    bool PadPending = false,
    bool PadDown = false,
    string? PadFault = null,
    bool NativeCatchObserved = false);

public enum FishPhase { Idle, Cast, Casting, WaitBite, Hook, Bar, Landing, CatchMenu, ReelIn, Done }

/// <summary>
/// World pad X through the G0 pad hold path. Hold = arm the cast press (held until Release); Tap = arm one 1-tick
/// press (press + Suppress next tick, i.e. one press and one release edge); Release = RequestRelease of the press
/// in flight (Suppress; idempotent upstream).
/// </summary>
public enum PadXIntent { None, Hold, Release, Tap }

/// <summary>The single <c>Game1.oldPadState</c> write of this tick (PLAN §1.5 c).</summary>
public enum BarPadWrite { None, X, Neutral }

/// <summary>
/// One tick's decision. CloseCatchMenu is retained for source compatibility and is always false (ADR 186).
/// </summary>
public readonly record struct FishingDecision(PadXIntent Pad, BarPadWrite Bar, string? Verdict, string Note, bool CloseCatchMenu = false);

/// <summary>
/// Result of the reflex-owned "take all, then close" on the catch's ItemGrabMenu (treasure chest FR:2543-2879 /
/// inventory-full catch FR:2341-2349). Items that do not fit are dropped at ARTI's feet (Game1.createItemDebris(item,
/// standing position, -1), what the game itself does with a catch at the end of the night, FR:2343-2346): never
/// deleted; the game picks them up again once a slot is free and ARTI is within reach. <paramref name="Refusal"/>
/// non-null = nothing was taken or closed (the deepest menu is not the instance the reflex captured, or not the rod's).
/// </summary>
public sealed record CatchMenuTake(bool Closed, string? Refusal, int Taken, int Dropped, bool FishTaken, bool FishDropped,
    IReadOnlyList<string> DroppedItems, IReadOnlyList<Dictionary<string, object>>? DroppedStacks = null)
{
    public static CatchMenuTake Refused(string why) => new(false, why, 0, 0, false, false, Array.Empty<string>());

    public Dictionary<string, object> Wire() => new(StringComparer.Ordinal)
    {
        ["closed"] = this.Closed, ["refusal"] = this.Refusal ?? "", ["taken"] = this.Taken, ["dropped"] = this.Dropped,
        ["fish_taken"] = this.FishTaken, ["fish_dropped"] = this.FishDropped,
        ["dropped_items"] = this.DroppedItems.Take(8).ToArray(),
        ["dropped_stacks"] = this.DroppedStacks?.Take(8).ToArray() ?? Array.Empty<Dictionary<string, object>>(),
    };
}

/// <summary>Bar telemetry summary for the journal (never per tick).</summary>
public sealed record FishBarSummary(int Updates, int InBarUpdates, int XWrites, int NeutralWrites, int ExitNeutralWrites,
    float StartDistance, float MinDistance, float MaxDistance, float LastDistance, bool TreasureSeen, bool TreasureCaught,
    int TreasureChaseUpdates);

/// <summary>
/// cast -> wait bite -> hook -> bar -> landing -> verdict. One instance per reflex request.
/// Budgets are in ticks at 60 updates/s (the game's fixed step).
/// </summary>
public sealed class FishingReflexMachine
{
    public const int CastArmBudget = 180;           // the cast press must be delivered (N-R .. first H) within this
    public const int CastStartBudget = 30;          // isTimingCast must appear while X is down (FishingRod.cs:1692)
    public const int CastHoldBudget = 150;          // X-down ticks; also the cast press tick cap
    public const int CastFlightBudget = 240;        // 350 ms countdown + bobber flight -> isFishing (FR :2458, :1842)
    public const int TapBudget = 120;               // a tap's hold terminal must arrive within this
    public const int HookConfirmBudget = 12;        // hit / isReeling after the tap's terminal
    public const int HookToBarBudget = 180;         // HIT overlay then BobberBar (live PRB-1: ~55 ticks; FR :648-657, :939)
    public const int LandingBudget = 600;
    public const int ReelInBudget = 180;
    public const int CatchDismissDelay = 2;         // ticks of neutral before the dismiss tap (§1.5 d)
    public const int CatchMenuSettleTicks = 2;      // read the landed menu before releasing it to generic UI
    public const int CatchMenuCloseBudget = 30;     // legacy protocol-test constant; the runner no longer closes catches

    private readonly FishReflexArgs args;
    private readonly BarControllerTuning tuning;
    private readonly Dictionary<string, long> phaseTicks = new(StringComparer.Ordinal);
    private long startTick = -1;
    private long nowTick;
    private long phaseTick;
    private bool reelTapIssued;
    private string? startLocation;
    private int startItems;
    private float prevPower = -1f;
    private bool holdingX;
    private bool castPressSeen;
    private int castDownTicks;
    private bool lastBarPress;
    private float lastDistance = -1f;
    private bool sawTiming;
    private int hookMisses;
    private bool tapRequested;     // a Tap was emitted and its terminal (PadPending false) not yet seen
    private bool tapSeenPending;
    private long tapDoneTick = -1;
    private long tapTick;
    private string? pendingVerdict;
    private bool chase;
    private int landingNeutralTicks;
    private bool dismissed;
    private long hitTick = -1;
    private bool fishInInventoryAtMenu;
    private CatchMenuTake? catchMenuTake;
    // bar telemetry
    private int barUpdates, inBarUpdates, chaseUpdates;
    private float startDistance = -1f, minDistance = 2f, maxDistance = -1f;
    private bool treasureSeen, treasureCaught;

    public FishingReflexMachine(FishReflexArgs args, BarControllerTuning? tuning = null)
    {
        string? bad = args.Validate();
        if (bad != null)
            throw new ArgumentException(bad, nameof(args));
        this.args = args;
        this.tuning = tuning ?? BarControllerTuning.Default;
    }

    public FishPhase Phase { get; private set; } = FishPhase.Idle;
    public string? Verdict { get; private set; }
    public string Detail { get; private set; } = "";
    public bool BarDriveActive { get; private set; }
    public int BarXWrites { get; private set; }
    public int BarNeutralWrites { get; private set; }
    public int BarExitNeutralWrites { get; private set; }
    public int HookMisses => this.hookMisses;
    public int Taps { get; private set; }
    public float CastPowerAtRelease { get; private set; } = -1f;

    /// <summary>The reflex-owned take-all + close of the catch menu (null = none issued yet).</summary>
    public CatchMenuTake? CatchMenu => this.catchMenuTake;

    /// <summary>The runner executed <see cref="FishingDecision.CloseCatchMenu"/>: its result (judged on the next ticks).</summary>
    public void OnCatchMenuTake(CatchMenuTake take) => this.catchMenuTake = take;

    /// <summary>Phase name -> tick offset from the reflex start at which the phase was entered (journal).</summary>
    public IReadOnlyDictionary<string, long> PhaseTicks => this.phaseTicks;

    public FishBarSummary BarSummary => new(this.barUpdates, this.inBarUpdates, this.BarXWrites, this.BarNeutralWrites,
        this.BarExitNeutralWrites, this.startDistance, this.barUpdates == 0 ? -1f : this.minDistance, this.maxDistance,
        this.lastDistance, this.treasureSeen, this.treasureCaught, this.chaseUpdates);

    private long MaxTicks => this.args.MaxSeconds * 60L;

    public FishingDecision Step(in FishingSnapshot s)
    {
        if (this.Phase == FishPhase.Done)
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "done");
        this.nowTick = s.Tick;
        if (this.startTick < 0)
            return this.Begin(s);

        // A tap is done when its hold terminal arrived (pending seen, now clear) or its arm was refused at once.
        if (this.tapRequested && s.PadPending)
            this.tapSeenPending = true;
        if (this.tapRequested && !s.PadPending && (this.tapSeenPending || s.PadFault != null))
        {
            this.tapRequested = false;
            this.tapDoneTick = s.Tick;
        }

        // ---- global aborts (every phase): highest first
        string? abort = null;
        if (s.StopRequested) abort = ReflexContracts.ReasonStop;
        else if (!s.LeaseHeld) abort = ReflexContracts.ReasonLeaseLost;
        else if (s.WindowActive) abort = ReflexContracts.ReasonFocusRegain;
        else if (s.EventUp) abort = ReflexContracts.ReasonEvent;
        else if (s.Location != this.startLocation) abort = ReflexContracts.ReasonLocationChanged;
        else if (!s.Rod.Held) abort = ReflexContracts.ReasonToolChanged;
        else if (s.MenuKind == FishMenuKind.Other && this.Phase is not (FishPhase.Landing or FishPhase.CatchMenu))
            abort = ReflexContracts.ReasonForeignMenu;
        else if (s.MenuKind == FishMenuKind.BobberBar && this.Phase == FishPhase.Bar && !s.BarIsOurs)
            abort = ReflexContracts.ReasonForeignMenu;
        else if (s.Tick - this.startTick > this.MaxTicks + LandingBudget + HookToBarBudget + CastArmBudget)
            abort = ReflexContracts.ReasonTimeout;
        if (abort != null)
            return this.Finish(ReflexContracts.AbortedVerdict(abort), "abort:" + abort);

        switch (this.Phase)
        {
            case FishPhase.Cast: return this.StepCast(s);
            case FishPhase.Casting: return this.StepCasting(s);
            case FishPhase.WaitBite: return this.StepWait(s);
            case FishPhase.Hook: return this.StepHook(s);
            case FishPhase.Bar: return this.StepBar(s);
            case FishPhase.Landing: return this.StepLanding(s);
            case FishPhase.CatchMenu: return this.StepCatchMenu(s);
            case FishPhase.ReelIn: return this.StepReelIn(s);
            default: return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "idle");
        }
    }

    private FishingDecision Begin(in FishingSnapshot s)
    {
        this.startTick = s.Tick;
        this.startLocation = s.Location;
        this.startItems = s.ItemCount;
        string? refuse = null;
        if (s.StopRequested) refuse = ReflexContracts.ReasonStop;
        else if (!s.LeaseHeld) refuse = ReflexContracts.ReasonLeaseLost;
        else if (s.WindowActive) refuse = ReflexContracts.ReasonFocusRegain;
        else if (s.EventUp) refuse = ReflexContracts.ReasonEvent;
        else if (!s.Rod.Held) refuse = ReflexContracts.ReasonToolChanged;
        else if (s.MenuKind != FishMenuKind.None) refuse = ReflexContracts.ReasonForeignMenu;
        else if (s.Stamina < ReflexContracts.FishStaminaFloor) refuse = ReflexContracts.ReasonStaminaLow;
        else if (s.Rod.UsingTool || s.PadPending) refuse = ReflexContracts.ReasonInterrupted;
        if (refuse != null)
            return this.Finish(ReflexContracts.AbortedVerdict(refuse), "refused:" + refuse);
        this.Enter(FishPhase.Cast, s.Tick);
        this.holdingX = true;
        return new FishingDecision(PadXIntent.Hold, BarPadWrite.None, null, "cast:hold");
    }

    private FishingDecision StepCast(in FishingSnapshot s)
    {
        long t = s.Tick - this.phaseTick;
        if (s.PadFault is { } fault && !this.castPressSeen)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonPadRefused), "cast_press_refused:" + fault);
        if (s.PadDown)
        {
            this.castPressSeen = true;
            this.castDownTicks++;
        }
        if (s.Rod.IsTimingCast)
            this.sawTiming = true;
        if (!this.sawTiming)
        {
            if (this.castPressSeen && (this.castDownTicks > CastStartBudget || !s.PadDown))
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonCastNoEffect), "no_timing_cast");
            if (!this.castPressSeen && (t > CastArmBudget || !s.PadPending && t > 2))
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonPadRefused), "cast_press_not_delivered");
            return new FishingDecision(PadXIntent.Hold, BarPadWrite.None, null, "cast:wait_timing");
        }
        float p = s.Rod.CastingPower;
        bool passedPeak = this.prevPower >= 0f && p < this.prevPower && this.prevPower >= 0.5f;
        this.prevPower = p;
        // castingPower sweeps 0 -> 1 in ~1000 ms then bounces back (FishingRod.cs:2024-2038); the cast starts on the
        // first tick the LIVE pad X is up (FishingRod.cs:2041) -> one Suppress releases it.
        if (p >= this.args.CastPower || passedPeak || this.castDownTicks > CastHoldBudget || !s.Rod.IsTimingCast || !s.PadDown)
        {
            this.holdingX = false;
            this.CastPowerAtRelease = p;
            this.Enter(FishPhase.Casting, s.Tick);
            return new FishingDecision(PadXIntent.Release, BarPadWrite.None, null, $"cast:release power={p:0.00}");
        }
        return new FishingDecision(PadXIntent.Hold, BarPadWrite.None, null, "cast:hold");
    }

    private FishingDecision StepCasting(in FishingSnapshot s)
    {
        long t = s.Tick - this.phaseTick;
        if (s.Rod.IsFishing)
        {
            this.Enter(FishPhase.WaitBite, s.Tick);
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "bobber_in_water");
        }
        bool idle = !s.Rod.UsingTool && !s.Rod.IsCasting && !s.Rod.BobberInAir && !s.Rod.IsTimingCast;
        if ((idle && t > 30) || t > CastFlightBudget)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonCastNoEffect), "not_fishable");
        return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "casting");
    }

    private FishingDecision StepWait(in FishingSnapshot s)
    {
        if (s.Rod.Hit || (s.MenuKind == FishMenuKind.BobberBar && s.BarIsOurs))
        {
            // Auto-hook enchantment hooks without input (FR:2130-2136): go straight to waiting for the bar.
            this.Enter(FishPhase.Hook, s.Tick);
            return this.StepHook(s);
        }
        if (s.Rod.IsNibbling)
        {
            if (s.PadPending)
                return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "hook:pad_busy");
            // The hook fires on the X RELEASE edge: Game1.UpdateControlInput -> useToolButtonReleased ->
            // EndUsingTool (canReleaseTool is true while fishing, FR:2100) -> Tool.endUsing -> DoFunction with
            // isNibbling (Tool.cs:599-604, FR:480, :645-657). One 1-tick tap = press frame + release frame.
            this.Enter(FishPhase.Hook, s.Tick);
            return this.Tap(s.Tick, "hook:tap");
        }
        if (!s.Rod.IsFishing && !s.Rod.UsingTool)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted), "rod_reset_while_waiting");
        if (s.Tick - this.startTick > this.MaxTicks)
            return this.StartReelIn(s, ReflexContracts.NoBite, "no_bite_within_max_s");
        return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "wait_bite");
    }

    private FishingDecision StepHook(in FishingSnapshot s)
    {
        if (s.MenuKind == FishMenuKind.BobberBar && s.BarIsOurs)
        {
            this.Enter(FishPhase.Bar, s.Tick);
            return this.StepBar(s);
        }
        if (s.Rod.PullingOutOfWater || s.Rod.FishCaught)
        {
            // Junk / trash / fish-pond pulls skip the BobberBar (FR:640-644).
            this.lastDistance = 1f;
            this.Enter(FishPhase.Landing, s.Tick);
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "no_bar_catch");
        }
        if (s.Rod.Hit || s.Rod.IsReeling)
        {
            if (this.hitTick < 0)
                this.hitTick = s.Tick;
            if (s.Tick - this.hitTick > HookToBarBudget)
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonTimeout), "hit_but_no_bar");
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "hook:hit_wait_bar");
        }
        if (this.tapRequested)
        {
            if (s.Tick - this.tapTick > TapBudget)
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonPadRefused), "hook_tap_no_terminal");
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "hook:tap_pending");
        }
        if (s.PadFault is { } refused)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonPadRefused), "hook_tap_refused:" + refused);
        long since = s.Tick - this.tapDoneTick;
        if (since > HookConfirmBudget || (!s.Rod.IsNibbling && since > 2))
        {
            this.hookMisses++;
            if (this.hookMisses > this.args.HookRetries)
                return this.StartReelIn(s, ReflexContracts.AbortedVerdict(ReflexContracts.ReasonHookMissed), "hook_missed");
            this.Enter(FishPhase.WaitBite, s.Tick);   // a missed nibble re-arms the bite timer (FR:2153-2162)
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "hook:missed_retry");
        }
        return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "hook:confirm");
    }

    private FishingDecision StepBar(in FishingSnapshot s)
    {
        if (s.MenuKind != FishMenuKind.BobberBar)
        {
            // BobberBar closed by the game (fadeOut end, BobberBar.cs:341-370) -> neutral in the same tick (§1.5 c iv).
            BarPadWrite exit = this.ExitBarDrive();
            if (this.lastDistance > 0.9f)
            {
                this.Enter(FishPhase.Landing, s.Tick);
                return new FishingDecision(PadXIntent.None, exit, null, "bar_closed:caught");
            }
            return this.FinishWith(exit, ReflexContracts.FishEscaped, "bar_closed:escaped");
        }
        BobberBarView? bar = s.Bar;
        if (bar == null)
            return this.FinishWith(this.ExitBarDrive(), ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInterrupted), "bar_unreadable");
        BobberBarView v = bar.Value;
        this.lastDistance = v.Distance;
        this.barUpdates++;
        if (v.InBar) this.inBarUpdates++;
        if (this.startDistance < 0f) this.startDistance = v.Distance;
        if (v.Distance < this.minDistance) this.minDistance = v.Distance;
        if (v.Distance > this.maxDistance) this.maxDistance = v.Distance;
        this.treasureSeen |= v.TreasureVisible;
        this.treasureCaught |= v.TreasureCaught;
        if (this.chase && v.Distance < this.tuning.TreasureAbandonBelow)
            this.chase = false;
        else if (!this.chase && this.args.Treasure && v.Distance >= this.tuning.TreasureChaseMin)
            this.chase = true;
        bool press = BobberBarController.Decide(v, this.lastBarPress, this.tuning, this.chase, out bool chasing);
        if (chasing) this.chaseUpdates++;
        this.lastBarPress = press;
        this.BarDriveActive = true;
        if (press)
        {
            this.BarXWrites++;
            return new FishingDecision(PadXIntent.None, BarPadWrite.X, null, "bar:x");
        }
        this.BarNeutralWrites++;
        return new FishingDecision(PadXIntent.None, BarPadWrite.Neutral, null, "bar:neutral");
    }

    private FishingDecision StepLanding(in FishingSnapshot s)
    {
        long t = s.Tick - this.phaseTick;
        if (s.MenuKind == FishMenuKind.ItemGrab)
        {
            // The catch opened an essential ItemGrabMenu: the treasure chest (also an accidental one with treasure=false;
            // the fish was added before it unless the inventory was full, FR:2362-2399, :2543-2879) or the inventory-full
            // catch (FR:2341-2349). Nothing else closes it (G1 refuses ItemGrabMenu, v1 container_exit is chest-only), so
            // the reflex takes everything and closes it itself; the verdict follows only once the menu is gone.
            this.fishInInventoryAtMenu = s.ItemCount > this.startItems;
            this.Enter(FishPhase.CatchMenu, s.Tick);
            return this.StepCatchMenu(s);
        }
        if (s.MenuKind == FishMenuKind.Other)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonForeignMenu), "landing:foreign_menu");
        if (this.dismissed && this.tapRequested)
        {
            if (s.Tick - this.tapTick > TapBudget)
                return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonPadRefused), "dismiss_tap_no_terminal");
        }
        else if (this.dismissed && s.PadFault is { } fault && s.Rod.FishCaught)
        {
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonPadRefused), "dismiss_tap_refused:" + fault);
        }
        if (s.Rod.FishCaught && !this.dismissed)
        {
            // catch pose: dismissed by a LIVE pad X/A down (didPlayerJustClickAtAll, FR:2273-2276). Wait for the
            // neutral oldPadState to be seen first (§1.5 d), then one 1-tick tap (a held X recasts after 200 ms,
            // recastTimerMs FR:1883-1895, :2316).
            this.landingNeutralTicks++;
            if (this.landingNeutralTicks >= CatchDismissDelay && !s.PadPending)
            {
                this.dismissed = true;
                return this.Tap(s.Tick, "landing:dismiss_tap");
            }
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "landing:neutral_wait");
        }
        if (!s.Rod.UsingTool && !s.Rod.FishCaught && !s.Rod.PullingOutOfWater && !s.Rod.ShowingTreasure
            && (s.ItemCount > this.startItems || s.NativeCatchObserved))
            return this.Finish(ReflexContracts.FishCaught, s.NativeCatchObserved ? "festival_score+1" : "item_added");
        if (t > LandingBudget)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonTimeout), "landing_timeout");
        return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "landing");
    }

    private FishingDecision StepCatchMenu(in FishingSnapshot s)
    {
        long t = s.Tick - this.phaseTick;
        if (s.MenuKind == FishMenuKind.None)
        {
            bool fish = this.fishInInventoryAtMenu || this.catchMenuTake is { FishTaken: true };
            string how = "catch_menu_closed_externally";
            if (fish)
                return this.Finish(ReflexContracts.FishCaught, how + ":fish_in_inventory");
            // ADR 107: a landed catch is a catch, including one dropped at her feet.
            if (this.catchMenuTake is { Closed: true, Refusal: null, FishDropped: true })
                return this.Finish(ReflexContracts.FishCaught, how + ":fish_dropped_at_feet");
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonInventoryFull),
                how + ":fish_not_in_inventory");
        }
        if (s.MenuKind != FishMenuKind.ItemGrab)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonForeignMenu), "catch_menu:foreign_menu");
        if (t < CatchMenuSettleTicks)
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "catch_menu:settle");
        // ADR 186: landing ends the reflex, not the player's inventory decision.
        // The runner releases its input and hands this exact receiver to generic UI.
        return this.Finish(ReflexContracts.FishCaught, "catch_menu_pending_choice");
    }

    private FishingDecision StartReelIn(in FishingSnapshot s, string verdict, string note)
    {
        // One X tap while fishing and not nibbling reels the line in (release edge -> DoFunction -> pullingOutOfWater, FR:595-596).
        this.pendingVerdict = verdict;
        this.Detail = note;
        this.Enter(FishPhase.ReelIn, s.Tick);
        if (s.PadPending)
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "reel_in:pad_busy");
        this.reelTapIssued = true;
        return this.Tap(s.Tick, "reel_in:tap");
    }

    private FishingDecision StepReelIn(in FishingSnapshot s)
    {
        long t = s.Tick - this.phaseTick;
        if (s.MenuKind == FishMenuKind.BobberBar && s.BarIsOurs)
        {
            // a bite landed in the same ticks as the reel-in tap: fight it anyway
            this.pendingVerdict = null;
            this.Enter(FishPhase.Bar, s.Tick);
            return this.StepBar(s);
        }
        if (!this.reelTapIssued)
        {
            if (s.PadPending)
                return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "reel_in:pad_busy");
            if (t > ReelInBudget)
                return this.Finish(this.pendingVerdict ?? ReflexContracts.NoBite, this.Detail);
            this.reelTapIssued = true;
            return this.Tap(s.Tick, "reel_in:tap");
        }
        if (this.tapRequested)
        {
            if (s.Tick - this.tapTick > TapBudget)
                return this.Finish(this.pendingVerdict ?? ReflexContracts.NoBite, this.Detail);
            return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "reel_in:tap_pending");
        }
        if (s.PadFault is { } fault)
            return this.Finish(ReflexContracts.AbortedVerdict(ReflexContracts.ReasonPadRefused), "reel_in_tap_refused:" + fault);
        if ((!s.Rod.UsingTool && !s.Rod.IsFishing && !s.Rod.PullingOutOfWater) || t > ReelInBudget)
            return this.Finish(this.pendingVerdict ?? ReflexContracts.NoBite, this.Detail);
        return new FishingDecision(PadXIntent.None, BarPadWrite.None, null, "reel_in");
    }

    private FishingDecision Tap(long tick, string note)
    {
        this.tapRequested = true;
        this.tapSeenPending = false;
        this.tapTick = tick;
        this.Taps++;
        return new FishingDecision(PadXIntent.Tap, BarPadWrite.None, null, note);
    }

    private BarPadWrite ExitBarDrive()
    {
        if (!this.BarDriveActive)
            return BarPadWrite.None;
        this.BarDriveActive = false;
        this.BarExitNeutralWrites++;
        return BarPadWrite.Neutral;
    }

    private FishingDecision Finish(string verdict, string note) => this.FinishWith(this.ExitBarDrive(), verdict, note);

    private FishingDecision FinishWith(BarPadWrite bar, string verdict, string note)
    {
        PadXIntent pad = this.holdingX || this.tapRequested ? PadXIntent.Release : PadXIntent.None;
        this.holdingX = false;
        this.tapRequested = false;
        this.Verdict = verdict;
        if (this.Detail.Length == 0)
            this.Detail = note;
        this.Enter(FishPhase.Done, this.nowTick);
        return new FishingDecision(pad, bar, verdict, note);
    }

    private void Enter(FishPhase phase, long tick)
    {
        this.Phase = phase;
        this.phaseTick = tick;
        string name = phase.ToString();
        if (!this.phaseTicks.ContainsKey(name))
            this.phaseTicks[name] = this.startTick < 0 ? 0 : tick - this.startTick;
    }
}
