using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

// WS3b (Primitives v1, 2026-10-07): the synchronous-receiver path of the background ACTION
// hold (npc talk, eat prompt: the DialogueBox opens inside the H frame) and the bed variant of
// move_through (the Sleep question opens mid-walk). Hooks only: no new input path, no menu
// call, no Game1.old* write. The hold keeps its single pinned key / VERIFY / Suppress /
// release proof / latch removal / containment; the move run keeps its substituted frames.
// Game facts arrive through the pinned target's closures (BgActionTarget.ExpectedReceiver,
// BgThroughExpectation.Probe), so this partial compiles against the offline harness doubles.
// Evidence ceiling: UNIT_TESTED. HYPOTHESIS until the CT live witness.
internal sealed partial class BackgroundMoveRuntime
{
    /// <summary>
    /// The pinned ACTION receiver: the chest ItemGrabMenu by reference (M1 rule unchanged), or
    /// the npc / eat target's expected DialogueBox, or a found quest item's dialogue (ADR 136). Never true for a door or any other forage.
    /// </summary>
    private bool IsExpectedHoldReceiver(IClickableMenu? menu)
    {
        if (menu is null || this.holdKind != BgHoldKind.Action)
            return false;
        if (IsPinnedChestMenu(menu, this.holdChest))
            return true;
        if (this.holdTarget is not { ExpectedReceiver: { } expected } target || !BgSyncReceiver.IsSynchronous(target.Kind))
            return false;
        try
        {
            return expected(menu);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>TickHolding: H consumed and the synchronous receiver is up (no R / N will follow).</summary>
    private bool SyncReceiverFramesDone(BgHoldFramePlan hold) =>
        BgSyncReceiver.FramesDone(this.HoldActionKind, hold.ConsumedH, hold.RConsumed, this.pinnedMenuObserved);

    /// <summary>BuildHoldResult: canonical history, or exactly the consumed H frame for a synchronous receiver.</summary>
    private bool HoldHistoryAcceptable(BgInputState history)
    {
        if (this.invariant is not { } inv)
            return true;
        bool baseline = history.Equals(inv.Baseline);
        bool consumedH = inv.PreviousSynthetic is { } last && history.Equals(last)
            && inv.Branch != BgHistoryBranch.No && inv.ConsumedFrames >= 1;
        if (this.FishingCatchReceiver(Game1.activeClickableMenu))
            return baseline || consumedH; // observe the consumed history; never rewrite it to manufacture R/N
        return BgSyncReceiver.HistoryAcceptable(this.HoldActionKind, this.pinnedMenuObserved,
            this.holdPlan?.RConsumed == true, baseline, consumedH);
    }

    // ------------------------------------------------------------------
    // move_through kind "bed"
    // ------------------------------------------------------------------

    /// <summary>
    /// ObserveThroughStart for a bed run: records the first Sleep question sighting and stops
    /// asking for H frames. Returns true for every bed run (the warp rules never apply to it).
    /// </summary>
    private bool ObserveBedTouch(ThroughRun run, BgThroughProbe probe)
    {
        if (!string.Equals(run.Expect.Kind, MoveThroughTarget.KindBed, StringComparison.Ordinal))
            return false;
        run.StartLocation ??= probe.LocationToken;
        if (run.TouchSeen || !probe.SleepQuestion)
            return true;
        run.TouchSeen = true;
        run.TouchMs = BackgroundContainmentWatchdog.NowMs();
        run.HAtWarpStart = this.plan?.ConsumedH ?? 0;
        // No movement key after the question: the menu setter already halted the farmer and no
        // further owned frame can be consumed while the DialogueBox is up. Remove the latch now.
        this.plan?.RequestRelease("TOUCH_QUESTION");
        if (this.latch is not null)
            this.latchRemovalDue = true;
        this.monitor.Log(
            $"background MOVE_THROUGH bed touch question run={this.runId} h={run.HAtWarpStart} location={probe.LocationToken}",
            LogLevel.Info);
        return true;
    }

    /// <summary>
    /// UpdateTicked (Moving / VerifyRelease) for a bed run whose Sleep question opened: decides
    /// the terminal once the latch is removed and containment released. Returns true when this
    /// method owned the tick.
    /// </summary>
    private bool TickBedThrough()
    {
        if (this.through is not { TouchSeen: true } run
            || !string.Equals(run.Expect.Kind, MoveThroughTarget.KindBed, StringComparison.Ordinal))
            return false;
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
            run.Last = probe;
        BgThroughProbe last = run.Last ?? new BgThroughProbe(false, null, "", true, false, false);
        BgBedFacts facts = new(
            true,
            last.SleepQuestion,
            last.MenuOpen && !last.SleepQuestion,
            string.Equals(last.LocationToken, run.StartLocation, StringComparison.Ordinal),
            this.latch is null && !this.latchRemovalDue,
            this.latchRestoredExact,
            this.policy?.Armed != true && this.PadReleaseSettled,
            MovementCount(),
            BackgroundContainmentWatchdog.NowMs() - run.TouchMs);
        string? decision = BgBedThrough.Decide(facts);
        if (decision is null)
            return true;
        run.Decision = decision;
        bool ok = string.Equals(decision, BgBedThrough.TouchQuestionSleep, StringComparison.Ordinal);
        this.monitor.Log(
            $"background MOVE_THROUGH COMPLETION run={this.runId} outcome={decision} location={last.LocationToken} "
                + $"question={last.SleepQuestion} latch_restored_exact={this.latchRestoredExact} containment={this.policy?.ResultLabel}",
            ok ? LogLevel.Info : LogLevel.Warn);
        this.Finish(ok ? BackgroundTerminalKind.Completed : BackgroundTerminalKind.Uncertain, decision);
        return true;
    }
}
