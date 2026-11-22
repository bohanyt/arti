using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Tools;

namespace ArtiStardewBridge;

// Primitives v2 WS-G4: the reflex press entry of the PRODUCTION background runtime. A reflex press is a G0 pad X
// TOOL hold of the in-flight reflex_fish request (same lease, N-R / N-H / latch / N0 / POST_N0_CANONICALIZE,
// containment armed before the single Press, VERIFY exact frame, Suppress in the UpdateTicking finally, release
// proof). Differences, all in BgReflexPress (pure, tested): tick cap (cast <= 150, tap 1), the rod-in-use gate
// terms of a tap, the settle rule (the rod stays in use) and the no-callback release proof under a menu the press
// opened. Compiled by the HoldRuntimeTests harness too. Evidence ceiling: UNIT_TESTED.

/// <summary>Reflex press helpers shared by the runtime partials (game reads only).</summary>
internal static class BgReflexRuntime
{
    public const string ReasonPadPathOff = "REFLEX_PAD_PATH_OFF";
    public const string ReasonRodNotHeld = "REFLEX_ROD_NOT_HELD";
    public const string ReasonWeaponNotHeld = "REFLEX_WEAPON_NOT_HELD";        // WS-G5 swing
    public const string ReasonToolNotChargeable = "REFLEX_TOOL_NOT_CHARGEABLE"; // WS-G5 charge

    /// <summary>The press kind belongs to this reflex request (fish: cast / tap, combat: swing, charge: charge).</summary>
    public static bool RequestMatches(BgReflexPressKind kind, in MenuStepCommandRequest req) => kind switch
    {
        BgReflexPressKind.Cast or BgReflexPressKind.Tap => MenuStepCommandInbox.IsReflexFish(req),
        BgReflexPressKind.Swing => MenuStepCommandInbox.IsReflexCombat(req),
        BgReflexPressKind.Charge => MenuStepCommandInbox.IsReflexCharge(req),
        _ => false,
    };

    /// <summary>null = the press kind's tool is in hand; else the refusal label (read fault = refused).</summary>
    public static string? HeldToolRefusal(BgReflexPressKind kind) => kind switch
    {
        BgReflexPressKind.Cast or BgReflexPressKind.Tap => RodHeld() ? null : ReasonRodNotHeld,
        BgReflexPressKind.Swing => MeleeWeaponHeld() ? null : ReasonWeaponNotHeld,
        BgReflexPressKind.Charge => ChargeToolHeld() ? null : ReasonToolNotChargeable,
        _ => ReasonRodNotHeld,
    };

    /// <summary>WS-G5: a MeleeWeapon is the current tool (owner 2026-10-23: the scythe counts as a weapon).</summary>
    public static bool MeleeWeaponHeld()
    {
        try
        {
            return Context.IsWorldReady && Game1.player?.CurrentTool is StardewValley.Tools.MeleeWeapon;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>WS-G5: a Hoe or a WateringCan with upgradeLevel >= 1 (the only tools that charge, PRB0 §T1) is the current tool.</summary>
    public static bool ChargeToolHeld()
    {
        try
        {
            return Context.IsWorldReady && Game1.player?.CurrentTool is StardewValley.Tool t
                && t is StardewValley.Tools.Hoe or StardewValley.Tools.WateringCan && t.UpgradeLevel >= 1;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>A FishingRod is the current tool (read fault = not held).</summary>
    public static bool RodHeld()
    {
        try
        {
            return Context.IsWorldReady && Game1.player?.CurrentTool is StardewValley.Tools.FishingRod;
        }
        catch
        {
            return false;
        }
    }
}

internal sealed partial class BackgroundMoveRuntime
{
    // ADR 186: the dismiss H opened this rod's item-grab receiver. R/N cannot run
    // under that modal, but effective release, latch restoration and containment still must prove clean.
    private bool FishingCatchReceiver(IClickableMenu? menu, bool capture = false)
    {
        if (this.reflexPress != BgReflexPressKind.Tap || this.holdPlan?.ConsumedH != 1
            || this.request is not { } req || !MenuStepCommandInbox.IsReflexFish(req)
            || !Context.IsWorldReady || Game1.player?.CurrentTool is not FishingRod rod
            || menu is not ItemGrabMenu grab || !ReferenceEquals(grab.context, rod)) return false;
        if (capture) this.observedMenu ??= menu;
        return ReferenceEquals(menu, this.observedMenu);
    }

    private BgReflexPressKind? reflexPress;

    /// <summary>The in-flight (or last) request is a reflex press of this kind.</summary>
    public BgReflexPressKind? ReflexPressKind => this.request is not null ? this.reflexPress : null;

    /// <summary>
    /// WS-G5: a reflex CHARGE press of the in-flight request has not reached its terminal (its R frame, the X release edge
    /// that fires the charged tool, may still be due). The pad adapter defers a STOP panic and every neutral oldPadState
    /// write while this holds.
    /// </summary>
    public bool ChargePressOutstanding => this.request is not null && this.reflexPress == BgReflexPressKind.Charge;

    /// <summary>The reflex press is issued and not Suppressed yet (the machine's PadDown).</summary>
    public bool ReflexPressDown => this.reflexPress is not null && this.pressSession is { PressIssued: true, SuppressIssued: false };

    /// <summary>
    /// Arm one reflex press for the in-flight reflex_fish request: a pad X TOOL hold of <paramref name="ticks"/> H frames
    /// (cast: released early by <see cref="RequestRelease"/>). Rejected terminal on refusal (nothing pressed), null when
    /// N-R started. The terminal arrives through <see cref="TakeTerminal"/> with the reflex request's id.
    /// </summary>
    /// <remarks>
    /// The reflex request's deadline_unix_ms is its ADMISSION deadline (seconds); the reflex itself runs up to max_s, so each
    /// press carries its own execution deadline (<see cref="ReflexPressDeadlineMs"/> from its arm, never earlier than the
    /// request's). Lease, STOP and focus still end a press at once; the machine bounds the whole reflex.
    /// </remarks>
    /// <para>The slot's controller client owns epoch adoption; subordinate presses retain the admitted pins.</para>
    public BackgroundMoveTerminal? TryArmReflexPress(MenuStepCommandRequest reflexRequest, BgReflexPressKind kind, int ticks) =>
        this.TryArmHoldCore(reflexRequest with
        {
            Ticks = ticks,
            DeadlineUnixMs = Math.Max(reflexRequest.DeadlineUnixMs, NowUnixMs() + ReflexPressDeadlineMs),
        }, BgHoldKind.Tool, null, null, kind);

    /// <summary>
    /// WS-G5: one reflex STEP for the in-flight reflex_combat request: a G0 pad D-pad move of <paramref name="ticks"/> frames
    /// (face = 1 frame; approach / retreat a few) on the unchanged move path (VERIFY-role hold, release proof, stopped
    /// readback). Rejected terminal on refusal (nothing pressed), null when N-R started.
    /// </summary>
    /// <para>A foreign epoch ends combat; no step may re-pin around a receiver change.</para>
    public BackgroundMoveTerminal? TryArmReflexStep(MenuStepCommandRequest reflexRequest, string direction, int ticks) =>
        this.TryArmCore(reflexRequest with
        {
            Direction = direction,
            Ticks = ticks,
            DeadlineUnixMs = Math.Max(reflexRequest.DeadlineUnixMs, NowUnixMs() + ReflexPressDeadlineMs),
        }, allowThrough: false, reflexStep: true);

    /// <summary>Execution bound of one reflex press (cast <= 2.5 s held + settle; a tap far less).</summary>
    public const long ReflexPressDeadlineMs = 10_000;

    public void NeutralizeController(string reason)
    {
        this.RequestRelease(reason);
        this.SuppressOutstandingPad(reason);
        // ADR 152: preserve consumer history until owned R/N consumes the
        // release. Rewriting oldPadState here fabricates a history branch change.
    }

    /// <summary>
    /// reflex_fish admission (before the first press): take / keep gamepad mode (PLAN §1.5 a) and the cursor-hidden gate
    /// with the take-over relaxation (§1.5 b), exactly as a pad hold's admission does. null = owned and hidden.
    /// </summary>
    public string? ReflexPadAdmission(out bool justTook)
    {
        justTook = false;
        if (this.padHost is not { } host || !host.PadInputEnabled)
            return BgReason.PreconditionUnavailable(BgHoldReason.PadModeUnavailable);
        if (!host.DrawPathOn)
            return BgReason.PreconditionUnavailable(BgCursorHidden.Prefix + ":draw_path_off");
        string? mode;
        try
        {
            mode = host.TakeForAdmission(out justTook);
        }
        catch (Exception ex)
        {
            mode = BgHoldReason.PadModeUnavailable + ":" + ex.GetType().Name;
        }
        if (mode is not null)
            return BgReason.PreconditionUnavailable(mode);
        if (BgCursorHidden.Reject(this.SamplePadCursorHidden(), atTakeOver: justTook, ignoreRetainedInput: true) is string hidden)
            return BgReason.PreconditionUnavailable(hidden);
        this.monitor.Log($"background PAD reflex admission take={(justTook ? "taken" : "kept")}", LogLevel.Info);
        return null;
    }
}
