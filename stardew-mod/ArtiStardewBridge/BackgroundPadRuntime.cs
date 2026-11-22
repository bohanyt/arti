using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

// Primitives v2 WS-G0 slice 1 (docs/stardew/primitives-v2/PLAN.md section 1.5, WS-G0_BRIEF.md
// section 2): the gamepad (pad) retained-press path INSIDE the existing Stage 2 machinery. Same
// lease, N-R / N-H / latch / N0 / POST_N0_CANONICALIZE, containment armed before the single Press,
// VERIFY exact frame, Suppress in the UpdateTicking finally, release proof, verdict vocabulary.
// What changes on the pad path (default OFF behind ModConfig.GamepadInputEnabled):
//   - the pinned key is a fixed pad pin (TOOL = X, ACTION = A, move = left-stick direction);
//   - gamepad mode is REQUIRED (ARTI owns ForceOn, PadModeOwner) instead of being a divergence;
//   - the cursor-hidden gate replaces CursorInert; furniture at the tool / grab tile is refused;
//   - a move is a VERIFY-role stick hold (one Press, B + {stick vector}), not a substituted frame;
//   - the release proof reads GetGamePadState() (button up / stick at zero) + IsSuppressed;
//   - neutral_tick_seen (PLAN 1.5 d) gates the first H frame.
// Compiled by the offline HoldRuntimeTests harness too (game/SMAPI doubles). Evidence ceiling:
// UNIT_TESTED. Everything about the real game is HYPOTHESIS until the CT witnesses (G0W1/G0W2).

/// <summary>Game-coupled pad-mode services the runtime needs (ModEntry.Pad.cs; a fake in the harness).</summary>
internal interface IPadModeHost
{
    /// <summary>ModConfig.GamepadInputEnabled (and the pad path wired).</summary>
    bool PadInputEnabled { get; }

    /// <summary>ModConfig.FarmhandInactiveRenderingEnabled: the inactive farmhand runs Draw (cursor fade).</summary>
    bool DrawPathOn { get; }

    /// <summary>Read-only native draw provenance; never an admission grant.</summary>
    string? CursorDrawDiagnostic => null;

    /// <summary>Same-binding owned ForceOn, no STOP/save/restore/fault; read-only authorization.</summary>
    bool CursorSyncOwned => false;

    /// <summary>Write a restorable, draw-generation-pinned override under current ownership.</summary>
    bool TryReconcileCursor() => false;

    /// <summary>
    /// Take (or keep) gamepad-mode ownership for an admitted pad request (PLAN 1.5 a). null = owned
    /// (<paramref name="justTook"/>: taken by this call, fade timer zeroed this tick); otherwise the
    /// refusal reason (e.g. the _oldGamepadConnectedState pin is unavailable).
    /// </summary>
    string? TakeForAdmission(out bool justTook);
}

/// <summary>
/// PLAN 1.5 c/d: the ONLY writer of <c>Game1.oldPadState</c> in this mod (G4's BobberBar adapter will
/// be the second). Writes a neutral (default) GamePadState: N-H history reset, menu-exit hygiene,
/// focus regain step 1, pad panic.
/// </summary>
internal static class PadHistoryHygiene
{
    public static int Writes { get; private set; }

    public static void WriteNeutral()
    {
        Game1.oldPadState = new GamePadState();
        Writes++;
    }
}

internal sealed partial class BackgroundMoveRuntime
{
    private bool padRequest;
    private readonly BgPadNeutralTickGate padNeutralTick = new();

    /// <summary>Every ARTI controller override (Press/Suppress) that SMAPI may still apply (PadModeOwner input).</summary>
    public PadOverrideLedger PadLedger { get; } = new();

    /// <summary>WS-G4: pad panics executed so far (the reflex runner aborts when this changes).</summary>
    public int PadPanics { get; private set; }

    /// <summary>The pad path is on: requests are pad holds / pad stick moves.</summary>
    public bool PadPathOn => this.padHost?.PadInputEnabled == true;

    /// <summary>The in-flight request uses the pad path.</summary>
    public bool PadRequestActive => this.request is not null && this.padRequest;

    /// <summary>
    /// FIXB16 D-B14b-2 (live V2B14bWATER-a: `PAD_MODE set ForceOn` and `PAD admission ... take=taken` in the same tick, then
    /// `HOLD COMPLETION InputUncertain SWING_COUNT_0`; the next press, `take=kept`, swung): a request admitted by the take
    /// waits PadTakeSettleTicks in N-R before its frames, so the game has read gamepadMode ForceOn for a whole update.
    /// </summary>
    public const int PadTakeSettleTicks = 1;
    private bool padJustTook;
    private int padTakeSettleTicks;

    /// <summary>Ticks the current request still waits after the take (tests / diagnostics).</summary>
    public int PadTakeSettleRemaining => this.padTakeSettleTicks;

    /// <summary>The last pad admission took gamepad mode and no request consumed / rejected it yet (tests).</summary>
    public bool PadJustTookPending => this.padJustTook;

    /// <summary>Start of every UpdateTicking (before any ARTI Press/Suppress of the tick).</summary>
    public void BeginPadTick(bool active) => this.PadLedger.BeginTick(active);

    /// <summary>
    /// PadAdmission (pad hold and pad move): every other admission term first (gamepad mode
    /// assumed), then the take of gamepadMode (whole inactive session), then the cursor-hidden gate
    /// with the take-over relaxations, then the real facts with gamepad mode REQUIRED.
    /// Returns a full refusal label, or null with <paramref name="facts"/> for the N-R/N-H plan.
    /// </summary>
    private string? PadAdmission(MenuStepCommandRequest req, int id, BgButton key, BgHoldKind? kind, bool bindingValid, bool chainAdmitted, bool latchFieldAdmitted,
        out BgAdmissionFacts facts, BgReflexPressKind? reflex = null)
    {
        // WS-G4: a reflex tap is admitted while its rod is in use (BgReflexPress.Relax); everything else stays strict.
        facts = BgReflexPress.Relax(this.EventMoveFacts(SampleAdmissionFacts(bindingValid, chainAdmitted, latchFieldAdmitted, padModeRequired: true), req),
            reflex, BgReflexRuntime.RodHeld());
        if (this.padHost is not { } host || !BgPadButtons.IsHoldPin(key))
            return BgReason.PreconditionUnavailable(BgHoldReason.PadModeUnavailable);
        if (BgAdmission.Reject(facts with { GamepadMode = true }) is string early)
            return BgReason.PreconditionUnavailable(early);
        // The cursor-hidden gate can never pass without the inactive Draw path: refuse before any take.
        if (!host.DrawPathOn)
            return BgReason.PreconditionUnavailable(BgCursorHidden.Prefix + ":draw_path_off");
        string? modeRefusal;
        bool justTook;
        try
        {
            modeRefusal = host.TakeForAdmission(out justTook);
        }
        catch (Exception ex)
        {
            modeRefusal = BgHoldReason.PadModeUnavailable + ":" + ex.GetType().Name;
            justTook = false;
        }
        if (modeRefusal is not null)
            return BgReason.PreconditionUnavailable(modeRefusal);
        this.ReconcilePadCursor(req, id, "admission");
        if (BgCursorHidden.Reject(this.SamplePadCursorHidden(), atTakeOver: justTook, ignoreRetainedInput: true) is string hidden)
        {
            // Admission has not stored this request/run yet; report the incoming identity, not a prior run.
            this.LogPadCursorRefusal(hidden, req, id, "admission", justTook ? "taken" : "kept");
            return BgReason.PreconditionUnavailable(hidden);
        }
        facts = BgReflexPress.Relax(this.EventMoveFacts(SampleAdmissionFacts(bindingValid, chainAdmitted, latchFieldAdmitted, padModeRequired: true), req),
            reflex, BgReflexRuntime.RodHeld());
        this.monitor.Log($"background PAD admission key={key} kind={kind?.ToString() ?? "Move"} take={(justTook ? "taken" : "kept")}",
            LogLevel.Info);
        this.padJustTook = justTook;
        return null;
    }

    /// <summary>
    /// Read-only cursor-hidden facts (PLAN 1.5 b), pinned to the installed Game1 MVID; null =
    /// unreadable (never hidden). No cursor warp, no mouse-state write.
    /// </summary>
    internal BgCursorHiddenFacts? SamplePadCursorHidden()
    {
        try
        {
            if (GameModuleVersionId() != PinnedGameMvid)
                return null;
            KeyboardState kb = Game1.input.GetKeyboardState();
            MouseState ms = Game1.input.GetMouseState();
            bool mouseDown = ms.LeftButton == ButtonState.Pressed || ms.RightButton == ButtonState.Pressed
                || ms.MiddleButton == ButtonState.Pressed || ms.XButton1 == ButtonState.Pressed || ms.XButton2 == ButtonState.Pressed;
            return new BgCursorHiddenFacts(
                DrawPathOn: this.padHost?.DrawPathOn == true,
                GamepadControls: Game1.options?.gamepadControls == true,
                WasMouseVisibleThisFrame: Game1.wasMouseVisibleThisFrame,
                MouseCursorTransparency: Game1.mouseCursorTransparency,
                TimerUntilMouseFade: Game1.timerUntilMouseFade,
                KeyDown: kb.GetPressedKeys().Length != 0,
                MouseButtonDown: mouseDown,
                EmoteBlocksFade: Game1.emoteMenu is { gamepadMode: false });
        }
        catch
        {
            return null;
        }
    }

    /// <summary>The strict cursor-hidden reason (no relaxation), or null = hidden.</summary>
    internal string? PadCursorHiddenReason() => BgCursorHidden.Reject(this.SamplePadCursorHidden());

    internal string? PadCursorRequestHiddenReason() => BgCursorHidden.Reject(this.SamplePadCursorForRequest());

    private static BgVanillaCursorFacts SampleVanillaCursor() => new(
        Game1.options?.gamepadControls == true, Game1.timerUntilMouseFade,
        Game1.activeClickableMenu is not null, Game1.emoteMenu is { gamepadMode: false },
        Game1.mouseCursor > Game1.cursor_none, Game1.currentLocation is not null,
        Game1.displayHUD && Game1.gameMode == 3 && !Game1.eventUp && !Game1.freezeControls && !Game1.panMode
            && !Game1.globalFade && Game1.currentMinigame is null && Game1.game1 is { takingMapScreenshot: false });

    private BgCursorHiddenFacts? SamplePadCursorForRequest()
    {
        if (this.padRequest && this.request is { } req)
            this.ReconcilePadCursor(req, this.runId, "guard");
        return this.SamplePadCursorHidden();
    }

    // FIXB54 r2: update-time A/X read the last draw's latch. Reproduce ONLY its hidden value;
    // no Draw call, mouse coordinates, transparency, timer, options or input-history write.
    private void ReconcilePadCursor(MenuStepCommandRequest req, int id, string stage)
    {
        try
        {
            if (GameModuleVersionId() != PinnedGameMvid || this.padHost?.CursorSyncOwned != true
                || !Context.IsWorldReady || Context.IsMainPlayer || Context.IsSplitScreen
                || this.releaseReason is not null // R5 S3: no focus term; the vanilla-cursor model below refuses on real mouse use
                || !BindingValidFor(req) || !this.LeaseValidFor(req, NowUnixMs()))
                return;
            BgCursorHiddenFacts? cursor = this.SamplePadCursorHidden();
            if (cursor is not { WasMouseVisibleThisFrame: true, KeyDown: false, MouseButtonDown: false }
                || !BgVanillaCursor.WouldHide(SampleVanillaCursor()))
                return;
            if (!this.padHost.TryReconcileCursor())
                return;
            this.monitor.Log($"background PAD cursor reconcile run={id} request_id={req.RequestId} "
                + $"session_id={req.SessionId} runtime_id={req.RuntimeInstanceId} stage={stage} tick={Game1.ticks} "
                + $"before_visible=True after_visible=False vanilla_hidden=True "
                + $"facts={cursor} draw={this.padHost.CursorDrawDiagnostic ?? "unavailable"}", LogLevel.Info);
        }
        catch { /* unreadable/unauthorized state never grants a latch write */ }
    }

    private void LogPadCursorRefusal(string reason, MenuStepCommandRequest req, int id, string stage, string? take = null)
    {
        try
        {
            this.monitor.Log($"background PAD cursor refusal run={id} request_id={req.RequestId} "
                + $"session_id={req.SessionId} runtime_id={req.RuntimeInstanceId} stage={stage} take={take ?? "n/a"} reason={reason} "
                + $"facts={this.SamplePadCursorHidden()?.ToString() ?? "unreadable"} "
                + $"draw={this.padHost?.CursorDrawDiagnostic ?? "unavailable"}", LogLevel.Warn);
        }
        catch { /* diagnostic failure cannot change the fail-closed verdict */ }
    }

    private void LogPadCursorReady(MenuStepCommandRequest req, int id)
    {
        try
        {
            this.monitor.Log($"background PAD cursor ready run={id} request_id={req.RequestId} "
                + $"session_id={req.SessionId} runtime_id={req.RuntimeInstanceId} stage=N-H "
                + $"facts={this.SamplePadCursorHidden()?.ToString() ?? "unreadable"} "
                + $"draw={this.padHost?.CursorDrawDiagnostic ?? "unavailable"}", LogLevel.Info);
        }
        catch { /* read-only diagnostics cannot change the guarded verdict */ }
    }

    /// <summary>
    /// WS-G0 ACTION on the pad path: non-walkable furniture whose bounding box intersects the grab
    /// tile (player tile + facing) is refused; a rug / walkable floor furniture holding nothing is
    /// harmless (pressActionButton never calls LowPriorityLeftClick). Read fault = refused.
    /// </summary>
    private static string? FurnitureAtGrabTileReason(bool facedTv = false)
    {
        try
        {
            Farmer? player = Context.IsWorldReady ? Game1.player : null;
            GameLocation? location = player?.currentLocation;
            if (player is null || location is null || BgToolTileGuard.FacingDelta(player.FacingDirection) is not var (dx, dy))
                return BgToolTileReason.FurnitureAtGrabTile + ":UNREADABLE";
            // Review minor 5: the A press acts on Character.GetGrabTile() (bounding box +-5 px), not Tile + facing.
            Vector2 grab = player.GetGrabTile();
            int gx = (int)grab.X, gy = (int)grab.Y;
            Rectangle tile = new(gx * 64, gy * 64, 64, 64);
            bool blocked = location.furniture.Any(f => f is not null && f.GetBoundingBox().Intersects(tile)
                && !(facedTv && f.GetType() == typeof(StardewValley.Objects.TV) && f.heldObject.Value is null)
                && !((f.furniture_type.Value == StardewValley.Objects.Furniture.rug || f.isPassable()) && f.heldObject.Value is null));
            return blocked ? BgToolTileReason.FurnitureAtGrabTile : null;
        }
        catch
        {
            return BgToolTileReason.FurnitureAtGrabTile + ":UNREADABLE";
        }
    }

    /// <summary>
    /// UpdateTicking, before the single pad Press: the cursor must be hidden now (the VERIFY hook
    /// re-checks every entry). Returns false (and latches the reason) only while no Press was issued.
    /// </summary>
    private bool PrePressPadCheck(BgPressSession session)
    {
        if (this.request is { } req && !this.NativeRouteValid(req)) return false;
        if (!this.padRequest || session.PressIssued)
            return true;
        if (this.PadCursorRequestHiddenReason() is not string hidden)
            return true;
        if (this.releaseReason is null)
            this.monitor.Log($"background PAD press withheld run={this.runId} reason={hidden}", LogLevel.Warn);
        this.releaseReason ??= hidden;
        return false;
    }

    /// <summary>A pad move completes only after the stick release is proved (keyboard moves: always true).</summary>
    private bool PadReleaseSettled => this.pressSession is not { PressIssued: true, ReleaseProved: false };

    /// <summary>
    /// R1 effective read for the release proof of a pad pin (UpdateTicked, never SMAPI IsDown): the pad
    /// button down, or any left-stick deflection for a stick pin. A read fault propagates (TICKED_EXCEPTION,
    /// fail closed), exactly like the keyboard read.
    /// </summary>
    private bool EffectivePadDown(BgButton key)
    {
        GamePadState pad = Game1.input.GetGamePadState();
        if (BgPadButtons.IsStick(key))
            return pad.ThumbSticks.Left != Vector2.Zero;
        return !Enum.TryParse(key.Name, out Buttons b) || pad.IsButtonDown(b);
    }

    private List<SButton> SButtonsOf(IEnumerable<BgButton> buttons)
    {
        List<SButton> result = new();
        foreach (BgButton b in buttons)
        {
            if (BackgroundInputNormalizer.TryToSButton(b, out SButton s))
                result.Add(s);
        }
        return result;
    }

    // ------------------------------------------------------------------
    // Pad move (VERIFY role): one Press of the stick direction, nothing substituted.
    // ------------------------------------------------------------------

    private void ServePadMoving(ArtiControlInputHooks from, ref KeyboardState k, ref MouseState m, ref GamePadState p, Action action)
    {
        KeyboardState kNow = k;
        MouseState mNow = m;
        GamePadState pNow = p;
        BgMoveFramePlan move = this.plan!;
        BgHistoryInvariant inv = this.invariant!;
        bool suppressedBefore = this.pressSession?.SuppressIssued == true;
        string? diverged = null;
        BgHookDecision? decision = null;
        BgHookFacts? sampled = null;
        try
        {
            // VERIFY role (Live7 rehearsal 9): once the move is in progress, StopLatched / LeaseValid are this tick's
            // Ticking snapshot, the same view the press decision used; a later deadline / STOP / lease change
            // releases next tick. Before the first H the deadline / lease are the snapshot's too; a hook-time owner STOP /
            // release still refuses the press (Live7 containment follow-up).
            decision = BgHookEntry.Decide(() => sampled = BgTickStopSnapshot.ApplyForTick(this.tickStop, this.request!.Value.RequestId,
                    move.ConsumedH, this.SampleHookFacts(from, kNow, mNow, pNow) with { PadMoveGate = this.PadCursorRequestHiddenReason() },
                    liveStopOrRelease: this.releaseReason is not null || this.owner.StopLatched),
                move, inv);
            switch (decision.Action)
            {
                case BgHookAction.SkipDivergence:
                    diverged = decision.Reason ?? BgReason.PreconsumerHistoryDivergence;
                    diverged = NameForeignInput(diverged, this.LogVerifyMismatch(diverged, move, inv, sampled, kNow, mNow, pNow));
                    break;
                case BgHookAction.PassThroughForeground:
                case BgHookAction.VerifyHeld:
                case BgHookAction.VerifyNeutral:
                    break;
                default:
                    diverged = BgReason.PreconsumerHistoryDivergence + ":NO_GRANT_UNDER_ARTI_LATCH";
                    break;
            }
            // A held stick frame is never consumed unless press containment is armed (R2).
            if (diverged is null && decision.Action == BgHookAction.VerifyHeld && this.policy?.PressOutstanding != true)
                diverged = BgReason.PreconsumerHistoryDivergence + ":PRESS_NOT_CONTAINED";
        }
        catch (Exception ex)
        {
            diverged = "CHECK_EXCEPTION:" + ex.GetType().Name;
        }

        if (diverged is not null)
        {
            this.OnDivergence(diverged);
            return;
        }

        if (decision!.Action == BgHookAction.PassThroughForeground)
        {
            from.InvokeParent(ref k, ref m, ref p, action);
            return;
        }

        BgHookDecision admitted = decision;
        BgFrameKind admittedFrame = admitted.Frame!.Value;
        this.tokenInFlight = true;
        BackgroundFrameCommit.InvokeThenCommit(
            ref k, ref m, ref p,
            (ref KeyboardState kk, ref MouseState mm, ref GamePadState pp) => from.InvokeParent(ref kk, ref mm, ref pp, action),
            () => this.CommitPadMoving(move, inv, admitted, suppressedBefore),
            () => this.OnParentFault(admittedFrame.ToString()),
            ex => this.OnCommitFault(ex));
    }

    /// <summary>
    /// Live D-B1-2 diagnostics: on a VERIFY_FRAME_MISMATCH log the exact expected owned frame next to the
    /// retained frame (never throws: runs inside the hook). Live7 rehearsal 9: the expected frame is the one
    /// the decision really used (EffectiveNext with the sampled stop / lease facts), and the stop components
    /// (release reason, owner STOP, deadline minus now, lease, this tick's snapshot) are printed with it.
    /// </summary>
    /// <returns>ADR 244: the <see cref="BgForeignInput.Describe"/> name of the difference, or null.</returns>
    private string? LogVerifyMismatch(string reason, IBgFramePlan plan, BgHistoryInvariant inv, BgHookFacts? facts,
        KeyboardState k, MouseState m, GamePadState p)
    {
        try
        {
            if (!reason.Contains(BgHoldReason.VerifyFrameMismatch, StringComparison.Ordinal))
                return null;
            bool stopOrLeaseInvalid = facts is { } f && (f.StopLatched || !f.LeaseValid);
            BgFrameKind frame = plan.EffectiveNext(stopOrLeaseInvalid);
            BgInputState expected = plan.FrameFor(frame, inv.Baseline);
            BgInputState retained = BackgroundInputNormalizer.Normalize(k, m, p, this.padRequest);
            this.monitor.Log($"background VERIFY mismatch run={this.runId} frame={frame} next={plan.Next} key={plan.Key} expected={expected.Describe()} "
                + $"retained={retained.Describe()} ledger=[{string.Join(",", this.PadLedger.Names)}] outstanding=[{string.Join(",", this.PadLedger.Outstanding)}] "
                + this.StopComponents(facts), LogLevel.Warn);
            return BgForeignInput.Describe(expected, retained);
        }
        catch
        {
            // diagnostics only
            return null;
        }
    }

    private void CommitPadMoving(BgMoveFramePlan move, BgHistoryInvariant inv, BgHookDecision decision, bool suppressedBefore)
    {
        this.tokenInFlight = false;
        BackgroundFrameCommit.CommitMovingFrame(move, inv, decision);
        long now = BackgroundContainmentWatchdog.NowMs();
        this.tokenConsumed = true;
        this.substitutedThisTick = true;
        this.lastConsumedMs = now;
        this.policy?.OnFrameConsumed(now);
        // R1: a VERIFY-admitted neutral frame after Suppress is the stick-absent receipt.
        if (decision.Frame != BgFrameKind.H && suppressedBefore)
            this.keyAbsentReceipt = true;
        this.lastHookReceipt = "PAD VERIFY " + decision.Frame + " seq=" + this.updateSeq + " h=" + move.ConsumedH + "/" + move.TargetHFrames
            + " branch=" + inv.Branch + (decision.Reason is null ? "" : " reason=" + decision.Reason);
    }

    // ------------------------------------------------------------------
    // Pad panic / focus-regain helpers (PLAN 1.5 a step 1, 1.5 e). Game thread only.
    // ------------------------------------------------------------------

    /// <summary>
    /// Step 1 of the focus-regain sequence and every lifecycle restore: Suppress every outstanding
    /// ARTI pad press through the port (the in-flight session first, so its bookkeeping stays exact).
    /// Returns the suppressed pad button names.
    /// </summary>
    public IReadOnlyList<string> SuppressOutstandingPad(string reason)
    {
        List<string> done = new();
        if (this.pressSession is { Outstanding: true } s && s.Key.Device == BgDevice.Pad && !s.SuppressIssued)
        {
            s.ForceRelease("PAD_MODE:" + reason);
            done.Add(s.Key.Name);
        }
        foreach (string name in this.PadLedger.Outstanding)
        {
            if (done.Contains(name, StringComparer.Ordinal))
                continue;
            try
            {
                this.port.Suppress(BgButton.Pad(name));
                done.Add(name);
            }
            catch (Exception ex)
            {
                this.monitor.Log($"background PAD suppress fault button={name}: {ex.GetType().Name}", LogLevel.Error);
            }
        }
        return done;
    }

    /// <summary>
    /// PLAN 1.5 e pad panic (idempotent): steer any request to R/N, release the session's press,
    /// Suppress EVERY pad button and stick direction of the panic set through the port. The mode
    /// restore and the neutral oldPadState are the caller's (PadModeOwner) part.
    /// </summary>
    public IReadOnlyList<string> PadPanic(string reason, bool suppressAll = true)
    {
        this.PadPanics++; // WS-G4: the fishing reflex aborts on any panic it observes
        // STOP is the adapter's ordinary bounded release path. Other panic
        // causes must remain faults even if a STOP follows them in the same tick.
        if (reason != "STOP")
        {
            this.FestivalFaultPanics++;
            this.FestivalLastFaultPanicReason = reason;
        }
        if (this.request is not null)
            this.RequestRelease("PAD_PANIC:" + reason);
        if (this.pressSession is { } s && s.Key.Device == BgDevice.Pad)
            s.ForceRelease("PAD_PANIC:" + reason);
        List<string> done = new();
        // Review major 1: nothing owned and nothing in the ledger = nothing to release; issuing 16 Suppress
        // overrides would only create a "connected" virtual pad (the caller pins when it does issue some).
        foreach (string name in suppressAll ? BgPadButtons.PanicSet : Array.Empty<string>())
        {
            try
            {
                this.port.Suppress(BgButton.Pad(name));
                done.Add(name);
            }
            catch (Exception ex)
            {
                this.monitor.Log($"PAD_PANIC suppress fault button={name}: {ex.GetType().Name}", LogLevel.Error);
            }
        }
        this.monitor.Log($"PAD_PANIC reason={reason} suppressed=[{string.Join(",", done)}] request={this.request?.RequestId ?? "-"}",
            LogLevel.Warn);
        return done;
    }
}
