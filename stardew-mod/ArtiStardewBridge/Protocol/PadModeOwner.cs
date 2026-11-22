namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G0, PLAN section 1.5 a/b: who owns Options.gamepadMode while the ARTI farmhand
// is driven as a virtual gamepad. Pure state machine: no game/SMAPI types. The adapter
// (ModEntry.Pad.cs) reads/writes Game1.options, Game1.timerUntilMouseFade and the protected
// Game1._oldGamepadConnectedState (reflection, MVID-pinned) exactly as the returned plans say.
//
// Decomp facts (Stardew 1.6.15, G1 = StardewValley/Game1.cs):
//   - CheckGamepadMode (G1:3584-3637, every _update): ForceOn -> gamepadControls = true and return
//     early (G1:3587-3591). Auto: when GetGamePadState().IsConnected differs from
//     _oldGamepadConnectedState (G1:1091) it records the new value and, on true -> false, shows
//     "controller disconnected" and opens a pause GameMenu (G1:3612-3630).
//   - Any applied SMAPI controller override (Press OR Suppress) builds a connected pad (probe Part 1
//     section 4); while inactive SMAPI re-applies every override each tick (probe Part 1 section 2).
//   - SaveGameMenu writes Game1.options into startup_preferences clientOptions after the host saved
//     (SaveGameMenu.cs:140, 157-162), reloaded at the next join (FarmhandMenu.cs:302).
//   - Setting ForceOn directly skips the transition block that zeroes timerUntilMouseFade
//     (G1:3641-3655): the take-over writes it once.

/// <summary>The three Options values ARTI owns while the pad mode is owned (restored together).</summary>
public readonly record struct PadModeSnapshot(string GamepadMode, bool GamepadControls, bool SnappyMenus)
{
    public const string ForceOn = "ForceOn";
    public const string Auto = "Auto";
}

/// <summary>Why the pad mode is being given back.</summary>
public enum PadModeExit
{
    None,
    /// <summary>The farmhand window became active (PLAN section 1.5 a steps 1-3).</summary>
    FocusRegain,
    /// <summary>A SaveGameMenu is open: the night save must never capture ForceOn.</summary>
    SaveMenu,
    ReturnedToTitle,
    WorldNotReady,
    /// <summary>
    /// STOP latched. Ownership survives individual requests within a live action lease.
    /// FIXB46 adds a separate exit once that lease retires and all native input is idle.
    /// </summary>
    Stop,
    PadPanic,
    Dispose,
    /// <summary>Terminal action lease retired, no native input or outstanding Press remains.</summary>
    LeaseReleased,
    /// <summary>An unwitnessed change to ARTI-owned options: retire without restoring them.</summary>
    OwnerOptionsChanged,
}

/// <summary>Facts the adapter samples at the END of every UpdateTicking (after every ARTI Press/Suppress of the tick).</summary>
public readonly record struct PadModeTickFacts(
    bool Active,
    bool WorldReady,
    bool SaveMenuOpen,
    /// <summary>The ARTI STOP latch (an exit); the lease ending between requests is not.</summary>
    bool StopLatched,
    /// <summary>PadOverrideLedger.Empty.</summary>
    bool OverridesEmpty,
    /// <summary>PadOverrideLedger.Outstanding.Count: ARTI pad presses not yet suppressed.</summary>
    int OutstandingPresses,
    /// <summary>
    /// The IsConnected value CheckGamepadMode will read this tick: GetGamePadState().IsConnected now
    /// OR an ARTI controller override issued this tick (applied by this tick's ApplyOverrides).
    /// </summary>
    bool ConnectedExpected,
    /// <summary>Live Options.gamepadMode (enum name), or null when unknown (no re-assert decision then).</summary>
    string? GamepadMode = null);

/// <summary>What the adapter must do this tick, in order: suppress, neutral oldPadState, restore, pin.</summary>
public sealed record PadModeTickPlan
{
    public static readonly PadModeTickPlan Nothing = new();

    /// <summary>Step 1: Suppress every outstanding ARTI pad press through HelperPressPort.</summary>
    public bool SuppressOutstanding { get; init; }
    /// <summary>Step 1 / lifecycle: write a neutral Game1.oldPadState (PLAN section 1.5 d).</summary>
    public bool NeutralOldPad { get; init; }
    /// <summary>Restore <see cref="PadModeOwner.Originals"/> into Game1.options.</summary>
    public PadModeSnapshot? Restore { get; init; }
    /// <summary>Write Game1._oldGamepadConnectedState := this value (the value CheckGamepadMode reads this tick).</summary>
    public bool? PinValue { get; init; }
    /// <summary>
    /// Live F-B1c-1 (v2 batch 1c): owned, inactive, no exit, but the live gamepadMode is not ForceOn (opening the
    /// GameMenu builds an OptionsPage whose dropdown reload assigns startup_preferences' gamepadMode,
    /// Options.cs:2006-2011 via OptionsDropDown.cs:37). This is only a candidate:
    /// the adapter must prove the writer before reasserting; an unexplained change
    /// relinquishes ownership and preserves all owner values instead.
    /// </summary>
    public bool ReassertForceOn { get; init; }
    /// <summary>Log line (PAD_MODE ...), or null.</summary>
    public string? Log { get; init; }
}

/// <summary>Result of <see cref="PadModeOwner.Take"/>: what the admission must write.</summary>
public readonly record struct PadModeTake(bool JustTook, PadModeSnapshot? Originals, string? Log)
{
    /// <summary>Write gamepadMode = ForceOn, gamepadControls = true, snappyMenus = false, timerUntilMouseFade = 0 (once).</summary>
    public bool WriteForceOn => this.JustTook;
}

/// <summary>
/// PLAN section 1.5 a; ADR 244. Ownership spans the whole session: taken at the first admitted pad
/// request, kept whatever the window focus does (ADR 244: tools aim by facing in front too), until a
/// lifecycle exit or FIXB46 terminal action-lease retirement (adapter confirms all native input is idle).
/// While owned with the window active, the <c>_oldGamepadConnectedState</c> pin is written every tick:
/// SMAPI's active TrueUpdate drops ARTI's virtual pad between presses, and the pin keeps the game from
/// reading that as a pad (dis)connection. There is no focus-regain restore any more.
/// Never restores a non-ForceOn mode while an ARTI controller override can still be applied,
/// except together with the <c>_oldGamepadConnectedState</c> pin (which then holds the value the
/// game reads, so CheckGamepadMode sees no connection change). Every restore pins.
/// Pin lifetime: from that restore (or from the first owned active tick), across a re-take, until the
/// first ACTIVE tick, not owned, on which the game reads IsConnected == false (that tick still writes
/// false, then the pin ends).
/// </summary>
public sealed class PadModeOwner
{
    public const int RestoreTickBudget = 10;

    private PadModeExit pendingExit = PadModeExit.None;

    /// <summary>ARTI set ForceOn and has not given it back.</summary>
    public bool Owned { get; private set; }
    /// <summary>
    /// Kept for session.json compatibility (<c>pad_mode_restore_pending</c>): always false since ADR 244 removed
    /// the bounded focus-regain restore (focus is no exit).
    /// </summary>
    public bool RestorePending { get; private set; }
    /// <summary>The _oldGamepadConnectedState pin is written every UpdateTicking.</summary>
    public bool PinActive { get; private set; }
    public int RestoreTicks { get; private set; }
    public PadModeSnapshot? Originals { get; private set; }
    public bool? LastPinValue { get; private set; }
    public PadModeExit LastExit { get; private set; }
    public int Takes { get; private set; }
    public int Restores { get; private set; }
    /// <summary>F-B1c-1 re-asserts of ForceOn while owned (diagnostics).</summary>
    public int Reasserts { get; private set; }

    /// <summary>
    /// Admission of a pad request (inactive farmhand, every other admission term already true).
    /// <paramref name="current"/> = the live Options values. Idempotent while owned.
    /// </summary>
    public PadModeTake Take(PadModeSnapshot current)
    {
        if (this.Owned)
            return new PadModeTake(false, this.Originals, null);
        // A ForceOn found at take time is never adopted as "original": that would let a crash-left
        // ForceOn survive every restore. Auto is the game default (Options.cs:701).
        // Review major 1: with Auto the game recomputes gamepadControls itself; the restore writes false
        // (a transient true captured at take time would keep the pad UI on the owner's window).
        PadModeSnapshot originals = string.Equals(current.GamepadMode, PadModeSnapshot.ForceOn, StringComparison.Ordinal)
            ? current with { GamepadMode = PadModeSnapshot.Auto, GamepadControls = false }
            : string.Equals(current.GamepadMode, PadModeSnapshot.Auto, StringComparison.Ordinal)
                ? current with { GamepadControls = false }
                : current;
        this.Originals = originals;
        this.Owned = true;
        this.RestorePending = false;
        this.RestoreTicks = 0;
        this.pendingExit = PadModeExit.None;
        this.Takes++;
        return new PadModeTake(true, originals,
            $"PAD_MODE set ForceOn orig_mode={originals.GamepadMode} found_mode={current.GamepadMode} "
                + $"orig_controls={originals.GamepadControls} orig_snappy={originals.SnappyMenus} pin_active={this.PinActive}");
    }

    /// <summary>
    /// Review major 1: ARTI issued controller overrides while NOT owning the mode (pad panic with an
    /// Auto mode): pin _oldGamepadConnectedState from the next tick on (same lifetime rule as a restore).
    /// </summary>
    public void StartPin(string reason)
    {
        if (this.Owned || this.PinActive)
            return;
        this.PinActive = true;
        this.PinStartReason = reason;
    }

    /// <summary>Why the last pin started outside a restore (diagnostics).</summary>
    public string? PinStartReason { get; private set; }

    /// <summary>
    /// Lifecycle exit requested outside UpdateTicking (STOP, pad panic, ReturnedToTitle): applied by
    /// the next <see cref="Tick"/>. The first non-None request wins until applied.
    /// </summary>
    public void RequestExit(PadModeExit reason)
    {
        if (reason != PadModeExit.None && this.pendingExit == PadModeExit.None)
            this.pendingExit = reason;
    }

    /// <summary>Give up a take after an owner change; never return an originals restore plan.</summary>
    public void RelinquishForOwnerChange()
    {
        if (!this.Owned)
            return;
        this.Owned = false;
        this.RestorePending = false;
        this.RestoreTicks = 0;
        this.pendingExit = PadModeExit.None;
        this.Originals = null; // irrevocably retire options write authority, including Dispose.
        this.LastExit = PadModeExit.OwnerOptionsChanged;
        this.PinActive = true;
    }

    /// <summary>Once per UpdateTicking, after every ARTI Press/Suppress of the tick.</summary>
    public PadModeTickPlan Tick(PadModeTickFacts f)
    {
        PadModeTickPlan plan = PadModeTickPlan.Nothing;
        bool connectedExpected = f.ConnectedExpected;
        if (this.Owned)
        {
            PadModeExit exit = this.pendingExit != PadModeExit.None ? this.pendingExit
                : !f.WorldReady ? PadModeExit.WorldNotReady
                : f.SaveMenuOpen ? PadModeExit.SaveMenu
                : f.StopLatched ? PadModeExit.Stop
                : PadModeExit.None;
            if (exit != PadModeExit.None)
            {
                // Pinned restore = the one exit path, window active or not (ADR 244; also during a save).
                plan = this.RestorePinned(exit, f);
                connectedExpected |= f.OutstandingPresses > 0; // this plan Suppresses them: applied this tick
            }
            else
            {
                // ADR 244: an active window is no exit. Owned + active pins every tick (see the class summary).
                if (f.Active)
                    this.PinActive = true;
                if (f.GamepadMode is string mode && !string.Equals(mode, PadModeSnapshot.ForceOn, StringComparison.Ordinal))
                {
                    // Candidate only: the adapter must distinguish a witnessed game write
                    // from an owner change before applying any options write.
                    this.Reasserts++;
                    string line = $"PAD_MODE reassert found_mode={mode} reasserts={this.Reasserts}";
                    plan = plan with { ReassertForceOn = true, Log = plan.Log is null ? line : plan.Log + "; " + line };
                }
            }
        }
        else
        {
            this.pendingExit = PadModeExit.None;
        }

        if (this.PinActive)
        {
            bool value = connectedExpected;
            string? pinLog = null;
            if (f.Active && !value && !this.Owned)
            {
                // First active tick (not owned any more) on which the game reads a disconnected pad: write
                // false once more (the previous pin may have been true), then the pin ends.
                this.PinActive = false;
                pinLog = "PAD_MODE pin_end";
            }
            this.LastPinValue = value;
            plan = plan with
            {
                PinValue = value,
                Log = plan.Log is null ? pinLog : pinLog is null ? plan.Log : plan.Log + "; " + pinLog,
            };
        }
        return plan;
    }

    /// <summary>
    /// Live D-B1b-1 (batch 1b, 2026-10-08): SMAPI raises no UpdateTicking while the night save runs, and
    /// SaveGameMenu.saveClientOptions persisted the owned ForceOn into startup_preferences. The save hooks
    /// (DayEnding / Saving / Saved) therefore restore SYNCHRONOUSLY through this call: the same pinned
    /// restore as a Tick exit (options, then the pin value the game will read, then Suppress outstanding),
    /// without waiting for a tick. Not owned: nothing to do.
    /// </summary>
    public PadModeTickPlan ExitNow(PadModeExit exit, PadModeTickFacts f)
    {
        if (!this.Owned)
            return PadModeTickPlan.Nothing;
        PadModeTickPlan plan = this.RestorePinned(exit, f);
        bool value = f.ConnectedExpected || f.OutstandingPresses > 0;
        this.LastPinValue = value;
        return plan with { PinValue = value };
    }

    private PadModeTickPlan RestorePinned(PadModeExit exit, PadModeTickFacts f)
    {
        PadModeSnapshot originals = this.Originals ?? new PadModeSnapshot(PadModeSnapshot.Auto, false, true);
        this.Owned = false;
        this.RestorePending = false;
        this.RestoreTicks = 0;
        this.PinActive = true;
        this.pendingExit = PadModeExit.None;
        this.LastExit = exit;
        this.Restores++;
        return new PadModeTickPlan
        {
            SuppressOutstanding = f.OutstandingPresses > 0,
            NeutralOldPad = true,
            Restore = originals,
            Log = $"PAD_MODE restore_pinned reason={exit} active={f.Active} outstanding={f.OutstandingPresses} overrides_empty={f.OverridesEmpty}",
        };
    }

    /// <summary>Mod dispose / process teardown: the options go back now; no further tick can pin.</summary>
    public PadModeSnapshot? DisposeRestore()
    {
        if (!this.Owned)
            return null;
        this.Owned = false;
        this.RestorePending = false;
        this.LastExit = PadModeExit.Dispose;
        this.Restores++;
        return this.Originals ?? new PadModeSnapshot(PadModeSnapshot.Auto, false, true);
    }
}
