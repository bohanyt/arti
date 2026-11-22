using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

// Kept together so removal, routing and the real wrapper callback can be exercised
// without loading the game. Production and the focused tests compile these same methods.
internal sealed partial class ArtiControlInputHooks
{
    public override void OnGame1_UpdateControlInput(
        ref KeyboardState keyboardState,
        ref MouseState mouseState,
        ref GamePadState gamePadState,
        Action action)
    {
        BackgroundMoveRuntime.HookRoute route;
        try
        {
            route = this.runtime.Route();
        }
        catch
        {
            route = this.runtime.RouteOnFault();
        }

        if (route == BackgroundMoveRuntime.HookRoute.PassThrough)
        {
            // Foreground / no ARTI grant: exact pass-through, original refs, Parent once.
            this.Parent.OnGame1_UpdateControlInput(ref keyboardState, ref mouseState, ref gamePadState, action);
            return;
        }

        this.runtime.Serve(this, route, ref keyboardState, ref mouseState, ref gamePadState, action);
    }

}

internal sealed partial class BackgroundMoveRuntime
{
    internal enum HookRoute { PassThrough, PreH, Moving, SkipFailClosed, Holding, SkipOwnedPress }

    private enum Phase { Idle, NeutralizeRetained, AwaitN0, PreHNeutral, Moving, VerifyRelease, Closing, Holding, HoldSettle }

    // Latch (identity per request, never silently reinstalled).
    private ArtiReachabilityLatch? latch;
    private bool latchRemovalDue;
    private bool latchRestoredExact;
    private bool latchEverInstalled;
    // Sticky after an unconfirmed removal. Only a confirmed clear resets it;
    // request/lifecycle cleanup must not forget unresolved field ownership.
    private bool latchRestorationUnresolved;

    /// <summary>
    /// Busy while a request is in flight, while latch restoration is unresolved,
    /// or while containment stays armed (movement outstanding after the request's
    /// result), or while a Stage 2 owned press override is not proved released.
    /// New mutations and owner release ACK wait for this.
    /// </summary>
    public bool Busy => this.request is not null || this.latch is not null
        || this.latchRestorationUnresolved || this.policy?.Armed == true
        || this.PressOverrideOutstanding;

    /// <summary>
    /// Stage 2 (independent review Q1): an owned Press was issued and no Suppress has
    /// succeeded yet, so the pinned key may be in the effective frame. Pass-through must
    /// not hand that frame to Parent without VERIFY.
    /// </summary>
    private bool OwnedPressAwaitingSuppress =>
        this.pressSession is { PressIssued: true, SuppressIssued: false, ReleaseProved: false };

    /// <summary>
    /// ADR 244: window focus is not a routing term for her requests. While one of her requests is in
    /// flight the same VERIFY / SUBSTITUTE roles serve the frame whether the window is active or not;
    /// a foreign input in an active window is caught by the frame comparison and named. With nothing of
    /// hers in flight (no request, no unsuppressed press) an active window's frames are the owner's and
    /// pass through untouched, exactly as before (including a latch whose removal is unresolved).
    /// </summary>
    internal HookRoute Route()
    {
        if (this.OwnedPressAwaitingSuppress && this.latch is null && !this.latchRestorationUnresolved)
            return HookRoute.SkipOwnedPress; // the latch is gone but an owned press may still be applied: never unverified
        if (this.latch is null && !this.latchRestorationUnresolved)
            return HookRoute.PassThrough; // no ARTI grant: vanilla/foreign untouched
        if (Game1.game1.IsActive && this.request is null && !this.OwnedPressAwaitingSuppress)
            return HookRoute.PassThrough; // nothing of hers in flight: the active window's frame is the owner's
        // A cancellation after the press decision cannot consume that tick's retained H.
        // Skip it without inventing history divergence; next Ticking suppresses and serves R/N.
        if (this.executionAttempt?.CancelReason is not null && this.request is { } cancelled
            && this.tickStop is { StopLatched: false } stop && stop.AppliesTo(this.updateSeq, cancelled.RequestId))
            return HookRoute.SkipFailClosed;
        if (this.latchRestorationUnresolved)
            return HookRoute.SkipFailClosed;
        return this.phase switch
        {
            Phase.AwaitN0 or Phase.PreHNeutral => HookRoute.PreH,
            Phase.Moving when this.plan?.Active == true => HookRoute.Moving,
            // Stage 2 VERIFY role: only while the hold plan is active.
            Phase.Holding when this.holdPlan?.Active == true => HookRoute.Holding,
            // ARTI latch installed while inactive but nothing admitted to serve:
            // fail closed (vanilla-inactive semantics), never leak real input.
            _ => HookRoute.SkipFailClosed,
        };
    }

    internal HookRoute RouteOnFault() => this.latch is null && !this.latchRestorationUnresolved && !this.OwnedPressAwaitingSuppress
        ? HookRoute.PassThrough : HookRoute.SkipFailClosed;

    internal void Serve(ArtiControlInputHooks from, HookRoute route, ref KeyboardState k, ref MouseState m, ref GamePadState p, Action action)
    {
        switch (route)
        {
            case HookRoute.PreH:
                this.ServePreH(from, ref k, ref m, ref p, action);
                return;
            case HookRoute.Moving:
                this.ServeMoving(from, ref k, ref m, ref p, action);
                return;
            case HookRoute.Holding:
                this.ServeHolding(from, ref k, ref m, ref p, action);
                return;
            case HookRoute.SkipOwnedPress:
                this.OnOwnedPressForegroundSkip();
                return; // zero Parent calls; the next UpdateTicking (active) Suppresses
            default:
                this.lastHookReceipt = "SKIP_FAIL_CLOSED seq=" + this.updateSeq;
                return; // zero Parent calls, no substitution, no old* write
        }
    }

    /// <summary>
    /// Compare-before-restore through the reflected field: re-read; only when it still
    /// holds exactly the owned latch may ARTI write null; then re-read to verify. A
    /// foreign replacement/clear is never overwritten, and nothing is retried; any
    /// unconfirmed outcome fails closed.
    /// </summary>
    private void RemoveLatch()
    {
        // One attempt, including lifecycle calls after a failure. Retain the identity
        // and busy/fail-closed condition until process teardown; never blindly retry.
        this.latchRemovalDue = false;
        ArtiReachabilityLatch? owned = this.latch;
        if (owned is null || this.latchRestorationUnresolved)
            return;
        this.latchRestorationUnresolved = true;
        this.latchRestoredExact = false;
        bool heldOwned = this.LatchFieldHolds(owned);
        if (heldOwned && this.fields?.TryWriteInputSimulator(null) == true && this.LatchFieldEmpty())
        {
            this.latchRestoredExact = true;
            this.latchRestorationUnresolved = false;
            this.latch = null;
            if (this.request is null && this.phase == Phase.Closing)
                this.phase = Phase.Idle;
            this.monitor.Log($"background LATCH removed exact run={owned.RunId} receipts={owned.Receipts}", LogLevel.Info);
            return;
        }

        // Keep the owned identity even if the field is foreign or unreadable. Closing
        // prevents post-N success/disarm logic; TickedCore reports an uncertain terminal.
        this.dispatchImpossible = true;
        this.divergence ??= "LATCH_RESTORATION_UNRESOLVED";
        this.phase = Phase.Closing;
        this.ReportDispatchImpossible("LATCH_RESTORE", BackgroundContainmentWatchdog.NowMs(), Game1.game1.IsActive, MovementCount());
        this.monitor.Log(heldOwned
            ? $"background LATCH removal unconfirmed run={owned.RunId}; identity retained, no retry (fail closed)"
            : $"background LATCH not owned at restore run={owned.RunId}; foreign/unknown value untouched, identity retained (fail closed)",
            LogLevel.Warn);
    }

}
