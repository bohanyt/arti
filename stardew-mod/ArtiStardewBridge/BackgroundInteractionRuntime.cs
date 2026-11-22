using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

// WS3a (Primitives v1, 2026-10-07): per-kind action targets (door, forage) for the existing
// background ACTION hold, and the walk-onto warp variant of the background move run
// (move_through). Game-coupled facts arrive through closures built in ModEntry, so this
// partial compiles against the offline harness doubles. No new input path: the hold keeps
// its single pinned key / VERIFY / Suppress / release proof / latch / containment, and the
// move run keeps its substituted H/R/N frames. Evidence ceiling: UNIT_TESTED.

/// <summary>
/// The pinned background ACTION target. <see cref="StillPresent"/> re-resolves the capture
/// rule against the pinned object (same reference at the grab tile); <see cref="SampleEffect"/>
/// reads location / transition / menu / inventory facts for door and forage. Chest keeps the
/// M1 menu-by-reference rule and needs no effect sampler.
/// </summary>
internal sealed record BgActionTarget(
    BgActionKind Kind,
    StardewValley.Objects.Chest? Chest,
    string? ExpectedLocationId,
    string ItemId,
    Func<bool> StillPresent,
    Func<BgActionEffectFacts>? SampleEffect)
{
    public static BgActionTarget ForChest(StardewValley.Objects.Chest chest, Func<bool>? present = null) =>
        new(BgActionKind.Chest, chest, null, "", present ?? (() => true), null);

    /// <summary>
    /// WS3b npc / eat: true when the given menu is this target's expected receiver (the pinned
    /// NPC's DialogueBox, or the Eat question). Read-only; null for chest / door / forage.
    /// </summary>
    public Func<object?, bool>? ExpectedReceiver { get; init; }

    /// <summary>WS-G6: the extras kind of an Extra / ExtraReceiver target (null otherwise).</summary>
    public string? ExtraKind { get; init; }

    /// <summary>WS-G6: the pad hold pins X instead of A (milk / shear / machine drop-in / place, PLAN 1.4 X rows).</summary>
    public bool UsePadX { get; init; }

    /// <summary>WS-G6: UsingTool after the consumed press is the kind's own effect (milk pail / shears).</summary>
    public bool ToolUseAfterPress { get; init; }

    /// <summary>WS-G6: tile / facing may change after the consumed press (animal pet, mount, dismount, harvest).</summary>
    public bool PoseMayChangeAfterPress { get; init; }

    /// <summary>FIXB59: strict event pin before A, exact expected transition only after consumed A.</summary>
    public Func<bool, bool>? EventScopeValid { get; init; }

    public FestivalSeatMotion? SeatMotion { get; init; }
    public Func<FestivalSeatMotionFacts>? SampleSeatMotion { get; init; }
}

/// <summary>Read-only warp facts for a move_through run (sampled every UpdateTicked).</summary>
internal sealed record BgThroughProbe(bool IsWarping, string? RequestToken, string LocationToken, bool Transitioning,
    bool PlayerFree, bool MenuOpen, bool EventUp = false, bool SleepQuestion = false, double? FadeAlpha = null,
    string? RequestEpoch = null);

/// <summary>The expected transition of one move_through request.</summary>
internal sealed record BgThroughExpectation(string Kind, string? ExpectedLocationId, Func<BgThroughProbe> Probe);

internal sealed partial class BackgroundMoveRuntime
{
    private BgActionTarget? holdTarget;
    private BgActionEffectFacts? effectBefore;
    private BgActionEffectFacts? effectNow;
    private bool effectTransitionSeen;
    private TravelProgress doorProgress = new();
    private string? doorProgressNotice;
    // SMAPI Player.Warped (local) observed while the current request was in flight.
    private bool localWarpSeen;
    private string? localWarpLocation;
    private ThroughRun? through;

    private sealed class ThroughRun
    {
        public ThroughRun(BgThroughExpectation expect) => this.Expect = expect;
        public BgThroughExpectation Expect { get; }
        public bool WarpStarted;
        public string? Wrong;
        public long WarpStartMs;
        public int HAtWarpStart;
        public bool ReleaseVerified;
        public BgThroughProbe? Last;
        public string? Decision;
        // WS3b bed: the Sleep question was observed (monotonic ms of the first sighting).
        public bool TouchSeen;
        public long TouchMs;
        public string? StartLocation;
        public TravelProgress Progress { get; } = new();
        public string? ProgressNotice;
    }

    /// <summary>
    /// A move_through request is in flight and a warp has started (expected or not): only R/N
    /// remain, served under the fade with player-free / can-move relaxed (the move gates).
    /// </summary>
    private bool ThroughWarpStarted => this.through is { } run && (run.WarpStarted || run.Wrong is not null);

    private void LogTravelProgress(TravelProgress progress, ref string? previous, string location)
    {
        if (!progress.Committed) return;
        long now = BackgroundContainmentWatchdog.NowMs();
        string notice = progress.Phase + ":" + progress.Notice(now);
        if (notice == previous) return;
        previous = notice;
        this.monitor.Log($"background TRAVEL run={this.runId} phase={notice} actual_location={location} "
            + $"request_epoch={progress.CommittedEpoch} committed_ms={progress.CommitMs} idle_ms={now - progress.ChangedMs}", LogLevel.Info);
    }

    /// <summary>The WS3a pinned target kind of the current ACTION hold (Chest when none).</summary>
    public BgActionKind HoldActionKind => this.holdTarget?.Kind ?? BgActionKind.Chest;

    /// <summary>ModEntry Player.Warped (local player only). Bookkeeping only; never input.</summary>
    public void OnLocalWarp(string? newLocationToken)
    {
        if (this.request is null)
            return;
        this.localWarpSeen = true;
        this.localWarpLocation = newLocationToken;
        this.monitor.Log($"background local warp observed run={this.runId} to={newLocationToken ?? "null"}", LogLevel.Info);
    }

    /// <summary>
    /// Admit one inactive world_interact with a per-kind pinned target. Same admission and
    /// mechanism as the chest; the target record only adds the pre-press re-check and the
    /// effect sampler.
    /// </summary>
    public BackgroundMoveTerminal? TryArmHoldTarget(MenuStepCommandRequest req, BgHoldKind kind, BgActionTarget? target)
    {
        if (kind == BgHoldKind.Action && target is null)
        {
            int id = this.nextRunId++;
            return new BackgroundMoveTerminal(BackgroundTerminalKind.Rejected, req,
                BgReason.PreconditionUnavailable("NO_PINNED_TARGET"), 0, "", id);
        }
        if (kind == BgHoldKind.Action && target!.Kind != BgActionKind.Chest && target.SampleEffect is null)
        {
            int id = this.nextRunId++;
            return new BackgroundMoveTerminal(BackgroundTerminalKind.Rejected, req,
                BgReason.PreconditionUnavailable("NO_EFFECT_SAMPLER"), 0, "", id);
        }
        BgActionEffectFacts? before = null;
        if (kind == BgHoldKind.Action && target!.SampleEffect is { } sample)
        {
            try
            {
                before = sample();
            }
            catch
            {
                int id = this.nextRunId++;
                return new BackgroundMoveTerminal(BackgroundTerminalKind.Rejected, req,
                    BgReason.PreconditionUnavailable("EFFECT_BASELINE_UNREADABLE"), 0, "", id);
            }
        }
        BackgroundMoveTerminal? refused;
        try { refused = this.TryArmHoldCore(req, kind, target?.Chest, target); }
        finally { this.eventAdmissionTarget = null; }
        if (refused is null && kind == BgHoldKind.Action)
        {
            this.effectBefore = before;
            this.effectNow = before;
        }
        return refused;
    }

    /// <summary>Pre-press re-check of the pinned ACTION target (TOOL holds: always present).</summary>
    private bool HoldTargetPresent()
    {
        if (this.holdKind != BgHoldKind.Action || this.holdTarget is not { } target)
            return true;
        try
        {
            if (!target.StillPresent())
                return false;
            // ADR 246 (review B2): a generic press re-reads its named harms with the pin, right before the press and at the
            // hook. A crop planted in the same dirt, a sapling in a sweep cell or a villager stepping into the grab rows after
            // admission makes the target "gone": the existing zero-press TARGET_GONE path ends it, the harm is named.
            if (target.Kind == BgActionKind.Generic
                && GenericHarm.Refusal(GenericHarmProbe.Sample(Game1.player, target.UsePadX)) is string harm)
            {
                if (this.holdHarmGone is null)
                    this.monitor.Log($"background HOLD generic harm appeared after admission run={this.runId} reason={harm} ("
                        + GenericHarm.Words(harm) + ")", LogLevel.Info);
                this.holdHarmGone = harm;
                return false;
            }
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// UpdateTicking, before the single Press: a vanished target never receives the press
    /// (the action button would fall through to held-item use / placement / other tiles).
    /// Returns false (and latches TARGET_GONE) only while no Press was issued.
    /// </summary>
    private bool PrePressTargetCheck(BgPressSession session)
    {
        if (session.PressIssued || this.holdPlan is not { KeyShouldBeDown: true })
            return true;
        if (this.HoldTargetPresent())
            return true;
        if (this.releaseReason is null)
            this.monitor.Log($"background HOLD target gone before press run={this.runId} kind={this.HoldActionKind}", LogLevel.Warn);
        // ADR 246 review B2: a harm found by the pin re-check names itself (PRECONDITION_UNAVAILABLE(TARGET_GONE:GENERIC_HARM:...)).
        this.releaseReason ??= GenericHarm.GoneLabel(BgHoldReason.TargetGone, this.holdHarmGone) ?? BgHoldReason.TargetGone;
        return false;
    }

    /// <summary>
    /// The consumed door press can start a native warp. Only R/N may cross that fade;
    /// a fade without a pending native warp (or local Warped receipt) stays foreign.
    /// The destination is still independently judged by ActionEffectVerdict.
    /// </summary>
    private bool DoorWarpRelease()
    {
        if (this.holdTarget?.Kind != BgActionKind.Door
            || this.holdPlan is not { ConsumedH: > 0, Next: not BgFrameKind.H }
            || this.effectBefore is not { PendingLocationToken: null })
            return false;
        this.ObserveActionEffect();
        return this.doorProgress.Committed
            || this.localWarpSeen && this.localWarpLocation == this.holdTarget.ExpectedLocationId;
    }

    /// <summary>Samples door / forage effect facts during settle; chest: nothing to sample.</summary>
    private void ObserveActionEffect()
    {
        if (this.holdKind != BgHoldKind.Action || this.holdTarget is not { SampleEffect: { } sample } || this.effectBefore is null)
            return;
        BgActionEffectFacts now;
        try
        {
            BgActionEffectFacts sampled = sample();
            now = sampled with { WarpObserved = this.localWarpSeen && this.localWarpLocation == sampled.LocationToken };
        }
        catch
        {
            this.effectNow = null;
            return;
        }
        this.effectNow = now;
        if (this.holdTarget.Kind == BgActionKind.Door)
        {
            this.doorProgress.Observe(BackgroundContainmentWatchdog.NowMs(), now.PendingLocationToken,
                now.LocationToken, now.Transitioning, now.PlayerFree, now.MenuOpen, now.EventUp, now.FadeAlpha,
                this.holdPlan is { ConsumedH: > 0 } && this.effectBefore.PendingLocationToken is null,
                this.localWarpSeen ? this.localWarpLocation : null, now.RequestEpoch);
            this.LogTravelProgress(this.doorProgress, ref this.doorProgressNotice, now.LocationToken);
            if (this.doorProgress.Committed && now.PendingLocationToken is null
                && this.FreshArrivalPose(this.holdTarget.ExpectedLocationId))
                this.effectNow = now = now with { WarpObserved = true };
        }
        if (now.Transitioning || this.localWarpSeen
            || !string.Equals(now.LocationToken, this.effectBefore.LocationToken, StringComparison.Ordinal))
            this.effectTransitionSeen = true;
    }

    /// <summary>Per-kind settle term replacing the chest menu wait for door / forage.</summary>
    private bool ActionEffectSettled(long now)
    {
        BgActionTarget? target = this.holdTarget;
        if (target is null || target.Kind == BgActionKind.Chest)
            return true;
        if (target.SeatMotion is { } seat && !seat.Faulted && !seat.Settled && seat.Started)
            return false;
        if (this.effectBefore is not { } before || this.effectNow is not { } current)
            return true;
        long since = this.pressSession?.FirstSuppressMs is long first ? now - first : 0;
        if (target.Kind == BgActionKind.Door && this.doorProgress.Committed)
        {
            // Arrival and modal handoff still use fresh facts. A quiet host phase has its own bound.
            if (this.doorProgress.Stalled(now)) return true;
            since = Math.Min(since, BgActionEffect.WarpWaitMs - 1);
        }
        return BgActionEffect.Settled(target.Kind, target.ExpectedLocationId, before, current, this.effectTransitionSeen, since);
    }

    /// <summary>Settle bound: a door waits for the farmhand warp round trip, everything else keeps the existing bound.</summary>
    private long HoldSettleBoundFor() => this.holdTarget?.Kind switch
    {
        BgActionKind.Door => this.doorProgress.Committed ? TravelProgress.TotalWaitMs + HoldSettleBoundMs
            : BgActionEffect.WarpWaitMs + HoldSettleBoundMs,
        BgActionKind.Generic => BgActionEffect.WarpWaitMs + HoldSettleBoundMs,
        // WS-G6: the slow extras (mount / dismount / milk / shear) wait up to 3 s for their effect.
        BgActionKind.Extra => BgExtrasEffect.WaitMs(this.holdTarget.ExtraKind) + HoldSettleBoundMs,
        _ => HoldSettleBoundMs,
    };

    private BgActionEffectVerdict? ActionEffectVerdict()
    {
        BgActionTarget? target = this.holdTarget;
        if (target is null || target.Kind == BgActionKind.Chest)
            return null;
        BgActionEffectVerdict verdict = BgActionEffect.Classify(target.Kind, target.ExpectedLocationId, this.effectBefore, this.effectNow, this.effectTransitionSeen);
        if (target.Kind == BgActionKind.Door && this.doorProgress.Redirected)
            return new(BgHoldCompletion.InteractionUncertain, false, "TRAVEL_REDIRECTED:" + this.effectNow?.LocationToken, null, 0);
        if (target.Kind == BgActionKind.Door && verdict.Success && !this.FreshArrivalPose(target.ExpectedLocationId))
            return new(BgHoldCompletion.InteractionUncertain, false, BgActionEffect.EffectUnread, null, 0);
        return verdict;
    }

    /// <summary>The EffectDetail for a successful forage pickup (qualified id + delta).</summary>
    public static string? EffectDetailOf(BgActionTarget? target, BgActionEffectVerdict? verdict) =>
        target is { Kind: BgActionKind.Forage } && verdict is { Success: true } ? target.ItemId + "+" + verdict.Delta : null;

    /// <summary>WS-G6: "kind|item|delta|value" of a successful extras effect (from the last settle readback).</summary>
    public string? ExtraEffectDetail(BgActionEffectVerdict? verdict) =>
        (this.LastHoldTarget is { Kind: BgActionKind.Extra } or { ExtraKind: WorldExtras.NpcGift or WorldExtras.QuestDelivery })
            && verdict is { Success: true }
            ? BgExtrasEffect.Detail(this.LastExtraFacts, verdict.Delta) : null;

    /// <summary>WS-G6: the last settle readback of the hold just built (kept past the next arm's reset).</summary>
    public BgExtraFacts? LastExtraFacts { get; private set; }
    public EventActionFacts? LastEventBefore { get; private set; }
    public EventActionFacts? LastEventAfter { get; private set; }
    public string? LastGenericDelta { get; private set; }

    /// <summary>The pinned target of the last built hold result (ModEntry reads it while writing the verdict).</summary>
    public BgActionTarget? LastHoldTarget { get; private set; }

    // ------------------------------------------------------------------
    // move_through (walk-onto warp)
    // ------------------------------------------------------------------

    /// <summary>
    /// Admit one inactive move_through request: the ordinary background move run (same
    /// N-R / N-H / latch / substituted H-R-N / containment), plus an expected transition.
    /// </summary>
    public BackgroundMoveTerminal? TryArmThrough(MenuStepCommandRequest req, BgThroughExpectation expectation)
    {
        if (!MenuStepCommandInbox.IsMoveThrough(req) || req.ThroughTarget is not { } target
            || !string.Equals(target.ExpectedLocationId, expectation.ExpectedLocationId, StringComparison.Ordinal)
            || !string.Equals(target.Kind, expectation.Kind, StringComparison.Ordinal))
        {
            int id = this.nextRunId++;
            return new BackgroundMoveTerminal(BackgroundTerminalKind.Rejected, req,
                BgReason.PreconditionUnavailable("MOVE_THROUGH_TARGET"), 0, req.Direction ?? "", id);
        }
        if (req.Ticks < 1 || req.Ticks > MoveThroughTarget.MaxTicks)
        {
            int id = this.nextRunId++;
            return new BackgroundMoveTerminal(BackgroundTerminalKind.Rejected, req,
                BgReason.PreconditionUnavailable("INVALID_TICKS"), 0, req.Direction ?? "", id);
        }
        // WS-G0 slice 2: on the pad path TryArmCore runs the through move as a pad stick hold.
        BackgroundMoveTerminal? refused = this.TryArmCore(req, allowThrough: true);
        if (refused is null)
            this.through = new ThroughRun(expectation);
        return refused;
    }

    /// <summary>UpdateTicked while Moving / VerifyRelease: detect the warp start and stop asking for H.</summary>
    private void ObserveThroughStart()
    {
        if (this.through is not { } run || run.WarpStarted || run.Wrong is not null
            || (this.plan?.ConsumedH ?? 0) == 0)
            return;
        BgThroughProbe probe;
        try
        {
            probe = run.Expect.Probe();
        }
        catch
        {
            return;
        }
        run.Last = probe;
        if (this.ObserveBedTouch(run, probe))
            return;
        run.Progress.Observe(BackgroundContainmentWatchdog.NowMs(), probe.RequestToken, probe.LocationToken,
            probe.Transitioning, probe.PlayerFree, probe.MenuOpen, probe.EventUp, probe.FadeAlpha, true,
            this.localWarpSeen ? this.localWarpLocation : null, probe.RequestEpoch);
        (bool started, string? wrong) = BgMoveThrough.ObserveWarpStart(probe.IsWarping, run.Progress.CommittedRequest, run.Expect.ExpectedLocationId ?? "");
        if (!started && wrong is null)
            return;
        run.WarpStarted = started;
        run.Wrong = wrong;
        run.WarpStartMs = BackgroundContainmentWatchdog.NowMs();
        run.HAtWarpStart = this.plan?.ConsumedH ?? 0;
        // No movement key after the warp started: the next owned frame is R, then N.
        this.plan?.RequestRelease(started ? "WARP_STARTED" : "UNEXPECTED_WARP");
        this.monitor.Log(
            $"background MOVE_THROUGH warp {(started ? "started" : "to unexpected location")} run={this.runId} "
                + $"request={probe.RequestToken} expected={run.Expect.ExpectedLocationId} h={run.HAtWarpStart}",
            started ? LogLevel.Info : LogLevel.Warn);
    }

    /// <summary>
    /// VerifyRelease for a through run whose warp started: release proof is sticky (the
    /// location changes under it), then the expected transition decides. Returns true when
    /// this method owned the tick (a terminal may have been written).
    /// </summary>
    private bool TickThroughVerify(bool releaseEvidence)
    {
        if (this.through is not { } run || (!run.WarpStarted && run.Wrong is null))
            return false;
        if (releaseEvidence)
            run.ReleaseVerified = true;
        // The native warp may finish after STOP/lease loss. Cleanup is still
        // required, but that later arrival must not turn cancellation into success.
        if (run.ReleaseVerified && this.ThroughCancelled(run))
        {
            this.Finish(BackgroundTerminalKind.Uncertain,
                "NOT_COMPLETED:RELEASED_EARLY:" + (this.releaseReason ?? "STOP_LATCHED"));
            return true;
        }
        BgThroughProbe? probe = null;
        try
        {
            probe = run.Expect.Probe();
        }
        catch
        {
            // Unreadable this tick: the bounded wait below still applies.
        }
        if (probe is not null)
        {
            run.Last = probe;
            run.Progress.Observe(BackgroundContainmentWatchdog.NowMs(), probe.RequestToken, probe.LocationToken,
                probe.Transitioning, probe.PlayerFree, probe.MenuOpen, probe.EventUp, probe.FadeAlpha, true,
                requestEpoch: probe.RequestEpoch);
        }
        // A previous sample may aid diagnostics, never a successful terminal.
        BgThroughProbe last = probe ?? new BgThroughProbe(false, null, "", true, false, false);
        long now = BackgroundContainmentWatchdog.NowMs();
        this.LogTravelProgress(run.Progress, ref run.ProgressNotice, last.LocationToken);
        long since = run.Progress.Stalled(now) ? BgActionEffect.WarpWaitMs : 0;
        BgThroughFacts facts = new(
            run.WarpStarted,
            run.Wrong,
            (this.plan?.ConsumedH ?? 0) != run.HAtWarpStart,
            run.ReleaseVerified,
            (this.localWarpSeen && this.localWarpLocation == run.Expect.ExpectedLocationId
                || run.Progress.Committed && last.RequestToken is null)
                && probe is not null && this.FreshArrivalPose(run.Expect.ExpectedLocationId),
            last.LocationToken,
            last.Transitioning,
            last.PlayerFree,
            last.MenuOpen,
            since,
            last.EventUp);
        string? decision = BgMoveThrough.Decide(facts, run.Expect.ExpectedLocationId ?? "");
        if (run.Progress.Redirected && run.ReleaseVerified && probe is not null && !last.Transitioning)
            decision = "UNCERTAIN(TRAVEL_REDIRECTED:" + last.LocationToken + ")";
        if (decision is null)
            return true;
        run.Decision = decision;
        bool warped = decision is BgMoveThrough.Warped or BgMoveThrough.WarpedEventUp;
        this.monitor.Log(
            $"background MOVE_THROUGH COMPLETION run={this.runId} outcome={decision} location={last.LocationToken} "
                + $"warp_seen={this.localWarpSeen} release_verified={run.ReleaseVerified} phase={run.Progress.Phase} "
                + $"idle_ms={now - run.Progress.ChangedMs} request_epoch={run.Progress.CommittedEpoch} containment={this.policy?.ResultLabel}",
            warped ? LogLevel.Info : LogLevel.Warn);
        this.Finish(warped ? BackgroundTerminalKind.Completed : BackgroundTerminalKind.Uncertain, decision);
        return true;
    }

    /// <summary>
    /// FIX_TRAVEL_DEADLINE (smoke cand-o, 44/44 RELEASED_EARLY:DEADLINE, all arrived): the request
    /// deadline only bounds movement H. Once the expected native warp started, it is progress and
    /// BgMoveThrough's WarpWaitMs window is its no-progress bound. STOP, lease loss, any other release
    /// signal, a wrong destination and a HOST pause still cancel.
    /// </summary>
    private bool ThroughCancelled(ThroughRun run)
    {
        if (this.owner.StopLatched)
            return true;
        if (this.releaseReason is null)
            return false;
        return !(this.releaseReason == "DEADLINE" && this.signaled.Count == 1 && this.signaled.Contains("DEADLINE")
            && run.WarpStarted && run.Wrong is null && !(Game1.paused || Game1.HostPaused)
            && this.request is { } req && this.LeaseValidFor(req, NowUnixMs()));
    }

    /// <summary>Fresh game-thread location and finite, stopped position; no coordinate assumptions.</summary>
    private bool FreshArrivalPose(string? expected)
    {
        Capture pose = Capture.Now();
        return expected is not null && pose.Location.Length > 0 && SceneIdentity.Token(pose.Location) == expected
            && float.IsFinite(pose.X) && float.IsFinite(pose.Y) && pose.MovementDirections == 0;
    }

    /// <summary>The observed location of a successful through terminal (ModEntry result field).</summary>
    private string? ThroughResultLocation() =>
        this.through is { Decision: BgMoveThrough.Warped or BgMoveThrough.WarpedEventUp } run ? run.Last?.LocationToken : null;

    /// <summary>
    /// Review 2026-10-07 (finding 4): UpdateTicking re-check of a through run in Moving, before the
    /// hook can consume another H. A warp that started after the last UpdateTicked observation
    /// releases the plan here, so no movement key reaches the consumer while warping.
    /// </summary>
    private void ObserveThroughStartTicking()
    {
        if (this.phase == Phase.Moving && this.through is { WarpStarted: false, Wrong: null })
            this.ObserveThroughStart();
    }

    private void ResetInteractionState()
    {
        this.holdTarget = null;
        this.effectBefore = null;
        this.effectNow = null;
        this.effectTransitionSeen = false;
        this.doorProgress = new TravelProgress();
        this.doorProgressNotice = null;
        this.localWarpSeen = false;
        this.localWarpLocation = null;
        this.through = null;
    }
}
