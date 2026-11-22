namespace ArtiStardewBridge.Protocol;

// Primitives v2 WS-G0 (docs/stardew/primitives-v2/PLAN.md section 1.5, WS-G0_BRIEF.md): pure,
// SMAPI-free pieces of the gamepad (pad) retained-press path. Nothing here reads or writes game
// state or emits input. The game-coupled runtime (BackgroundControlInputHooks.cs, ModEntry.Pad.cs)
// samples facts into these records and applies the decisions. Evidence ceiling: UNIT_TESTED.
//
// Decomp references (Stardew 1.6.15, MVID aa1f513a): G1 = StardewValley/Game1.cs.
//   - UpdateControlInput hardcodes the pad buttons (G1:12672-12795): A = action, X = use tool,
//     sticks / D-pad = movement. There is no binding table to derive from: the pins are fixed.
//   - Left stick: X < -0.2 left, X > 0.2 right, Y < -0.2 down, Y > 0.2 up (XNA: +Y = up).
//   - LT/RT are never pad-emulated (SMAPI sets only the analog value; probe step 3b).

/// <summary>The fixed pad button pins (XNA <c>Buttons</c> enum names) and the pad panic set.</summary>
public static class BgPadButtons
{
    public const string A = "A";
    public const string B = "B";
    public const string X = "X";
    public const string Y = "Y";
    public const string Start = "Start";
    public const string Back = "Back";
    public const string LeftShoulder = "LeftShoulder";
    public const string RightShoulder = "RightShoulder";
    public const string DPadUp = "DPadUp";
    public const string DPadDown = "DPadDown";
    public const string DPadLeft = "DPadLeft";
    public const string DPadRight = "DPadRight";
    public const string StickUp = "LeftThumbstickUp";
    public const string StickDown = "LeftThumbstickDown";
    public const string StickLeft = "LeftThumbstickLeft";
    public const string StickRight = "LeftThumbstickRight";

    /// <summary>
    /// Buttons a background pad hold may pin (WS-G0 brief item 4): A, X, Start, Back, Y, the four
    /// left-stick directions and the D-pad. B, the shoulders and the triggers are never held
    /// (B in the world is refused; shoulders / triggers are direct calls, PLAN section 1.1 rule 4).
    /// </summary>
    public static readonly IReadOnlyCollection<string> HoldPins = new HashSet<string>(StringComparer.Ordinal)
    {
        A, X, Start, Back, Y, StickUp, StickDown, StickLeft, StickRight, DPadUp, DPadDown, DPadLeft, DPadRight,
    };

    /// <summary>
    /// PLAN section 1.5 e: pad panic Suppresses every pad button and every left-stick direction ARTI
    /// can press. This is also the containment suppress set of the pad path (WS-G0 test: it is a
    /// superset of <see cref="HoldPins"/> plus B and the shoulders).
    /// </summary>
    public static readonly IReadOnlyList<string> PanicSet = new[]
    {
        A, B, X, Y, Start, Back, LeftShoulder, RightShoulder,
        DPadUp, DPadDown, DPadLeft, DPadRight,
        StickUp, StickDown, StickLeft, StickRight,
    };

    public static bool IsHoldPin(BgButton b) => b.Device == BgDevice.Pad && HoldPins.Contains(b.Name);

    public static bool IsPanicButton(BgButton b) => b.Device == BgDevice.Pad && PanicSet.Contains(b.Name, StringComparer.Ordinal);

    public static bool IsStick(BgButton b) =>
        b.Device == BgDevice.Pad && b.Name is StickUp or StickDown or StickLeft or StickRight;

    /// <summary>
    /// MonoGame derives LeftThumbstick* / RightThumbstick* "virtual buttons" from the analog vector.
    /// The normalizer drops them from the button set; the exact vector carries that fact instead.
    /// </summary>
    public static bool IsVirtualStickButton(string name) =>
        name.StartsWith("LeftThumbstick", StringComparison.Ordinal) || name.StartsWith("RightThumbstick", StringComparison.Ordinal);

    /// <summary>Quantized stick vector of a stick pin (+Y = up, XNA convention). (0,0) for a button.</summary>
    public static (int X, int Y) StickVector(BgButton b) => b.Device != BgDevice.Pad ? (0, 0) : b.Name switch
    {
        StickUp => (0, 1),
        StickDown => (0, -1),
        StickLeft => (-1, 0),
        StickRight => (1, 0),
        _ => (0, 0),
    };

    /// <summary>
    /// The D-pad button of a direction. Live D-B1-2 (batch 1, 2026-10-08): SMAPI keeps every Suppress
    /// override until focus returns, and a Suppress of the OPPOSITE stick direction zeroes the shared
    /// axis (GamePadStateBuilder), so a stick Press up after a stick move down never reached the frame.
    /// D-pad directions are independent button flags (MonoGame GamePadDPad); vanilla UpdateControlInput
    /// moves on them in gamepad mode exactly like the stick (G1:12729-12775 pressed/held, and with
    /// autoRun both run: checkForRunButton G1:13530-13556 only toggles away from autoRun on the stick).
    /// </summary>
    public static BgButton DPad(BgDirection direction) => BgButton.Pad(direction switch
    {
        BgDirection.Up => DPadUp,
        BgDirection.Down => DPadDown,
        BgDirection.Left => DPadLeft,
        _ => DPadRight,
    });

    /// <summary>The pin of a pad move (the model-facing "stick" button): the D-pad direction (D-B1-2).</summary>
    public static BgButton MovePin(BgDirection direction) => DPad(direction);

    public static BgButton Stick(BgDirection direction) => BgButton.Pad(direction switch
    {
        BgDirection.Up => StickUp,
        BgDirection.Down => StickDown,
        BgDirection.Left => StickLeft,
        _ => StickRight,
    });

    /// <summary>The fixed pin of a hold kind: TOOL = pad X (use tool), ACTION = pad A (action).</summary>
    public static BgButton ForKind(BgHoldKind kind) => BgButton.Pad(kind == BgHoldKind.Tool ? X : A);
}

/// <summary>Quantization of one analog axis for exact VERIFY equality.</summary>
public static class BgStickAxis
{
    /// <summary>0 -> 0; full deflection (|v| >= 0.999) -> +-1; any other deflection -> +-2 (foreign/partial).</summary>
    public static int Quantize(float v)
    {
        if (float.IsNaN(v)) return 2;
        if (v == 0f) return 0;
        if (v >= 0.999f) return 1;
        if (v <= -0.999f) return -1;
        return v > 0 ? 2 : -2;
    }
}

/// <summary>
/// Expected owned pad frame: B's mouse X/Y/scroll, no keyboard key, no mouse button, plus the
/// pinned pad button (button pins) or the exact stick vector (stick pins, analog active).
/// R/N frames (<paramref name="key"/> null) equal the button-free baseline exactly.
/// </summary>
public static class BgPadFrame
{
    public static BgInputState Build(BgInputState baseline, BgButton? key)
    {
        if (key is not { } k)
            return new BgInputState(Array.Empty<BgButton>(), baseline.MouseX, baseline.MouseY, baseline.ScrollWheel, false);
        if (!BgPadButtons.IsHoldPin(k))
            throw new ArgumentException("pad frame key must be a pad hold pin", nameof(key));
        if (BgPadButtons.IsStick(k))
        {
            (int x, int y) = BgPadButtons.StickVector(k);
            return new BgInputState(Array.Empty<BgButton>(), baseline.MouseX, baseline.MouseY, baseline.ScrollWheel, true, x, y);
        }
        return new BgInputState(new[] { k }, baseline.MouseX, baseline.MouseY, baseline.ScrollWheel, false);
    }
}

/// <summary>
/// Every SMAPI controller override ARTI issued (Press or Suppress) that SMAPI may still apply.
/// Facts (probe Part 1 section 2, SIS:88 / SCORE:795-797, 1144): while the window is inactive SMAPI
/// re-applies the pressed/released override sets every tick; only the active-tick TrueUpdate clears
/// them. Any applied controller override builds a "connected" virtual pad (GPB:542-551, MonoGame
/// GamePadState IsConnected = true). The PadModeOwner restores gamepadMode only when this ledger
/// is empty (PLAN section 1.5 a step 2). HYPOTHESIS until PRB-10: the active TrueUpdate of tick T
/// runs before UpdateTicking of tick T and clears every earlier override.
/// </summary>
public sealed class PadOverrideLedger
{
    private readonly Dictionary<string, (bool Pressed, long Seq)> entries = new(StringComparer.Ordinal);

    /// <summary>Update sequence of the current tick (advanced by <see cref="BeginTick"/>).</summary>
    public long Seq { get; private set; }

    /// <summary>
    /// Start of every UpdateTicking, before any ARTI Press/Suppress of that tick. On an active tick
    /// the TrueUpdate that just ran cleared every override issued in an earlier tick.
    /// </summary>
    public void BeginTick(bool active)
    {
        this.Seq++;
        if (!active)
            return;
        foreach (string name in this.entries.Where(e => e.Value.Seq < this.Seq).Select(e => e.Key).ToList())
            this.entries.Remove(name);
    }

    public void RecordPress(string padButton) => this.entries[padButton] = (true, this.Seq);

    public void RecordSuppress(string padButton) => this.entries[padButton] = (false, this.Seq);

    /// <summary>No ARTI controller override can still be applied.</summary>
    public bool Empty => this.entries.Count == 0;

    /// <summary>An override was issued in the current tick (it will be applied by this tick's ApplyOverrides).</summary>
    public bool AnyIssuedThisTick => this.entries.Values.Any(e => e.Seq == this.Seq);

    /// <summary>Pad buttons whose last override is a Press (held down by ARTI).</summary>
    public IReadOnlyList<string> Outstanding =>
        this.entries.Where(e => e.Value.Pressed).Select(e => e.Key).OrderBy(n => n, StringComparer.Ordinal).ToArray();

    public IReadOnlyList<string> Names => this.entries.Keys.OrderBy(n => n, StringComparer.Ordinal).ToArray();

    public int Count => this.entries.Count;
}

/// <summary>
/// PLAN section 1.5 b / WS-G0 cursor-hidden gate (replaces CursorInert on the pad path). Facts are
/// read-only samples (Game1 statics). The inactive-render flag remains required. Vanilla advances
/// the fade and writes <c>wasMouseVisibleThisFrame</c> in drawMouseCursor (G1:14691-14700,14780);
/// FIXB54 r2 can reconcile a stale visible latch for an owned, bound background request using
/// BgVanillaCursor's native hide predicate before this strict guard reads it.
///
/// Deviation from the brief's predicate (decomp-verified): drawMouseCursor sets
/// <c>mouseCursorTransparency = 0</c> before computing <c>wasMouseVisibleThisFrame</c> and resets it
/// to 1 at its end whenever no menu is open and no action is under the cursor (G1:14784-14787), so
/// an update-time read of <c>mouseCursorTransparency == 0</c> is false on almost every tick. The game's
/// own pad readers use <c>!wasMouseVisibleThisFrame</c> (tool G1:11633, GetToolLocation CH:1245-1251)
/// or <c>!wasMouseVisibleThisFrame || transparency == 0</c> (action G1:11317, placement G1:11486). The
/// transparency is published for diagnostics only and is not a gate term.
/// </summary>
public sealed record BgCursorHiddenFacts(
    bool DrawPathOn,
    bool GamepadControls,
    bool WasMouseVisibleThisFrame,
    float MouseCursorTransparency,
    int TimerUntilMouseFade,
    bool KeyDown,
    bool MouseButtonDown,
    bool EmoteBlocksFade = false);

/// <summary>
/// Conservative world-only subset of Game1.drawMouseCursor's hide/visibility assignment
/// (1.6.15 Game1.cs:14698-14702,14780), with DrawOverlays eligibility (:13611-13613).
/// No elapsed-time approximation: a positive fade timer is never advanced here.
/// </summary>
public sealed record BgVanillaCursorFacts(bool GamepadControls, int FadeTimer, bool MenuOpen,
    bool EmoteBlocksFade, bool CursorCanRecord, bool LocationPresent, bool OverlayEligible);

public static class BgVanillaCursor
{
    public static bool WouldHide(BgVanillaCursorFacts? f) => f is not null
        && f.GamepadControls && f.FadeTimer <= 0 && !f.MenuOpen && !f.EmoteBlocksFade
        && f.CursorCanRecord && f.LocationPresent && f.OverlayEligible;
}

public static class BgCursorHidden
{
    public const string Prefix = "CURSOR_NOT_HIDDEN";

    /// <summary>
    /// null = hidden. <paramref name="atTakeOver"/>: the admission that just took gamepadMode zeroed
    /// the fade timer this tick and no Draw ran since, so <c>visible_this_frame</c> is not a reason
    /// yet; <paramref name="ignoreRetainedInput"/>: admission only (N-R suppresses retained keys
    /// before the latch). Both are re-checked without relaxation before the latch (N-H) and at every
    /// VERIFY hook entry, so no pad Press reaches the consumer with a visible cursor.
    /// </summary>
    public static string? Reject(BgCursorHiddenFacts? f, bool atTakeOver = false, bool ignoreRetainedInput = false)
    {
        if (f is null) return Prefix + ":unreadable";
        if (!f.DrawPathOn) return Prefix + ":draw_path_off";
        if (!f.GamepadControls) return Prefix + ":gamepad_controls_off";
        if (!ignoreRetainedInput && f.KeyDown) return Prefix + ":key_down";
        if (!ignoreRetainedInput && f.MouseButtonDown) return Prefix + ":mouse_button_down";
        if (f.TimerUntilMouseFade > 0 && !atTakeOver) return Prefix + ":fade_timer";
        if (f.EmoteBlocksFade) return Prefix + ":emote_blocks_fade";
        if (f.WasMouseVisibleThisFrame && !atTakeOver) return Prefix + ":visible_this_frame";
        return null;
    }
}

/// <summary>
/// WS-G0: before the latch (N-H), a pad request whose only failing cursor term is the fade still in
/// progress waits a bounded number of ticks (a native draw or eligible owned reconciliation hides it).
/// </summary>
public static class BgPadFade
{
    public const int WaitTicks = 30;

    public static bool Pending(string reason) =>
        reason == BgCursorHidden.Prefix + ":visible_this_frame" || reason == BgCursorHidden.Prefix + ":fade_timer";
}

/// <summary>
/// PLAN section 1.5 d: <c>neutral_tick_seen</c> := one UpdateControlInput tick on which no ARTI pad
/// Press is outstanding (Suppress-only overrides are allowed; they persist while inactive) AND
/// <c>Game1.oldPadState</c> is neutral. The pad latch admits the first H frame of a request only
/// after it (the pre-H N0 / neutral frames are such ticks once N-H has neutralized oldPadState).
/// </summary>
public sealed class BgPadNeutralTickGate
{
    public bool Seen { get; private set; }

    /// <summary>A menu exit wrote a neutral oldPadState: the next world pad frame needs a fresh neutral tick.</summary>
    public void Reset() => this.Seen = false;

    /// <summary>Observed at an UpdateControlInput entry (pre-H frame).</summary>
    public bool Observe(bool pressOutstanding, bool oldPadNeutral)
    {
        if (!pressOutstanding && oldPadNeutral)
            this.Seen = true;
        return this.Seen;
    }

    public static bool IsNeutral(BgInputState history) =>
        !history.PadAnalogActive && history.LeftStickX == 0 && history.LeftStickY == 0
        && !history.Down.Any(b => b.Device == BgDevice.Pad);
}

/// <summary>
/// WS-G0 OS-cursor guard decision (PLAN section 1.5; probe Part 1 (d), step 4a/4b): a Harmony prefix
/// on <c>StardewValley.InputState.SetMousePosition(int,int)</c> skips the OS call while the farmhand
/// window is inactive and <c>GamepadInputEnabled</c> is on. Counters are published in session.json.
/// <c>UnblockedInactive</c> counts inactive calls that reached the OS because the guard was off at
/// call time (flag switched off while the patch is installed); it must stay 0 (an unblocked inactive
/// warp = global stop, PLAN section 6.2).
/// </summary>
public sealed class OsWarpGuardCounters
{
    private long calls, blocked, unblockedInactive, blockedActive;

    public long Calls => Interlocked.Read(ref this.calls);
    public long Blocked => Interlocked.Read(ref this.blocked);
    public long UnblockedInactive => Interlocked.Read(ref this.unblockedInactive);
    /// <summary>ADR 244: snaps blocked while her window was the active one (included in <see cref="Blocked"/>).</summary>
    public long BlockedActive => Interlocked.Read(ref this.blockedActive);

    /// <summary>
    /// true = let the original run (the OS cursor moves). Never throws. ADR 244: with the guard enabled the
    /// real Windows cursor is never moved by her game, whether her window is active or not (the owner's
    /// cursor is the same OS cursor in both states). Guard off: the game's own behaviour, unchanged.
    /// </summary>
    public bool Decide(bool guardEnabled, bool windowActive)
    {
        Interlocked.Increment(ref this.calls);
        if (guardEnabled)
        {
            Interlocked.Increment(ref this.blocked);
            if (windowActive)
                Interlocked.Increment(ref this.blockedActive);
            return false;
        }
        if (!windowActive)
            Interlocked.Increment(ref this.unblockedInactive);
        return true;
    }
}

/// <summary>
/// WS-G0 world denylist / route term for the pad path: a world request on the pad path needs the
/// pad flag, the background runtime and an inactive window; it never falls back to the keyboard
/// path while the pad flag is on (the keyboard latch stays behind the flag, owner D14).
/// </summary>
public static class BgPadRoute
{
    public static bool PadPath(bool gamepadInputEnabled, bool backgroundToolUiEnabled) =>
        gamepadInputEnabled && backgroundToolUiEnabled;
}
