using ArtiStardewBridge.Protocol;
using StardewValley;

namespace ArtiStardewBridge;

/// <summary>Only a non-HOST, non-split farmhand may supply a binding. References pin the actual instances.</summary>
internal sealed record PadModeBinding(object Game, object Player, object Options, string Session, int Screen)
{
    public bool Matches(PadModeBinding? other) => other is not null
        && ReferenceEquals(this.Game, other.Game) && ReferenceEquals(this.Player, other.Player)
        && ReferenceEquals(this.Options, other.Options) && this.Session == other.Session && this.Screen == other.Screen;
}

// Primitives v2 WS-G0: the pad-mode adapter (PLAN 1.5 a/d/e) between the pure PadModeOwner, the
// background runtime (press port / ledger / pad panic) and the game. Mode/options/pin reads and writes go
// through IPadGame (ModEntry.Pad.cs: Game1 / Options / the reflected _oldGamepadConnectedState; the
// HoldRuntimeTests harness: a fake), so the take-over, the per-tick restore / pin, STOP and pad panic,
// menu-exit hygiene and lifecycle exits are behaviour-tested against the production runtime.
// FIXB54's cursor latch override is also retired here, independently of options restore authority.
// Evidence ceiling: UNIT_TESTED.

/// <summary>Game-side operations of the pad-mode adapter (game thread only).</summary>
internal interface IPadGame
{
    bool Active { get; }
    bool WorldReady { get; }
    bool MenuOpen { get; }
    /// <summary>A SaveGameMenu in the menu chain (its update writes startup_preferences clientOptions).</summary>
    bool SaveMenuOpen { get; }
    bool StopLatched { get; }
    /// <summary>Action lease retired/acknowledged, no command or native input in flight.</summary>
    bool LeaseReleased { get; }
    /// <summary>GetGamePadState().IsConnected now (SMAPI cached state).</summary>
    bool PadConnected { get; }
    long Tick { get; }
    PadModeBinding? ReadBinding();
    /// <summary>The live Options values (gamepadMode as string, gamepadControls, snappyMenus), or null.</summary>
    PadModeSnapshot? ReadOptions();
    void WriteOptions(PadModeSnapshot values);
    /// <summary>gamepadMode = ForceOn, gamepadControls = true, snappyMenus = false, timerUntilMouseFade = 0.</summary>
    void TakeForceOn();
    /// <summary>gamepadMode = ForceOn, gamepadControls = true (owned, but something changed them).</summary>
    void ReassertForceOn();
    /// <summary>Resolve the _oldGamepadConnectedState pin (null = available).</summary>
    string? ResolvePin();
    /// <summary>Write _oldGamepadConnectedState (throws on failure).</summary>
    void WritePin(bool value);
    void Log(string line, bool warn = false);
}

internal sealed class PadModeAdapter : IPadModeHost
{
    private readonly IPadGame game;
    private readonly Func<BackgroundMoveRuntime?> runtime;
    private readonly Func<bool> enabled;
    private readonly Func<bool> drawPath;
    private readonly Func<bool> warpGuardInstalled;
    private readonly Func<string?>? cursorDrawDiagnostic;
    private readonly Func<long?> cursorLatchGeneration;
    private sealed record CursorOverride(PadModeBinding Binding, long Generation, bool Before);
    private CursorOverride? cursorOverride;
    private string? panicPending;
    // WS-G5 review minor 2: a STOP panic waits (bounded) while a reflex CHARGE press is in flight, so the graceful release's
    // R frame still reaches UpdateControlInput in gamepad mode (X release edge -> one EndUsingTool at the current power).
    private int chargeStopDeferTicks;
    private bool stopDeferredThisTick;
    public const int ChargeStopDeferMaxTicks = 30;
    // Live7 M12 (2026-10-23): the same wait for a pad move / hold in flight. The STOP latch already releases it (the press
    // session Suppresses its key in that same UpdateTicking; the plan goes R -> N); restoring gamepad mode before R/N closed
    // the request's gamepad gate mid-walk (GAMEPAD_MODE_NOT_OWNED) with movement still set -> divergence -> T2 FailFast.
    // Release first, then the STOP panic + mode exit. Bounded: past it the panic runs as before (containment stays).
    private int requestStopDeferTicks;
    public const int RequestStopDeferMaxTicks = 90;
    private bool saveWindow;
    private bool stopSeen;
    private bool prevMenuOpen;
    private bool oldPadNeutralDue;
    private bool disposed;
    private PadModeSnapshot? restoreRetry;
    private PadModeBinding? binding;
    private PadModeSnapshot written;
    private DropdownWitness? dropdownWitness;
    private int restoreAttempts;
    private long restoreStartedTick, nextRestoreTick;
    private bool unownedModeReported;
    public const int RestoreAttemptLimit = 4;
    public const int RestoreBackoffTicks = 15;
    public const int RestoreRetryTickLimit = 180;
    public const int DisposeAttemptLimit = 3;

    public PadModeAdapter(IPadGame game, Func<BackgroundMoveRuntime?> runtime, Func<bool> enabled, Func<bool> drawPath,
        Func<bool> warpGuardInstalled, Func<string?>? cursorDrawDiagnostic = null, Func<long?>? cursorLatchGeneration = null)
    {
        this.game = game;
        this.runtime = runtime;
        this.enabled = enabled;
        this.drawPath = drawPath;
        this.warpGuardInstalled = warpGuardInstalled;
        this.cursorDrawDiagnostic = cursorDrawDiagnostic;
        this.cursorLatchGeneration = cursorLatchGeneration ?? (() => PadCursorDrawWitness.LatchWriteGeneration);
    }

    public string? CursorDrawDiagnostic => this.cursorDrawDiagnostic?.Invoke();

    public PadModeOwner Owner { get; } = new();

    public int PanicsExecuted { get; private set; }
    public bool RestoreWritePending => this.restoreRetry is not null;
    public string? CleanupFault { get; private set; }

    // Harmony __state is scoped to one synchronous OptionsDropDown constructor. It is
    // never a permission to correct a later tick/menu snapshot, even on the same binding.
    internal sealed record DropdownWitness(PadModeBinding Binding, PadModeSnapshot Before, long Tick, int Take);

    internal DropdownWitness? BeginDropdownInitialization(int whichOption)
    {
        if (whichOption != 38)
            return null;
        this.dropdownWitness = null;
        if (this.disposed || !this.Owner.Owned || this.CleanupFault is not null || this.RestoreWritePending
            || !this.CheckBinding() || this.game.ReadOptions() is not { } before || !this.CheckOwnedOptions(before))
            return null;
        return this.dropdownWitness = new(this.binding!, before, this.game.Tick, this.Owner.Takes);
    }

    internal void EndDropdownInitialization(DropdownWitness? witness)
    {
        if (witness is null || !ReferenceEquals(witness, this.dropdownWitness))
            return;
        this.dropdownWitness = null; // consume before any check/write; no retry or reuse
        if (this.disposed || !this.Owner.Owned || this.CleanupFault is not null || !this.CheckBinding())
            return;
        if (this.game.ReadOptions() is not { } current)
            return;
        bool exactReset = witness.Before == this.written && witness.Tick == this.game.Tick
            && witness.Take == this.Owner.Takes && witness.Binding.Matches(this.game.ReadBinding())
            && current.GamepadMode is PadModeSnapshot.Auto or "ForceOff"
            && current.GamepadControls && !current.SnappyMenus;
        if (!exactReset)
        {
            this.CheckOwnedOptions(current);
            return;
        }
        // The pinned constructor's case-38 assignment is the witness of the persisted
        // mode reload. Correct it NOW, before CheckGamepadMode recomputes controls.
        this.game.ReassertForceOn();
        this.game.Log($"PAD_MODE dropdown_init_reassert from={current.GamepadMode} tick={this.game.Tick}");
        if (this.game.ReadOptions() is { } after)
            this.CheckOwnedOptions(after);
    }

    /// <summary>Options.setGamepadMode is an owner write, never the dropdown initialization reload.</summary>
    internal void OnInteractiveModeWrite()
    {
        if (!this.disposed && (this.Owner.Owned || this.RestoreWritePending))
            this.PreserveOwnerChange();
    }

    /// <summary>Never throws into Harmony/vanilla, even if containment or diagnostics fail.</summary>
    internal void OnDropdownCallbackFault(string callback, Exception error)
    {
        // Revoke/latch using only local state BEFORE any fallible game/runtime call.
        // No restoration is safe after an incompletely witnessed constructor/write.
        this.RevokeRestoreAuthority();
        this.Owner.RelinquishForOwnerChange();
        this.CleanupFault ??= "dropdown_callback_exception:" + callback;
        this.oldPadNeutralDue = true;
        this.panicPending ??= "DROPDOWN_CALLBACK_EXCEPTION";
        try { this.runtime()?.PadPanic("DROPDOWN_CALLBACK_EXCEPTION"); }
        catch { /* Keep the latch and queued containment if synchronous suppression fails. */ }
        try
        {
            this.game.Log($"PAD_MODE cleanup_fault={this.CleanupFault} callback={callback} exception={error.GetType().Name}; new takes refused", warn: true);
        }
        catch { /* Diagnostics must never abort vanilla construction or the owner's write. */ }
    }

    /// <summary>
    /// Config refusal of the pad path (null = may be set up). The test-only gamepad probe is a second
    /// writer of gamepadMode / Press and a second SetMousePosition prefix, and the test-only reflex probe
    /// also presses pad buttons: never together with the pad path (review minor 6).
    /// </summary>
    public static string? ConfigRefusal(bool gamepadInput, bool backgroundMovement, bool backgroundToolUi, bool gamepadProbe,
        bool reflexProbe = false) =>
        !gamepadInput ? "flag_off"
        : !backgroundMovement || !backgroundToolUi ? "needs_background_movement_and_tool_ui"
        : gamepadProbe ? "gamepad_probe_enabled"
        : reflexProbe ? "reflex_probe_enabled"
        : null;

    // ---------------- IPadModeHost (the runtime's admission hooks)

    public bool PadInputEnabled => this.enabled() && this.warpGuardInstalled() && !this.disposed;

    public bool DrawPathOn => this.drawPath();

    public bool CursorSyncOwned => this.PadInputEnabled && this.DrawPathOn && this.Owner.Owned
        && this.CleanupFault is null && !this.RestoreWritePending && !this.saveWindow
        && this.game.WorldReady && !this.game.StopLatched && !this.game.SaveMenuOpen // R5 S3: no focus term (ADR 244)
        && this.panicPending is null && this.binding?.Matches(this.game.ReadBinding()) == true
        && this.game.ReadOptions() == this.written;

    public bool TryReconcileCursor()
    {
        if (!this.CursorSyncOwned || this.cursorLatchGeneration() is not long generation
            || !Game1.wasMouseVisibleThisFrame || this.binding is not { } current)
            return false;
        // A later native write supersedes an earlier override, even if its value was false.
        if (this.cursorOverride is not { } prior || prior.Generation != generation)
            this.cursorOverride = new(current, generation, Game1.wasMouseVisibleThisFrame);
        Game1.wasMouseVisibleThisFrame = false;
        return true;
    }

    private void ReleaseCursorOverride()
    {
        CursorOverride? prior = this.cursorOverride;
        this.cursorOverride = null; // one cleanup; never revive options restoration authority
        if (prior is null)
            return;
        try
        {
            if (prior.Binding.Matches(this.game.ReadBinding())
                && this.cursorLatchGeneration() == prior.Generation && !Game1.wasMouseVisibleThisFrame)
                Game1.wasMouseVisibleThisFrame = prior.Before;
        }
        catch { /* An unreadable/replaced binding cannot authorize a stale routing write. */ }
    }

    private void ReleaseCursorIfUnauthorized()
    {
        bool authorized;
        try { authorized = this.CursorSyncOwned; }
        catch { authorized = false; }
        if (!authorized)
            this.ReleaseCursorOverride();
    }

    /// <summary>PLAN 1.5 a take-over (admission of a pad request while inactive). Never without the pin.</summary>
    public string? TakeForAdmission(out bool justTook)
    {
        justTook = false;
        if (this.disposed)
            return BgHoldReason.PadModeUnavailable + ":disposed";
        if (this.CleanupFault is not null)
            return BgHoldReason.PadModeUnavailable + ":cleanup_fault:" + this.CleanupFault;
        if (this.RestoreWritePending)
            return BgHoldReason.PadModeUnavailable + ":restore_write_pending";
        if (this.game.ReadBinding() is not { } currentBinding)
            return BgHoldReason.PadModeUnavailable + ":not_farmhand";
        if ((this.Owner.Owned || this.Owner.PinActive && this.binding is not null) && !this.CheckBinding())
            return BgHoldReason.PadModeUnavailable + ":cleanup_fault:" + this.CleanupFault;
        // D-B1b-1: never take (or re-assert) while the night save / shipping menu runs; the next
        // admission after Saved / DayStarted takes again (normal path).
        if (this.saveWindow || this.game.SaveMenuOpen)
            return BgHoldReason.PadModeUnavailable + ":saving";
        if (this.game.ResolvePin() is string pinFailure)
            return BgHoldReason.PadModeUnavailable + ":" + pinFailure;
        if (this.game.ReadOptions() is not { } current)
            return BgHoldReason.PadModeUnavailable + ":no_options";
        if (!this.CheckOwnedOptions(current))
            return BgHoldReason.PadModeUnavailable + ":cleanup_fault:" + this.CleanupFault;
        PadModeTake take = this.Owner.Take(current);
        if (take.WriteForceOn)
        {
            this.binding = currentBinding;
            this.written = new PadModeSnapshot(PadModeSnapshot.ForceOn, true, false);
            this.unownedModeReported = false;
            this.game.TakeForceOn();
            justTook = true;
            this.game.Log(take.Log + " tick=" + this.game.Tick);
            return null;
        }
        if (!string.Equals(current.GamepadMode, PadModeSnapshot.ForceOn, StringComparison.Ordinal) || !current.GamepadControls)
        {
            this.game.ReassertForceOn();
            this.game.Log($"PAD_MODE reasserted ForceOn tick={this.game.Tick}", warn: true);
        }
        return null;
    }

    // ---------------- requests from outside UpdateTicking

    /// <summary>Pad panic is executed at the next UpdateTicking (after the ledger tick start).</summary>
    public void RequestPanic(string reason)
    {
        this.panicPending ??= reason;
        this.game.Log($"pad panic requested reason={reason} (executed at the next UpdateTicking)", warn: true);
    }

    /// <summary>The pad_panic.json control file was read (and deleted) by the caller.</summary>
    public void OnPanicFile(string? content)
    {
        string requestId = "?";
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(content ?? "");
            if (doc.RootElement.TryGetProperty("request_id", out var id) && id.ValueKind == System.Text.Json.JsonValueKind.String)
                requestId = id.GetString() ?? "?";
        }
        catch
        {
            // Malformed content still panics: the file's presence is the request.
        }
        this.RequestPanic("file request_id=" + requestId);
    }

    /// <summary>ReturnedToTitle / disconnect: Suppress outstanding now, restore at the next UpdateTicking.</summary>
    public void LifecycleExit(string reason)
    {
        this.ReleaseCursorOverride();
        try
        {
            this.runtime()?.SuppressOutstandingPad(reason);
        }
        catch (Exception ex)
        {
            this.game.Log($"PAD lifecycle suppress fault: {ex.GetType().Name}", warn: true);
        }
        this.Owner.RequestExit(PadModeExit.ReturnedToTitle);
        this.oldPadNeutralDue = true;
    }

    /// <summary>Mod dispose: give the options back now (no tick can pin afterwards).</summary>
    public void Dispose()
    {
        if (this.disposed)
            return;
        this.disposed = true;
        this.ReleaseCursorOverride();
        try
        {
            if (this.Owner.DisposeRestore() is { } restore)
                this.BeginRestore(restore);
            // No future tick exists. Drain even when an earlier restore cleared ownership,
            // or its bounded runtime retry budget latched a fault. Identity/value checks still apply.
            for (int i = 0; i < DisposeAttemptLimit && this.RestoreWritePending; i++)
                if (!this.TryRestore(teardown: true))
                    break;
            if (this.RestoreWritePending)
            {
                this.LatchFault("dispose_restore_unresolved");
                this.game.Log($"PAD_MODE dispose_restore_unresolved fault={this.CleanupFault}", warn: true);
            }
        }
        catch (Exception ex)
        {
            this.LatchFault("dispose_exception:" + ex.GetType().Name);
        }
    }

    /// <summary>True between DayEnding / Saving and Saved / DayStarted (pad admissions refused).</summary>
    public bool SaveWindow => this.saveWindow;

    /// <summary>
    /// Live D-B1b-1: SMAPI save lifecycle (DayEnding, Saving, Saved, DayStarted), raised on the game thread
    /// while NO UpdateTicking runs. DayEnding / Saving open the save window and give the mode back NOW
    /// (options, pin, Suppress outstanding) so SaveGameMenu.saveClientOptions writes the originals into
    /// startup_preferences. Saved repeats the check (belt and braces) and closes the window; DayStarted
    /// closes it too. Never throws.
    /// </summary>
    public void OnSaveEvent(string evt)
    {
        this.ReleaseCursorOverride();
        bool closes = evt is "Saved" or "DayStarted";
        try
        {
            if (!closes)
                this.saveWindow = true;
            if (this.runtime() is { } rt && !this.disposed)
            {
                bool active;
                try { active = this.game.Active; } catch { active = false; }
                PadModeTickPlan plan = this.Owner.ExitNow(PadModeExit.SaveMenu, this.Facts(rt, active));
                if (!ReferenceEquals(plan, PadModeTickPlan.Nothing))
                {
                    this.Apply(plan, rt);
                    this.game.Log($"PAD_MODE save_restore evt={evt} tick={this.game.Tick}", warn: true);
                }
                this.SaveFallback();
            }
        }
        catch (Exception ex)
        {
            this.game.Log($"PAD_MODE save hook fault evt={evt}: {ex.GetType().Name}: {ex.Message}", warn: true);
        }
        finally
        {
            if (closes)
                this.saveWindow = false;
        }
    }

    /// <summary>Save hooks may retry only a still-pending, identity-pinned restoration.</summary>
    private void SaveFallback()
    {
        this.RetryRestore();
        this.ReportUnownedMode();
    }

    private PadModeTickFacts Facts(BackgroundMoveRuntime rt, bool active) => new(
        Active: active,
        WorldReady: this.game.WorldReady,
        SaveMenuOpen: this.saveWindow || this.game.SaveMenuOpen,
        // WS-G5 review minor 2: the STOP mode exit waits with the STOP panic while a charge press releases (bounded).
        StopLatched: this.game.StopLatched && !this.stopDeferredThisTick,
        OverridesEmpty: rt.PadLedger.Empty,
        OutstandingPresses: rt.PadLedger.Outstanding.Count,
        ConnectedExpected: this.game.PadConnected || rt.PadLedger.AnyIssuedThisTick,
        GamepadMode: this.ReadModeOrNull());

    private string? ReadModeOrNull()
    {
        try
        {
            return this.game.ReadOptions()?.GamepadMode;
        }
        catch
        {
            return null; // unknown: no re-assert decision this tick
        }
    }

    // ---------------- the per-tick sequence

    /// <summary>
    /// End of every UpdateTicking (after every ARTI Press/Suppress of the tick): STOP / fault / panic,
    /// menu-exit hygiene, then the PadModeOwner tick. Applied in safety order (review minor 2): the
    /// options restore first, then the pin, then the Suppress calls, each in its own try; plus the
    /// restore fallback (focused window or an idle retired lease).
    /// </summary>
    public void Tick()
    {
        try { this.TickCore(); }
        finally { this.ReleaseCursorIfUnauthorized(); }
    }

    private void TickCore()
    {
        if (this.runtime() is not { } rt || this.disposed)
            return;
        bool active;
        try
        {
            active = this.game.Active;
        }
        catch
        {
            active = false;
        }
        try
        {
            // PLAN 1.5 e: STOP runs pad panic once per latch (the graceful request release continues).
            if (this.game.StopLatched)
            {
                if (!this.stopSeen && (this.Owner.Owned || rt.PadRequestActive || !rt.PadLedger.Empty))
                    this.panicPending ??= "STOP";
                this.stopSeen = true;
            }
            else
            {
                this.stopSeen = false;
            }
            if (rt.PadPanicRequest is string fault)
            {
                rt.PadPanicRequest = null;
                this.panicPending ??= fault;
            }
            bool chargeInFlight = rt.ChargePressOutstanding;
            bool chargeDefer = this.panicPending == "STOP" && chargeInFlight && this.chargeStopDeferTicks < ChargeStopDeferMaxTicks;
            bool requestDefer = this.panicPending == "STOP" && !chargeInFlight && rt.PadRequestActive
                && this.requestStopDeferTicks < RequestStopDeferMaxTicks;
            this.stopDeferredThisTick = chargeDefer || requestDefer;
            if (chargeDefer)
            {
                // The STOP latch already makes the request release (lease invalid); only the mode exit / panic Suppress waits.
                if (this.chargeStopDeferTicks++ == 0)
                    this.game.Log("PAD_PANIC STOP deferred: reflex charge press releasing through its R frame", warn: true);
            }
            else if (requestDefer)
            {
                if (this.requestStopDeferTicks++ == 0)
                    this.game.Log("PAD_PANIC STOP deferred: pad request releasing through its R/N frames in gamepad mode", warn: true);
            }
            else if (this.panicPending is string panic)
            {
                if (panic == "STOP" && this.requestStopDeferTicks >= RequestStopDeferMaxTicks && rt.PadRequestActive)
                    this.game.Log($"PAD_PANIC STOP defer bound reached ({RequestStopDeferMaxTicks} ticks): panic with the request in flight", warn: true);
                this.chargeStopDeferTicks = 0;
                this.requestStopDeferTicks = 0;
                this.panicPending = null;
                this.ExecutePanic(rt, panic);
            }

            // PLAN 1.5 d: a neutral oldPadState on every observed menu exit while inactive.
            bool menuOpen = this.game.WorldReady && this.game.MenuOpen;
            if (this.prevMenuOpen && !menuOpen && !active)
                this.oldPadNeutralDue = true;
            this.prevMenuOpen = menuOpen;
        }
        catch (Exception ex)
        {
            this.game.Log($"PAD panic/hygiene fault: {ex.GetType().Name}: {ex.Message}", warn: true);
        }

        PadModeTickPlan plan;
        try
        {
            // Check all owned values before the state machine can request a reassertion
            // or retire ownership for restoration. Mode alone misses control-only changes.
            if (this.Owner.Owned && this.CheckBinding() && this.game.ReadOptions() is { } current)
                this.CheckOwnedOptions(current);
            // FIXB46: inactive ownership spans requests within a lease, not a retired
            // terminal lease. Use the same pinned restore as panic/save; never while
            // a native request or an unsuppressed Press can still reach the consumer.
            if (this.Owner.Owned && this.game.LeaseReleased && !rt.Busy
                && !rt.PadRequestActive && rt.PadLedger.Outstanding.Count == 0)
                this.Owner.RequestExit(PadModeExit.LeaseReleased);
            plan = this.Owner.Tick(this.Facts(rt, active));
        }
        catch (Exception ex)
        {
            this.game.Log($"PAD mode tick fault: {ex}", warn: true);
            this.panicPending ??= "PAD_TICK_EXCEPTION";
            plan = PadModeTickPlan.Nothing;
        }
        this.Apply(plan, rt);
        this.RetryAtIdle(rt);

        // The neutral write never lands inside a request (it would break the VERIFY history invariant).
        try
        {
            // WS-G5 review minor 2: never while a charge press is outstanding (its X release edge needs X in oldPadState).
            if (this.oldPadNeutralDue && !rt.Busy && !rt.ChargePressOutstanding)
            {
                if (this.game.ReadBinding() is not null && (this.binding is null ? this.CleanupFault is null : this.CheckBinding()))
                {
                    this.oldPadNeutralDue = false;
                    PadHistoryHygiene.WriteNeutral();
                }
            }
        }
        catch (Exception ex)
        {
            this.game.Log($"PAD oldPadState hygiene fault: {ex.GetType().Name}", warn: true);
        }
    }

    /// <summary>
    /// PLAN 1.5 e. Review major 1: the 16 Suppress calls create SMAPI overrides (a "connected" virtual
    /// pad while inactive); without ownership nothing would pin _oldGamepadConnectedState, so Auto would
    /// report a connection now and a disconnect + pause GameMenu on focus. So: with nothing owned, no
    /// ledger entry and no pad request in flight there is nothing to release and no Suppress is issued;
    /// if any override is issued while not owned, the pin starts.
    /// </summary>
    private void ExecutePanic(BackgroundMoveRuntime rt, string reason)
    {
        this.ReleaseCursorOverride();
        bool full = this.Owner.Owned || !rt.PadLedger.Empty || rt.PadRequestActive;
        IReadOnlyList<string> issued = rt.PadPanic(reason, suppressAll: full);
        this.PanicsExecuted++;
        if (this.Owner.Owned)
            this.Owner.RequestExit(PadModeExit.PadPanic);
        else if (issued.Count > 0)
            this.Owner.StartPin("PadPanic");
        this.oldPadNeutralDue = true;
    }

    private void Apply(PadModeTickPlan plan, BackgroundMoveRuntime rt)
    {
        if (plan.Restore is { } restore)
        {
            this.BeginRestore(restore);
            this.RetryRestore();
        }
        if (plan.ReassertForceOn && this.Owner.Owned && this.CleanupFault is null && this.CheckBinding())
        {
            try
            {
                // A plan is not evidence of who changed the options. Re-read immediately
                // before writing; constructor witnesses are consumed synchronously, never here.
                if (this.game.ReadOptions() is { } current && this.CheckOwnedOptions(current))
                    this.game.ReassertForceOn();
            }
            catch (Exception ex)
            {
                this.game.Log($"PAD_MODE reassert write fault: {ex.GetType().Name}", warn: true);
            }
        }
        if (plan.PinValue is bool pin && this.game.ReadBinding() is not null
            && (this.binding is null ? this.CleanupFault is null : this.CheckBinding()))
        {
            try
            {
                this.game.WritePin(pin);
            }
            catch (Exception ex)
            {
                this.game.Log($"PAD_MODE pin write fault: {ex.GetType().Name}", warn: true);
            }
        }
        if (plan.SuppressOutstanding)
        {
            try
            {
                IReadOnlyList<string> done = rt.SuppressOutstandingPad("PAD_MODE");
                this.game.Log($"PAD_MODE suppress outstanding=[{string.Join(",", done)}] tick={this.game.Tick}");
            }
            catch (Exception ex)
            {
                this.game.Log($"PAD_MODE suppress fault: {ex.GetType().Name}", warn: true);
            }
        }
        if (plan.NeutralOldPad)
            this.oldPadNeutralDue = true;
        if (plan.Log is string log)
            this.game.Log(log + " tick=" + this.game.Tick);
    }

    /// <summary>Retry a pending restore only at an idle boundary, never from historical Takes.</summary>
    private void RetryAtIdle(BackgroundMoveRuntime rt)
    {
        if (!rt.PadLedger.AnyIssuedThisTick && !rt.Busy && !rt.PadRequestActive && rt.PadLedger.Outstanding.Count == 0)
            this.RetryRestore();
        this.ReportUnownedMode();
    }

    private void BeginRestore(PadModeSnapshot restore)
    {
        this.ReleaseCursorOverride();
        if (this.RestoreWritePending)
            return;
        this.restoreRetry = restore;
        this.restoreAttempts = 0;
        this.restoreStartedTick = this.nextRestoreTick = this.game.Tick;
    }

    private void LatchFault(string reason)
    {
        this.ReleaseCursorOverride();
        if (this.CleanupFault is not null)
            return;
        this.CleanupFault = reason;
        this.game.Log($"PAD_MODE cleanup_fault={reason} attempts={this.restoreAttempts} tick={this.game.Tick}; new takes refused", warn: true);
    }

    private bool CheckBinding()
    {
        if (this.binding?.Matches(this.game.ReadBinding()) == true)
            return true;
        this.RevokeRestoreAuthority();
        this.Owner.RelinquishForOwnerChange();
        this.LatchFault("farmhand_session_options_changed");
        return false;
    }

    /// <summary>
    /// Only the synchronous constructor boundary can witness a reset. Every divergence
    /// observed elsewhere belongs to the owner and must be preserved.
    /// </summary>
    private bool CheckOwnedOptions(PadModeSnapshot current)
    {
        this.dropdownWitness = null; // any independent divergence check ends the constructor scope
        if (!this.Owner.Owned || current == this.written)
            return true;
        this.PreserveOwnerChange();
        return false;
    }

    private void RevokeRestoreAuthority()
    {
        this.ReleaseCursorOverride();
        this.restoreRetry = null;
        this.dropdownWitness = null;
    }

    private void PreserveOwnerChange()
    {
        this.RevokeRestoreAuthority();
        this.Owner.RelinquishForOwnerChange();
        this.LatchFault("owner_options_changed_preserved");
        this.oldPadNeutralDue = true;
        try
        {
            // Retire native requests/presses too, without creating options restoration
            // authority. The relinquished owner's connection pin contains these Suppresses.
            this.runtime()?.PadPanic("OWNER_OPTIONS_CHANGED");
        }
        catch (Exception ex)
        {
            this.game.Log($"PAD_MODE owner-change suppress fault: {ex.GetType().Name}", warn: true);
        }
    }

    private void RetryRestore()
    {
        if (!this.RestoreWritePending || this.CleanupFault is not null)
            return;
        if (this.restoreAttempts >= RestoreAttemptLimit || this.game.Tick - this.restoreStartedTick >= RestoreRetryTickLimit)
        {
            this.LatchFault("restore_retry_exhausted");
            return;
        }
        if (this.game.Tick >= this.nextRestoreTick)
            this.TryRestore(teardown: false);
    }

    // false means further teardown attempts cannot safely write (identity or owner change).
    private bool TryRestore(bool teardown)
    {
        this.restoreAttempts++;
        this.nextRestoreTick = this.game.Tick + RestoreBackoffTicks * (1L << Math.Min(this.restoreAttempts - 1, 3));
        try
        {
            if (!this.CheckBinding())
                return false;
            PadModeSnapshot restore = this.restoreRetry!.Value;
            PadModeSnapshot? now = this.game.ReadOptions();
            if (now == restore)
            {
                this.CompleteRestore();
                return true;
            }
            if (now is { } current && current != this.written)
            {
                this.PreserveOwnerChange();
                return false;
            }
            if (now is null)
                throw new InvalidOperationException("options unreadable");
            this.game.WriteOptions(restore);
            if (!this.CheckBinding())
                return false;
            if (this.game.ReadOptions() == restore)
            {
                this.CompleteRestore();
                return true;
            }
            this.game.Log($"PAD_MODE restore unverified attempt={this.restoreAttempts} teardown={teardown}", warn: true);
        }
        catch (Exception ex)
        {
            this.game.Log($"PAD_MODE restore write fault: {ex.GetType().Name} attempt={this.restoreAttempts} teardown={teardown}", warn: true);
        }
        if (!teardown && this.restoreAttempts >= RestoreAttemptLimit)
            this.LatchFault("restore_retry_exhausted");
        return true;
    }

    private void CompleteRestore()
    {
        this.game.Log($"PAD_MODE restore_verified attempts={this.restoreAttempts} tick={this.game.Tick}");
        this.restoreRetry = null;
        // Keep the identity while the connection pin remains live, but retire write authority.
        this.CleanupFault = null;
    }

    private void ReportUnownedMode()
    {
        if (this.Owner.Owned || this.RestoreWritePending || this.Owner.Takes == 0 || this.unownedModeReported)
            return;
        try
        {
            if (this.game.ReadOptions()?.GamepadMode == PadModeSnapshot.ForceOn)
            {
                this.unownedModeReported = true;
                this.game.Log("PAD_MODE unowned ForceOn preserved: no pending ARTI restoration", warn: true);
            }
        }
        catch { /* Read-only diagnostic; never creates restoration authority. */ }
    }
}
