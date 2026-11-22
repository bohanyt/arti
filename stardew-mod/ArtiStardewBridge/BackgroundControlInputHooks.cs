using System.Reflection;
using ArtiCore.Pure;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using Microsoft.Xna.Framework.Input;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Tools;
// Exact installed namespaces (CT correction C1); aliases keep the rest of those
// namespaces out of scope.
using DelegatingModHooks = StardewModdingAPI.Utilities.DelegatingModHooks;
using IInputSimulator = StardewValley.Util.IInputSimulator;
using ModHooks = StardewValley.Mods.ModHooks;

namespace ArtiStardewBridge;

// V4 / V4.1 background movement, S2 game-coupled layer (#278/5791090505 +
// #278/5791607180, PASS #278/5791691966). Stage 1 = background MOVEMENT ONLY.
//
// Pieces in this file:
//   * ArtiReachabilityLatch  - no-write IInputSimulator (A1). Only makes the
//     installed Game1._update admit UpdateControlInput while inactive.
//   * ArtiControlInputHooks  - the single DelegatingModHooks wrapper (A2), pinned
//     chain ARTI -> SModHooks -> vanilla ModHooks. Pre-consumer substitution seam.
//   * BackgroundMoveRuntime  - game-thread coordinator for one input_once
//     world_key/move_cardinal request from the EXISTING command inbox under the
//     EXISTING NativeActionOwner lease. Decisions come from the S1 pure layer
//     (BackgroundFramePlan.cs / BackgroundContainmentPolicy.cs).
//
// Not here: no second inbox/store/transport/owner, no desktop/OS input, no
// Farmer.Halt(), no direct world outcome call, no fishing/combat.
//
// V4 Stage 2 (#278/5994543545 + 2.1 5994935998 + 2.2 5995067151, dispatch
// 5995122084 R1-R5), default OFF behind ModConfig.BackgroundToolUiEnabled:
// BackgroundMoveRuntime is also the SOLE issuer of SMAPI Press/Suppress for an
// inactive-admitted use_tool / world_interact request, through one
// BgPressSession for one pinned keyboard key (HelperPressPort below is the only
// Press call site). The S2 wrapper serves that request in the VERIFY role.
// The foreground use-tool / world-interaction objects are never touched here.

/// <summary>
/// A1 reachability latch. <see cref="SimulateInput"/> writes NONE of the 18 ref
/// flags and is never a movement filter; it records only a bounded receipt count
/// and the update sequence of the last receipt. One instance per request.
/// </summary>
internal sealed class ArtiReachabilityLatch : IInputSimulator
{
    private readonly Func<long> updateSeq;
    private int receipts;
    private long lastReceiptSeq = -1;

    public ArtiReachabilityLatch(string requestId, string sessionId, string runtimeId, string ownerId, long generation, int runId, Func<long> updateSeq)
    {
        this.RequestId = requestId;
        this.SessionId = sessionId;
        this.RuntimeId = runtimeId;
        this.OwnerId = ownerId;
        this.Generation = generation;
        this.RunId = runId;
        this.updateSeq = updateSeq;
    }

    public string RequestId { get; }
    public string SessionId { get; }
    public string RuntimeId { get; }
    public string OwnerId { get; }
    public long Generation { get; }
    public int RunId { get; }
    public int Receipts => this.receipts;
    public long LastReceiptSeq => this.lastReceiptSeq;

    public void SimulateInput(
        ref bool actionButtonPressed,
        ref bool switchToolButtonPressed,
        ref bool useToolButtonPressed,
        ref bool useToolButtonReleased,
        ref bool addItemToInventoryButtonPressed,
        ref bool cancelButtonPressed,
        ref bool moveUpPressed,
        ref bool moveRightPressed,
        ref bool moveLeftPressed,
        ref bool moveDownPressed,
        ref bool moveUpReleased,
        ref bool moveRightReleased,
        ref bool moveLeftReleased,
        ref bool moveDownReleased,
        ref bool moveUpHeld,
        ref bool moveRightHeld,
        ref bool moveLeftHeld,
        ref bool moveDownHeld)
    {
        // Intentionally writes none of the 18 flags (V4 A1). Receipt/identity only.
        if (this.receipts < int.MaxValue)
            this.receipts++;
        this.lastReceiptSeq = this.updateSeq();
    }
}

/// <summary>
/// CT correction C3: the single accessor for the two game fields this layer owns a
/// slot in. <c>Game1.hooks</c> and <c>Game1.inputSimulator</c> are both
/// <c>protected internal static</c>, so a mod assembly cannot touch them directly.
/// Both are resolved ONCE through SMAPI <see cref="IReflectionHelper"/> (no per-tick
/// lookup, no raw FieldInfo writes, no Harmony, no subclass access trick). Every
/// read / write / ownership check in this file goes through this object.
/// Reads and writes never throw: a failure reports false / "not owned", i.e. the
/// caller fails closed.
/// </summary>
internal sealed class GameInputFieldAccess
{
    public const string HooksFieldName = "hooks";
    public const string InputSimulatorFieldName = "inputSimulator";

    private readonly IReflectedField<ModHooks?> hooks;
    private readonly IReflectedField<IInputSimulator?> inputSimulator;

    private GameInputFieldAccess(IReflectedField<ModHooks?> hooks, IReflectedField<IInputSimulator?> inputSimulator)
    {
        this.hooks = hooks;
        this.inputSimulator = inputSimulator;
    }

    /// <summary>
    /// Resolve both reflected fields once. Null (+ reason) when either is missing or
    /// not the exact expected static field; background movement then stays disabled.
    /// Never throws.
    /// </summary>
    public static GameInputFieldAccess? TryCreate(IReflectionHelper reflection, out string failure)
    {
        try
        {
            IReflectedField<ModHooks?>? h = reflection.GetField<ModHooks?>(typeof(Game1), HooksFieldName, required: true);
            IReflectedField<IInputSimulator?>? s = reflection.GetField<IInputSimulator?>(typeof(Game1), InputSimulatorFieldName, required: true);
            if (h is null || s is null)
            {
                failure = "GAME_FIELD_MISSING";
                return null;
            }
            // SMAPI's ReflectedField does not validate the field type; pin it here.
            if (!h.FieldInfo.IsStatic || h.FieldInfo.FieldType != typeof(ModHooks))
            {
                failure = "GAME_FIELD_SHAPE(" + HooksFieldName + ")";
                return null;
            }
            if (!s.FieldInfo.IsStatic || s.FieldInfo.FieldType != typeof(IInputSimulator))
            {
                failure = "GAME_FIELD_SHAPE(" + InputSimulatorFieldName + ")";
                return null;
            }
            failure = "";
            return new GameInputFieldAccess(h, s);
        }
        catch (Exception ex)
        {
            failure = "GAME_FIELD_REFLECTION_FAILED(" + ex.GetType().Name + ")";
            return null;
        }
    }

    public bool TryReadHooks(out ModHooks? value)
    {
        try
        {
            value = this.hooks.GetValue();
            return true;
        }
        catch
        {
            value = null;
            return false;
        }
    }

    public bool TryWriteHooks(ModHooks value)
    {
        try
        {
            this.hooks.SetValue(value);
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>Fresh re-read; exact reference identity. Read failure = false.</summary>
    public bool HooksAre(ModHooks expected) => this.TryReadHooks(out ModHooks? v) && ReferenceEquals(v, expected);

    public bool TryReadInputSimulator(out IInputSimulator? value)
    {
        try
        {
            value = this.inputSimulator.GetValue();
            return true;
        }
        catch
        {
            value = null;
            return false;
        }
    }

    public bool TryWriteInputSimulator(IInputSimulator? value)
    {
        try
        {
            this.inputSimulator.SetValue(value);
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>Fresh re-read; exact reference identity (null == null). Read failure = false.</summary>
    public bool InputSimulatorIs(object? expected) =>
        this.TryReadInputSimulator(out IInputSimulator? v) && ReferenceEquals(v, expected);

    /// <summary>Fresh re-read; field empty. Read failure = false (treated as not empty).</summary>
    public bool InputSimulatorIsNull() => this.TryReadInputSimulator(out IInputSimulator? v) && v is null;
}

/// <summary>
/// A2: the one ARTI <see cref="DelegatingModHooks"/> wrapper. Overrides only
/// <see cref="OnGame1_UpdateControlInput"/>; every other hook delegates to Parent.
/// Admitted chain is exactly ARTI wrapper -> SModHooks -> vanilla ModHooks.
/// </summary>
internal sealed partial class ArtiControlInputHooks : DelegatingModHooks
{
    public const string SmapiHooksTypeName = "StardewModdingAPI.Framework.SModHooks";

    public const string ReasonInstallUnconfirmed = "HOOK_INSTALL_UNCONFIRMED";

    private readonly BackgroundMoveRuntime runtime;
    private readonly GameInputFieldAccess fields;
    private readonly ModHooks pinnedSmapi;
    private readonly ModHooks pinnedVanilla;

    private ArtiControlInputHooks(ModHooks smapi, ModHooks vanilla, BackgroundMoveRuntime runtime, GameInputFieldAccess fields)
        : base(smapi)
    {
        this.pinnedSmapi = smapi;
        this.pinnedVanilla = vanilla;
        this.runtime = runtime;
        this.fields = fields;
    }

    /// <summary>Admission: current hooks are exactly SModHooks whose Parent is exactly vanilla ModHooks.</summary>
    public static bool IsAdmissibleSmapiChain(ModHooks? current, out ModHooks? vanilla)
    {
        vanilla = null;
        if (current is not DelegatingModHooks smapi
            || !string.Equals(current.GetType().FullName, SmapiHooksTypeName, StringComparison.Ordinal))
            return false;
        ModHooks? parent = smapi.Parent;
        if (parent is null || parent.GetType() != typeof(ModHooks))
            return false;
        vanilla = parent;
        return true;
    }

    /// <summary>
    /// Install once on an admitted chain, through the reflected field only: read,
    /// require exact SModHooks -> vanilla ModHooks, write the wrapper, re-read and
    /// require exact reference identity. Never replaces a foreign chain and never
    /// writes again after an unconfirmed install.
    /// </summary>
    public static ArtiControlInputHooks? TryInstall(BackgroundMoveRuntime runtime, GameInputFieldAccess fields, out string failure)
    {
        if (!fields.TryReadHooks(out ModHooks? current)
            || current is null
            || !IsAdmissibleSmapiChain(current, out ModHooks? vanilla)
            || vanilla is null)
        {
            failure = BgReason.HookChainNotAdmitted;
            return null;
        }
        ArtiControlInputHooks wrapper = new(current, vanilla, runtime, fields);
        if (!fields.TryWriteHooks(wrapper) || !fields.HooksAre(wrapper))
        {
            // Whatever the field holds now is left untouched (no retry, no overwrite).
            // An orphaned wrapper, if it did land, is exact pass-through: this runtime
            // never admits a request after an unconfirmed install.
            failure = ReasonInstallUnconfirmed;
            return null;
        }
        failure = "";
        return wrapper;
    }

    /// <summary>
    /// Exact pinned identity, re-read through the reflected field: the hooks field is
    /// this, Parent is the pinned SModHooks, its Parent the pinned vanilla.
    /// </summary>
    public bool IsExactChain() =>
        this.fields.HooksAre(this)
        && ReferenceEquals(this.Parent, this.pinnedSmapi)
        && this.pinnedSmapi is DelegatingModHooks smapi
        && ReferenceEquals(smapi.Parent, this.pinnedVanilla)
        && this.pinnedVanilla.GetType() == typeof(ModHooks);

    /// <summary>The single Parent invocation used by admitted substituted frames.</summary>
    internal void InvokeParent(ref KeyboardState keyboardState, ref MouseState mouseState, ref GamePadState gamePadState, Action action)
    {
        var slot = this.runtime.ExecutionSlot ?? throw new InvalidOperationException("input_unadmitted");
        var keyboard = keyboardState; var mouse = mouseState; var pad = gamePadState;
        try { slot.Bracket(() => this.Parent.OnGame1_UpdateControlInput(ref keyboard, ref mouse, ref pad, action)); }
        finally { keyboardState = keyboard; mouseState = mouse; gamePadState = pad; }
    }
}

internal enum BackgroundTerminalKind
{
    /// <summary>Refused before any game input-history mutation.</summary>
    Rejected,
    /// <summary>Lifecycle ended, no world-effect claim (status completed, success=false).</summary>
    NotCompleted,
    /// <summary>All V4 completion evidence present.</summary>
    Completed,
    /// <summary>Interrupted / uncertain / containment involvement. Never PASS.</summary>
    Uncertain,
}

internal sealed record BackgroundMoveTerminal(
    BackgroundTerminalKind Kind,
    MenuStepCommandRequest Request,
    string Label,
    int ConsumedH,
    string Direction,
    int RunId,
    BackgroundHoldResult? Hold = null,
    // WS3a move_through: the observed destination of a WARPED terminal.
    string? ResultLocationId = null,
    BgMoveCompletionFacts? MovementReadback = null,
    // fix92A / ADR 162: how far a guarded path got and why it ended (diagnostics only).
    MovePathReport? Path = null);

/// <summary>Stage 2 hold terminal payload: the pure verdict plus the pinned menu reference (ACTION).</summary>
internal sealed record BackgroundHoldResult(BgHoldKind Kind, BgHoldOutcome Outcome, object? Menu, BgHoldCompletionFacts? Facts);

/// <summary>
/// Stage 2: the ONLY Press call site for background requests (2.2 N1). Adapts the
/// pure <see cref="IBgPressPort"/> to SMAPI. Keyboard keys, and (WS-G0) the fixed pad pins
/// mapped to SMAPI controller buttons (<c>Buttons.X</c> -> <c>SButton.ControllerX</c>, sticks ->
/// <c>LeftThumbstick*</c>). Every pad Press/Suppress is recorded in the shared
/// <see cref="PadOverrideLedger"/> (the PadModeOwner's "override set", PLAN section 1.5 a). The N-R
/// release set, the pad-mode focus-regain step and pad panic Suppress through this port too, so
/// IInputHelper.Press / Suppress appear in this runtime file only inside this class.
/// </summary>
internal sealed class HelperPressPort : IBgPressPort
{
    private readonly IInputHelper input;
    private readonly PadOverrideLedger? ledger;

    public HelperPressPort(IInputHelper input, PadOverrideLedger? ledger = null)
    {
        this.input = input;
        this.ledger = ledger;
    }

    private static SButton ToSButton(BgButton key)
    {
        bool admissible = key.Device == BgDevice.Keyboard || BgPadButtons.IsHoldPin(key) || BgPadButtons.IsPanicButton(key);
        if (!admissible || !BackgroundInputNormalizer.TryToSButton(key, out SButton button) || button == SButton.None)
            throw new InvalidOperationException("hold key is not a mappable keyboard key or pad pin: " + key);
        return button;
    }

    public void Press(BgButton key)
    {
        _ = ToSButton(key); // validate before recording
        if (key.Device == BgDevice.Pad)
            this.ledger?.RecordPress(key.Name); // recorded before the call: a throwing Press is still "may be applied"
        this.input.Press(ToSButton(key));
    }

    public void Suppress(BgButton key)
    {
        SButton button = ToSButton(key);
        this.input.Suppress(button);
        // Recorded only after the call returned: a faulting Suppress leaves the press outstanding (conservative).
        if (key.Device == BgDevice.Pad)
            this.ledger?.RecordSuppress(key.Name);
    }

    public bool IsSuppressed(BgButton key) => this.input.IsSuppressed(ToSButton(key));

    /// <summary>N-R release of a retained SMAPI button (release-only). Pad buttons are recorded in the ledger.</summary>
    public void SuppressRetained(SButton button)
    {
        this.input.Suppress(button);
        if (button.TryGetController(out Buttons pad))
            this.ledger?.RecordSuppress(pad.ToString());
    }

    /// <summary>
    /// Review R5 S1: when her press ends, an OWNER button she suppressed and the owner still holds is handed back. SMAPI
    /// keeps a Suppress while the physical button is down. With the window active, its next TrueUpdate applies this Press
    /// once and then reads the physical button again, so the owner's held key resumes. Only call it with her window active
    /// (in the background a Press is retained). false = nothing to hand back (already released, so SMAPI dropped it).
    /// </summary>
    public bool RestoreOwner(SButton button)
    {
        // Only a button the owner still physically holds: a just-released one would become a phantom tap in her game.
        if (!this.input.IsSuppressed(button) || !OwnerPhysicallyDown(button))
            return false;
        this.input.Press(button);
        return true;
    }

    /// <summary>
    /// Review R5 S1: whether the owner physically holds a keyboard key or mouse button. This reads raw XNA state, not
    /// SMAPI's masked view. Tests substitute their physical model; a read fault answers false (nothing is handed back).
    /// </summary>
    internal static Func<SButton, bool> OwnerPhysicallyDown = NotKnown;

    /// <summary>Default until the mod installs OwnerInputProbe.RawDown: unknown means nothing is handed back.</summary>
    static bool NotKnown(SButton button) => false;
}

/// <summary>SMAPI/XNA <-> S1 normalization. Pure conversions; no game writes.</summary>
internal static class BackgroundInputNormalizer
{
    private static readonly Buttons[] PadButtons = (Buttons[])Enum.GetValues(typeof(Buttons));

    /// <summary>
    /// <paramref name="padPath"/> (WS-G0, pad requests only): MonoGame's derived LeftThumbstick* /
    /// RightThumbstick* virtual buttons are dropped and the exact quantized left-stick vector is carried
    /// instead. Keyboard-path calls (false) are the v1 normalization exactly (review minor 3).
    /// </summary>
    public static BgInputState Normalize(KeyboardState k, MouseState m, GamePadState p, bool padPath = false)
    {
        List<BgButton> down = new();
        foreach (Keys key in k.GetPressedKeys())
            down.Add(BgButton.Key(key.ToString()));
        if (m.LeftButton == ButtonState.Pressed) down.Add(BgButton.Mouse("Left"));
        if (m.RightButton == ButtonState.Pressed) down.Add(BgButton.Mouse("Right"));
        if (m.MiddleButton == ButtonState.Pressed) down.Add(BgButton.Mouse("Middle"));
        if (m.XButton1 == ButtonState.Pressed) down.Add(BgButton.Mouse("XButton1"));
        if (m.XButton2 == ButtonState.Pressed) down.Add(BgButton.Mouse("XButton2"));
        foreach (Buttons b in PadButtons)
        {
            // WS-G0: MonoGame's LeftThumbstick* / RightThumbstick* are derived from the analog vector;
            // the exact quantized vector below carries that fact (analog != 0 already diverges v1 frames).
            if (p.IsButtonDown(b) && !(padPath && BgPadButtons.IsVirtualStickButton(b.ToString())))
                down.Add(BgButton.Pad(b.ToString()));
        }
        bool analog = p.ThumbSticks.Left != Vector2.Zero || p.ThumbSticks.Right != Vector2.Zero
            || p.Triggers.Left != 0f || p.Triggers.Right != 0f;
        if (!padPath)
            return new BgInputState(down, m.X, m.Y, m.ScrollWheelValue, analog);
        return new BgInputState(down, m.X, m.Y, m.ScrollWheelValue, analog,
            BgStickAxis.Quantize(p.ThumbSticks.Left.X), BgStickAxis.Quantize(p.ThumbSticks.Left.Y));
    }

    /// <summary>Synthetic keyboard frame: only the admitted keyboard keys of <paramref name="s"/>.</summary>
    public static KeyboardState ToKeyboard(BgInputState s)
    {
        if (s.Down.Any(b => b.Device != BgDevice.Keyboard) || s.PadAnalogActive)
            throw new InvalidOperationException("synthetic frame carries non-keyboard input");
        Keys[] keys = s.Down.Select(b => Enum.Parse<Keys>(b.Name)).ToArray();
        return new KeyboardState(keys);
    }

    /// <summary>Synthetic mouse: B's X/Y/scroll, every button released, B's horizontal scroll.</summary>
    public static MouseState ToMouse(BgInputState s, MouseState baselineRaw) =>
        new(s.MouseX, s.MouseY, s.ScrollWheel,
            ButtonState.Released, ButtonState.Released, ButtonState.Released, ButtonState.Released, ButtonState.Released,
            baselineRaw.HorizontalScrollWheelValue);

    /// <summary>Every Options InputButton[] field (reflection), normalized for S1 BgBindingTable.</summary>
    public static Dictionary<string, IReadOnlyList<BgButton>> EnumerateBindings(Options options)
    {
        Dictionary<string, IReadOnlyList<BgButton>> result = new(StringComparer.Ordinal);
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
        foreach (FieldInfo field in typeof(Options).GetFields(flags))
        {
            if (field.FieldType != typeof(InputButton[]))
                continue;
            InputButton[]? raw = field.GetValue(field.IsStatic ? null : options) as InputButton[];
            List<BgButton> buttons = new();
            foreach (InputButton ib in raw ?? Array.Empty<InputButton>())
            {
                if (ib.key != Keys.None)
                    buttons.Add(BgButton.Key(ib.key.ToString()));
                if (ib.mouseLeft)
                    buttons.Add(BgButton.Mouse("Left"));
                if (ib.mouseRight)
                    buttons.Add(BgButton.Mouse("Right"));
            }
            result[field.Name] = buttons;
        }
        return result;
    }

    public static bool TryToSButton(BgButton b, out SButton button)
    {
        button = SButton.None;
        switch (b.Device)
        {
            case BgDevice.Keyboard:
                if (!Enum.TryParse(b.Name, out Keys key))
                    return false;
                button = key.ToSButton();
                return true;
            case BgDevice.Mouse:
                button = b.Name switch
                {
                    "Left" => SButton.MouseLeft,
                    "Right" => SButton.MouseRight,
                    "Middle" => SButton.MouseMiddle,
                    "XButton1" => SButton.MouseX1,
                    "XButton2" => SButton.MouseX2,
                    _ => SButton.None,
                };
                return button != SButton.None;
            default:
                if (!Enum.TryParse(b.Name, out Buttons pad))
                    return false;
                button = pad.ToSButton();
                return true;
        }
    }

    public static bool TryParseDirection(string? raw, out BgDirection direction)
    {
        direction = BgDirection.Up;
        switch ((raw ?? "").Trim().ToLowerInvariant())
        {
            case "up": direction = BgDirection.Up; return true;
            case "down": direction = BgDirection.Down; return true;
            case "left": direction = BgDirection.Left; return true;
            case "right": direction = BgDirection.Right; return true;
            default: return false;
        }
    }
}

/// <summary>
/// Game-thread coordinator for one stage-1 background movement request.
/// Lifecycle: N-R -> N-H -> latch -> N0 (+ bounded neutral cleanup) ->
/// POST_N0_CANONICALIZE -> H/R/N -> latch removal -> independent readback.
/// The hook wrapper calls <see cref="Route"/> / <see cref="Serve"/>; SMAPI
/// UpdateTicking/UpdateTicked call the tick methods. Only the watchdog thread
/// may terminate the process, and only through the S1 policy decision.
/// </summary>
internal sealed partial class BackgroundMoveRuntime
{
    public ExecutionSlot? ExecutionSlot { get; set; }
    public Func<long>? ReceiverEpoch { get; set; }
    private ActiveAttempt? executionAttempt;
    public bool CleanupPending => this.latch is not null || this.latchRestorationUnresolved || this.policy?.Armed == true
        || this.PressOverrideOutstanding;

    private string SlotPhase => this.holdKind is null ? "walking"
        : this.holdKind == BgHoldKind.Tool
            ? (this.holdPlan?.ConsumedH ?? 0) > 0 ? "release" : "charging"
            : (this.holdPlan?.ConsumedH ?? 0) > 0 ? "post_edge" : "pre_edge";

    private bool SlotValid(MenuStepCommandRequest req, bool binding, bool target = true, bool pose = true)
    {
        var facts = new ValidityFacts(Context.IsWorldReady, this.ReceiverEpoch?.Invoke() ?? -1, binding,
            TargetExists: target, PoseMatches: pose);
        return this.ExecutionSlot?.Owns(req) == true
            && (this.ExecutionSlot.CanContinue(this.executionAttempt, this.SlotPhase, facts)
                || this.releaseReason is not null && this.ExecutionSlot.CanNeutralize(this.executionAttempt, this.SlotPhase, facts));
    }
    /// <summary>N-R: ticks to observe bound-free retained state after Suppress (disclosed S2 bound).</summary>
    public const int RetainedNeutralBoundTicks = 60;
    /// <summary>Ticks to wait for the first owned N0 callback after latch installation (disclosed S2 bound).</summary>
    public const int AwaitN0BoundTicks = 30;
    /// <summary>Post-N0 fresh captures before the N0 stop decision (2 consecutive stable captures prove stop).</summary>
    public const int N0ReadbackCaptures = 3;
    /// <summary>Unarmed stall / post-N verification bound; reuses the frozen 10 s T4 bound.</summary>
    public const long ProgressBoundMs = 10_000;

    public const string ReasonN0NotConsumed = "N0_NOT_CONSUMED";
    public const string ReasonNhWriteUnconfirmed = "N_H_WRITE_UNCONFIRMED";
    public const string ReasonLatchUnreachable = "LATCH_NOT_OWNED";
    public const string ReasonNoCallbackProgress = "NO_CALLBACK_PROGRESS";
    public const string ReasonReleaseUnverified = "RELEASE_UNVERIFIED";
    /// <summary>
    /// Live7 smoke T5 (2026-10-23): a menu / DialogueBox opened mid pad walk. Game1 skips
    /// UpdateControlInput while it is up, so no VERIFY callback can carry R/N or the key-absent receipt.
    /// </summary>
    public const string ReasonMenuOpenedMidMove = "MENU_OPENED_MID_MOVE";

    /// <summary>Stage 2: an outstanding press without VERIFY progress is released after this (T3-sized) bound.</summary>
    public const long HoldProgressBoundMs = 1500;
    /// <summary>Stage 2 settle bound (release proof, latch removal, UsingTool false); existing use-tool cleanup bound.</summary>
    public const long HoldSettleBoundMs = UseToolInputInvocation.CleanupTimeoutMs;
    /// <summary>Installed Game1 module the read-only cursor projection is pinned to (2.1 section 2).</summary>
    public static readonly Guid PinnedGameMvid = new("aa1f513a-94f0-4ef6-a35f-2d5b4a3721bf");
    /// <summary>
    /// Module identity read for the MVID pin. Production always reads the loaded Game1
    /// module; the offline runtime harness substitutes it (its doubles are not the game).
    /// </summary>
    internal static Func<Guid> GameModuleVersionId = static () => typeof(Game1).Module.ModuleVersionId;

    private readonly IModHelper helper;
    private readonly IMonitor monitor;
    private readonly NativeActionOwner owner;
    private readonly BackgroundContainmentWatchdog watchdog;
    private readonly Func<CommandInboxIdentity> identity;
    private readonly string runtimeInstanceId;
    private readonly Func<bool> farmToolsEnabled;
    // WS-G0: the single press port (shared pad override ledger) and the pad-mode host (null = keyboard only).
    private readonly HelperPressPort port;
    private readonly IPadModeHost? padHost;

    public const string ReasonFieldAccessUnavailable = "GAME_FIELD_ACCESS_UNAVAILABLE";
    public const string ReasonLatchInstallUnconfirmed = "LATCH_INSTALL_UNCONFIRMED";

    // C3: resolved once at construction; null = background capability disabled (fail closed).
    private readonly GameInputFieldAccess? fields;
    private readonly string fieldAccessFailure = "";
    internal object FieldShape => new { bound = this.fields is not null, failure = this.fieldAccessFailure };

    private ArtiControlInputHooks? hooks;
    private bool hooksInstallAttempted;
    private int nextRunId = 1;

    // Per-request state (game thread only).
    private Phase phase = Phase.Idle;
    private MenuStepCommandRequest? request;
    private int runId;
    private string directionName = "";
    private BgDirection direction;
    private BgBindingTable? bindings;
    private BgButton syntheticKey;
    private IReadOnlyList<SButton> suppress = Array.Empty<SButton>();
    private BgInputState? retainedSnapshot;
    private BgInputState? historyBeforeN0;
    private BgInputState? n0Frame;
    private MouseState n0MouseRaw;
    private ActionProxy preN0Proxy;
    private int preHConsumed;
    private BgHistoryBranch preHBranch = BgHistoryBranch.Unpinned;
    private BgStepResult? n0Result;
    private bool actionEdgeObserved;
    private readonly List<Capture> captures = new();
    private BgMoveFramePlan? plan;
    private MoveSegmentGuard? segmentGuard; // fix89D / ADR 158: position check while a segment hold runs
    // fix92A / ADR 162: a guarded path (segment_stop + path_next) turns at reached stops inside one request.
    private IReadOnlyList<MoveSegmentStop>? pathStops; // the stops after the first segment
    private int pathIndex;          // stops of pathStops already turned toward
    private int pathSegmentStartH;  // plan ConsumedH when the current segment began (guard counts per segment)
    private int pathReached;        // segments that ended on SEGMENT_STOP_REACHED
    private int pathStartHealth;
    private bool pathEnded;         // no further turn in this request (sticky)
    private string? pathEnd;        // why the path ended (report)
    private BgHistoryInvariant? invariant;
    private MouseState baselineMouseRaw;
    private Capture startCapture;
    private Capture? nCapture;
    private int stableAfterN;
    private bool neutralVerified;
    private long phaseStartedMs;
    private long lastConsumedMs;
    private int phaseTicks;
    private string? releaseReason;
    /// <summary>ADR 246 review B2: the named harm a generic press's pin re-check found after admission (null = none).</summary>
    private string? holdHarmGone;
    private readonly HashSet<string> signaled = new(StringComparer.Ordinal);
    private bool dispatchImpossible;
    private string? divergence;
    private BackgroundMoveTerminal? terminal;

    // Pre-consumer frame token.
    private long updateSeq;
    private long tokenSeq = -1;
    private bool tokenConsumed = true;
    // In-flight guard only (duplicate-dispatch prevention while Parent runs). NOT consumption.
    private bool tokenInFlight;
    private bool substitutedThisTick;
    private string? lastHookReceipt;
    // Live7 rehearsal 9: this tick's stop / deadline / lease view, taken once in UpdateTicking.
    // VERIFY-role hooks judge their frame with it (never a fresh clock / STOP / lease read).
    private BgTickStopSnapshot? tickStop;

    private BackgroundContainmentPolicy? policy;

    // Stage 2 hold state (game thread only). Null holdKind = movement request.
    private BgHoldKind? holdKind;
    private BgHoldFramePlan? holdPlan;
    private BgPressSession? pressSession;
    private Keys holdXnaKey = Keys.None;
    private List<BgButton> nrSuppress = new();
    private HoldPins holdPins;
    private StardewValley.Objects.Chest? holdChest;
    private bool holdSettling;
    private BackgroundTerminalKind? holdExitKind;
    private string? holdExitLabel;
    private long holdSettleStartMs;
    private bool keyAbsentReceipt;
    private int usingToolRisingEdges;
    private bool lastUsingTool;
    private bool pinnedMenuObserved;
    private bool menuOpenedNoCallback;
    private object? observedMenu;
    private Capture? lastObservationCapture;
    // m3 / R3: menu-open debt after an ACTION terminal (survives the request).
    private readonly BgChestMenuDebt chestDebt = new();
    private StardewValley.Objects.Chest? debtChest;

    public BackgroundMoveRuntime(
        IModHelper helper,
        IMonitor monitor,
        NativeActionOwner owner,
        BackgroundContainmentWatchdog watchdog,
        string runtimeInstanceId,
        Func<CommandInboxIdentity> identity,
        Func<bool>? farmToolsEnabled = null,
        IPadModeHost? padHost = null,
        Func<bool>? eventControlEnabled = null)
    {
        this.helper = helper;
        this.padHost = padHost;
        this.eventControlEnabled = eventControlEnabled ?? (static () => false);
        this.port = new HelperPressPort(helper.Input, this.PadLedger);
        // WS1: ModConfig.BackgroundFarmToolsEnabled (default OFF); absent = OFF (Axe/Pickaxe only).
        this.farmToolsEnabled = farmToolsEnabled ?? (static () => false);
        this.monitor = monitor;
        this.owner = owner;
        this.watchdog = watchdog;
        this.runtimeInstanceId = runtimeInstanceId;
        this.identity = identity;

        // Never throws: an unavailable field only disables the optional background
        // capability; foreground gameplay and every other mod path are unaffected.
        this.fields = GameInputFieldAccess.TryCreate(helper.Reflection, out string fieldFailure);
        if (this.fields is null)
        {
            this.fieldAccessFailure = fieldFailure;
            monitor.Log($"background movement disabled: {fieldFailure} (Game1.hooks / Game1.inputSimulator not reachable via SMAPI reflection).",
                LogLevel.Warn);
        }
    }

    // C3 helpers: every inputSimulator ownership check re-reads the reflected field.
    // A read failure is never "owned" and never "empty" (fail closed).
    private bool LatchFieldHolds(object? expected) => this.fields?.InputSimulatorIs(expected) == true;

    private bool LatchFieldEmpty() => this.fields?.InputSimulatorIsNull() == true;

    /// <summary>A background request is in flight (its inbox slot is held).</summary>
    public bool RequestActive => this.request is not null;

    public static bool IsBackgroundMoveRequest(in MenuStepCommandRequest request) =>
        string.Equals(request.Op, MenuStepCommandInbox.OpInputOnce, StringComparison.Ordinal)
        && string.Equals(request.Family, MenuStepCommandInbox.FamilyWorldKey, StringComparison.Ordinal)
        && string.Equals(request.Primitive, MenuStepCommandInbox.PrimitiveMoveCardinal, StringComparison.Ordinal);

    // ------------------------------------------------------------------
    // Admission (game thread, from the existing inbox arm path).
    // ------------------------------------------------------------------

    /// <summary>
    /// Admit one input_once move_cardinal request for background execution. Returns a
    /// Rejected terminal on refusal (nothing mutated), or null when N-R has started.
    /// </summary>
    public BackgroundMoveTerminal? TryArm(MenuStepCommandRequest req) => this.TryArmCore(req, allowThrough: false);

    private BackgroundMoveTerminal? TryArmCore(MenuStepCommandRequest req, bool allowThrough, bool reflexStep = false)
    {
        int id = this.nextRunId++;
        string dirName = (req.Direction ?? "").Trim().ToLowerInvariant();
        BackgroundMoveTerminal Reject(string label)
        {
            this.eventAdmissionTarget = null;
            this.padJustTook = false;  // FIXB16 review: a take whose request was rejected never settles a later request
            return new(BackgroundTerminalKind.Rejected, req, label, 0, dirName, id);
        }

        if (this.ExecutionSlot?.Blocks(req) != false || this.phase != Phase.Idle)
            return Reject("COMMAND_BUSY");
        this.executionAttempt = this.ExecutionSlot.ActiveAttempt;
        if (req.EventToken is not null && !this.PinnedEventValid(req))
            return Reject(BgReason.PreconditionUnavailable("EVENT_CONTROL_CHANGED"));
        // WS-G5: a reflex step is a D-pad move of the in-flight reflex_combat request (pad path only).
        if (!IsBackgroundMoveRequest(req) && !(allowThrough && MenuStepCommandInbox.IsMoveThrough(req))
            && !(reflexStep && MenuStepCommandInbox.IsReflexCombat(req)))
            return Reject(BgReason.PreconditionUnavailable("UNSUPPORTED_PRIMITIVE"));
        if (reflexStep && !this.PadPathOn)
            return Reject(BgReason.PreconditionUnavailable(BgReflexRuntime.ReasonPadPathOff));
        if (this.fields is not { } fieldAccess)
            return Reject(BgReason.PreconditionUnavailable(ReasonFieldAccessUnavailable + ":" + this.fieldAccessFailure));
        if (!BackgroundInputNormalizer.TryParseDirection(dirName, out BgDirection dir))
            return Reject(BgReason.PreconditionUnavailable("INVALID_DIRECTION"));
        // fix89D / ADR 158: only a plain move_cardinal with a segment stop may hold past 60 frames.
        bool segmentHold = req.SegmentStop is not null && req.EventToken is null && IsBackgroundMoveRequest(req)
            && !MenuStepCommandInbox.IsMoveThrough(req);
        if (req.SegmentStop is not null && !segmentHold)
            return Reject(BgReason.PreconditionUnavailable(MoveSegmentGuard.ReasonInvalid));
        // fix92A / ADR 162: a path rides only on a segment hold of the pad VERIFY path (checked below once `pad` is known).
        bool pathHold = segmentHold && req.PathNext is not null;
        if (req.PathNext is not null && !pathHold)
            return Reject(BgReason.PreconditionUnavailable(MoveSegmentGuard.ReasonInvalid));
        if (req.Ticks < BgMoveFramePlan.MinTicks
            || req.Ticks > (pathHold ? MovePath.MaxTicks : segmentHold ? MoveSegmentGuard.MaxTicks : BgMoveFramePlan.MaxTicks))
            return Reject(BgReason.PreconditionUnavailable("INVALID_TICKS"));
        if (Game1.options is not Options options)
            return Reject(BgReason.PreconditionUnavailable(BgReason.BindingsIncomplete));

        if (!BgBindingTable.TryCreate(BackgroundInputNormalizer.EnumerateBindings(options), out BgBindingTable? table, out string? tableReason)
            || table is null)
            return Reject(BgReason.PreconditionUnavailable(tableReason ?? BgReason.BindingsIncomplete));

        bool chainAdmitted = this.hooks is not null
            ? this.hooks.IsExactChain()
            : !this.hooksInstallAttempted
                && fieldAccess.TryReadHooks(out ModHooks? currentHooks)
                && ArtiControlInputHooks.IsAdmissibleSmapiChain(currentHooks, out _);
        bool latchFieldAdmitted = fieldAccess.InputSimulatorIsNull();
        bool bindingValid = BindingValidFor(req) && this.LeaseValidFor(req, NowUnixMs()) && this.NativeRouteValid(req);

        // WS-G0: with gamepad input on, a move is a VERIFY-role stick hold (one SMAPI Press of the
        // left-stick direction); every keyboard key counts as bound for neutralization.
        // ADR-23 Q14: a barrier-opening crossing (warp/door/bed) rides the proven seat path, never pad ForceOn.
        bool pad = this.PadPathOn && !MenuStepCommandInbox.IsSeatCrossing(req);
        if (pathHold && !pad)
            return Reject(BgReason.PreconditionUnavailable(MoveSegmentGuard.ReasonInvalid + ":PATH_NEEDS_PAD"));
        BgStaleNeutralizePlan nPlan;
        if (pad)
        {
            if (this.PadAdmission(req, id, BgPadButtons.MovePin(dir), null, bindingValid, chainAdmitted, latchFieldAdmitted,
                    out BgAdmissionFacts padFacts) is string padRefusal)
                return Reject(padRefusal);
            table = table.ForHoldNeutralization();
            nPlan = BgStaleNeutralize.PlanForPadKey(table, padFacts, BgPadButtons.MovePin(dir), this.SampleRetained(true), this.SampleHistory(true));
        }
        else
        {
            BgAdmissionFacts facts = SampleAdmissionFacts(bindingValid, chainAdmitted, latchFieldAdmitted);
            nPlan = BgStaleNeutralize.Plan(table, facts, dir, this.SampleRetained(false), this.SampleHistory(false));
        }
        if (!nPlan.Admitted)
            return Reject(nPlan.Label ?? BgReason.PreconditionUnavailable("ADMISSION"));

        List<SButton> suppressButtons = new();
        foreach (BgButton b in nPlan.SuppressRetained)
        {
            if (BackgroundInputNormalizer.TryToSButton(b, out SButton s))
                suppressButtons.Add(s);
        }

        if (this.hooks is null)
        {
            this.hooksInstallAttempted = true;
            this.hooks = ArtiControlInputHooks.TryInstall(this, fieldAccess, out string installFailure);
            if (this.hooks is null)
                return Reject(BgReason.PreconditionUnavailable(installFailure));
            this.monitor.Log("background: ArtiControlInputHooks installed (ARTI -> SModHooks -> vanilla ModHooks).", LogLevel.Info);
        }

        BackgroundContainmentPolicy newPolicy = new(BgContainmentLimits.FrozenV4);
        if (!this.watchdog.TryPublish(newPolicy, req.RequestId ?? "", this.runtimeInstanceId))
            return Reject("COMMAND_BUSY");

        this.ResetRequestState();
        this.padTakeSettleTicks = pad && this.padJustTook ? PadTakeSettleTicks : 0;  // FIXB16 D-B14b-2
        this.padJustTook = false;
        this.policy = newPolicy;
        this.request = req;
        this.ExecutionSlot.AttachClient(req, this, this.RequestRelease, busy: () => this.Busy);
        this.runId = id;
        this.directionName = dirName;
        this.direction = dir;
        this.bindings = table;
        this.syntheticKey = nPlan.SyntheticKey;
        this.suppress = suppressButtons;
        if (pad)
        {
            // WS-G0: one session, one pinned stick direction, armed before its single Press (R2).
            this.padRequest = true;
            this.nrSuppress = nPlan.SuppressRetained.ToList();
            this.pressSession = new BgPressSession(nPlan.SyntheticKey, newPolicy, this.port, BackgroundContainmentWatchdog.NowMs,
                Context.IsMainPlayer, Context.IsSplitScreen, req.RequestId ?? "", this.runtimeInstanceId);
        }
        this.EnterPhase(Phase.NeutralizeRetained);
        this.ApplySuppress();
        this.monitor.Log(
            $"background ARM request_id={req.RequestId} run={id} dir={dirName} ticks={req.Ticks} key={nPlan.SyntheticKey} pad={pad} "
                + $"N-R suppress=[{string.Join(",", nPlan.SuppressRetained)}] unbound_ignored=[{string.Join(",", nPlan.UnboundIgnored)}]",
            LogLevel.Info);
        return null;
    }

    public int RunId => this.runId;

    // ------------------------------------------------------------------
    // Stage 2 admission (game thread, from the existing inbox dispatch sites).
    // ------------------------------------------------------------------

    /// <summary>An owned press override for a Stage 2 hold is not proved released.</summary>
    public bool PressOverrideOutstanding => this.pressSession?.Outstanding == true;

    /// <summary>A background use_tool (TOOL hold) request is in flight (gather last_use shows no verdict meanwhile).</summary>
    public bool ToolHoldInFlight => this.request is not null && this.holdKind == BgHoldKind.Tool;

    /// <summary>m3 / R3: refuse non-container requests while the pinned chest menu debt holds.</summary>
    public bool MenuDebtActive => this.chestDebt.Active;
    public string? MenuDebtId => this.chestDebt.DebtId;
    public bool HasMenuDebt(string attemptId) => this.chestDebt.Active && this.chestDebt.OpenedBy == attemptId;
    public string? ObservedChestAttempt => this.chestDebt.ObservedEffect.Attempt;
    public (string? Attempt, long Frame) ObservedChestEffect => this.chestDebt.ObservedEffect;
    public object[] MenuDebts => this.chestDebt.Snapshot;
    public bool MenuDebtDisallows(MenuStepCommandRequest request) => this.chestDebt.Active && !this.chestDebt.Allows(request);
    public string? MenuDebtRefusal(string op) => this.chestDebt.AllowsOp(op) ? null : "BackgroundChestMenuDebt";

    public Dictionary<string, object>? ChestMenuInputFence(IClickableMenu? menu) =>
        this.chestDebt.InputFence(IsPinnedChestMenu(menu, this.debtChest));

    /// <summary>
    /// ContainerActorEligible background term: the active menu is the ItemGrabMenu of the
    /// chest pinned by the last background ACTION (by reference), or that menu closed
    /// during this tick's own container command (debt refreshed every UpdateTicked).
    /// </summary>
    public bool PinnedChestMenuEligible(IClickableMenu? menu) =>
        this.debtChest is not null && this.chestDebt.ContainerEligible(IsPinnedChestMenu(menu, this.debtChest), menu is null);

    public static bool IsPinnedChestMenu(IClickableMenu? menu, StardewValley.Objects.Chest? chest) =>
        chest is not null && menu is ItemGrabMenu m && m.source == ItemGrabMenu.source_chest
        && ReferenceEquals(m.context, chest) && ReferenceEquals(m.sourceItem, chest);

    /// <summary>2.1 section 3 / M6: exact Axe / Pickaxe runtime type only.</summary>
    public static bool CurrentToolWhitelisted() =>
        Context.IsWorldReady && Game1.player?.CurrentTool is { } tool && BgHoldAdmission.ToolWhitelisted(tool.GetType().FullName);

    /// <summary>
    /// WS1: the held tool's admitted kind (BgToolClass), or null. Axe/Pickaxe always; basic
    /// Hoe / WateringCan and the (W)47 Scythe only with <paramref name="farmToolsEnabled"/>.
    /// </summary>
    public static BgToolKind? CurrentToolKind(bool farmToolsEnabled) =>
        Context.IsWorldReady && BackgroundToolTileProbe.SampleTool(Game1.player) is { } facts
            ? BgToolClass.Classify(facts, farmToolsEnabled)
            : null;

    /// <summary>Read-only CursorInert (2.1 section 2 / N6) for one hold kind (default the strict TOOL rule); null = inert.</summary>
    public static string? CursorInertReason(BgHoldKind kind = BgHoldKind.Tool) => BgCursorInert.Reject(SampleCursorFacts(), kind);

    public static bool IsBackgroundHoldRequest(in MenuStepCommandRequest request, BgHoldKind kind) =>
        kind == BgHoldKind.Tool ? MenuStepCommandInbox.IsUseTool(request) : MenuStepCommandInbox.IsWorldInteraction(request);

    /// <summary>
    /// Admit one inactive use_tool (TOOL) / world_interact (ACTION) request. Returns a
    /// Rejected terminal on refusal (nothing pressed, nothing written), or null once
    /// N-R has started. The caller already matched the request target with the
    /// current pinned tool / chest target.
    /// </summary>
    public BackgroundMoveTerminal? TryArmHold(MenuStepCommandRequest req, BgHoldKind kind, StardewValley.Objects.Chest? chest) =>
        this.TryArmHoldTarget(req, kind, chest is null ? null : BgActionTarget.ForChest(chest));

    private BackgroundMoveTerminal? TryArmHoldCore(MenuStepCommandRequest req, BgHoldKind kind, StardewValley.Objects.Chest? chest,
        BgActionTarget? target, BgReflexPressKind? reflex = null)
    {
        int id = this.nextRunId++;
        BackgroundMoveTerminal Reject(string label)
        {
            this.eventAdmissionTarget = null;
            this.padJustTook = false;  // FIXB16 review: a take whose request was rejected never settles a later request
            return new(BackgroundTerminalKind.Rejected, req, label, 0, "", id);
        }

        if (this.ExecutionSlot?.Blocks(req) != false || this.phase != Phase.Idle)
            return Reject("COMMAND_BUSY");
        this.executionAttempt = this.ExecutionSlot.ActiveAttempt;
        // WS-G4/G5: a reflex press is a TOOL hold of its own reflex request (fish / combat / charge) on the pad path only.
        if (reflex is { } reflexOf ? kind != BgHoldKind.Tool || !BgReflexRuntime.RequestMatches(reflexOf, req) : !IsBackgroundHoldRequest(req, kind))
            return Reject(BgReason.PreconditionUnavailable("UNSUPPORTED_PRIMITIVE"));
        if (kind == BgHoldKind.Action && target is null)
            return Reject(BgReason.PreconditionUnavailable("NO_PINNED_TARGET"));
        this.eventAdmissionTarget = target;
        if (this.fields is not { } fieldAccess)
            return Reject(BgReason.PreconditionUnavailable(ReasonFieldAccessUnavailable + ":" + this.fieldAccessFailure));
        if ((reflex is { } reflexKind ? BgReflexPress.ValidateTicks(reflexKind, req.Ticks) : BgHoldAdmission.ValidateTicks(kind, req.Ticks))
                is string tickReason)
            return Reject(BgReason.PreconditionUnavailable(tickReason));
        if (reflex is not null && !this.PadPathOn)
            return Reject(BgReason.PreconditionUnavailable(BgReflexRuntime.ReasonPadPathOff));
        if (Game1.options is not Options options)
            return Reject(BgReason.PreconditionUnavailable(BgReason.BindingsIncomplete));
        if (!BgBindingTable.TryCreate(BackgroundInputNormalizer.EnumerateBindings(options), out BgBindingTable? table, out string? tableReason)
            || table is null)
            return Reject(BgReason.PreconditionUnavailable(tableReason ?? BgReason.BindingsIncomplete));
        // WS-G0: the pad path pins the fixed pad button (TOOL = X, ACTION = A; G1:12672-12688);
        // the keyboard path keeps the TOOL_ONLY / ACTION_ONLY derivation unchanged. ADR-23 Q14: a door crossing rides the seat.
        bool pad = this.PadPathOn && (reflex is not null || !MenuStepCommandInbox.IsSeatCrossing(req));
        BgButton key;
        Keys xnaKey = Keys.None;
        if (pad)
        {
            // WS-G6: an extras target may pin pad X (milk / shear / drop-in / place, PLAN 1.4 X rows).
            key = kind == BgHoldKind.Action && target is { UsePadX: true } ? BgButton.Pad(BgPadButtons.X) : BgPadButtons.ForKind(kind);
        }
        else
        {
            if (!BgHoldBindings.TryDerive(table, kind, out key, out string? keyReason))
                return Reject(BgReason.PreconditionUnavailable(keyReason ?? BgHoldReason.NoKeyboardCandidate));
            if (!Enum.TryParse(key.Name, out xnaKey) || xnaKey == Keys.None)
                return Reject(BgReason.PreconditionUnavailable(BgHoldReason.NoKeyboardCandidate));
        }
        BgToolKind? toolKind = null;
        if (reflex is not null)
        {
            // WS-G4: the rod replaces the tool classification and the faced tool-tile guard (the reflex admission
            // checked the water landing tile); nothing else of the TOOL path is skipped. WS-G5: a melee weapon (swing) or
            // an upgraded hoe / can (charge; the reflex admission checked the faced tile and the area) the same way.
            if (BgReflexRuntime.HeldToolRefusal(reflex.Value) is string held)
                return Reject(BgReason.PreconditionUnavailable(held));
        }
        else if (kind == BgHoldKind.Tool)
        {
            // WS1: classification replaces the type-name whitelist; then the per-kind tick cap and
            // the faced tool-tile guard, both before CursorInert and before anything is pressed.
            toolKind = CurrentToolKind(this.farmToolsEnabled());
            if (toolKind is not BgToolKind tk)
                return Reject(BgReason.PreconditionUnavailable(BgHoldReason.ToolNotWhitelisted));
            if (BgHoldAdmission.ValidateToolKindTicks(tk, req.Ticks) is string kindTicks)
                return Reject(BgReason.PreconditionUnavailable(kindTicks));
            BgToolTileFacts? tileFacts = BackgroundToolTileProbe.SampleTileFacts(Game1.player, tk, furnitureAtToolTile: pad);
            if (BgToolTileGuard.Reject(tk, tileFacts) is string toolTile)
            {
                this.monitor.Log(
                    $"background HOLD tool-tile guard refused request_id={req.RequestId} toolKind={tk} reason={toolTile} "
                        + (tileFacts is null ? "facts=unreadable" : $"toolTile=({tileFacts.ToolTileX},{tileFacts.ToolTileY}) "
                            + $"playerTile=({tileFacts.PlayerTileX},{tileFacts.PlayerTileY}) facing={tileFacts.Facing} "
                            + $"adjacent={tileFacts.Adjacent} object={tileFacts.ObjectQualifiedItemId ?? "-"} terrain={tileFacts.TerrainType ?? "-"}"),
                    LogLevel.Info);
                return Reject(BgReason.PreconditionUnavailable(toolTile));
            }
        }
        // ADR 246: the named harms of a generic world press (an object handed to a villager, a stack shipped, a crop,
        // sapling or fruit tree destroyed) are refused here, in the one hold admission every caller of a generic press
        // passes, before anything is pressed. The facts are the game's own (GenericHarmProbe).
        if (kind == BgHoldKind.Action && reflex is null && target is { Kind: BgActionKind.Generic } generic
            && GenericHarm.Refusal(GenericHarmProbe.Sample(Game1.player, generic.UsePadX)) is string harm)
        {
            this.monitor.Log($"background HOLD generic harm refused request_id={req.RequestId} use={generic.UsePadX} reason={harm} "
                + "(" + GenericHarm.Words(harm) + ")", LogLevel.Info);
            return Reject(BgReason.PreconditionUnavailable(harm));
        }
        if (!pad && CursorInertReason(kind) is string cursor)
            return Reject(BgReason.PreconditionUnavailable(cursor));
        if (pad && kind == BgHoldKind.Action && FurnitureAtGrabTileReason(target?.ExtraKind == WorldExtras.Tv) is string grabFurniture)
            return Reject(BgReason.PreconditionUnavailable(grabFurniture));

        bool chainAdmitted = this.hooks is not null
            ? this.hooks.IsExactChain()
            : !this.hooksInstallAttempted
                && fieldAccess.TryReadHooks(out ModHooks? currentHooks)
                && ArtiControlInputHooks.IsAdmissibleSmapiChain(currentHooks, out _);
        bool latchFieldAdmitted = fieldAccess.InputSimulatorIsNull();
        bool bindingValid = BindingValidFor(req) && this.LeaseValidFor(req, NowUnixMs()) && this.NativeRouteValid(req);

        // Every keyboard key counts as bound for hold neutralization (VERIFY exactness).
        BgBindingTable holdTable = table.ForHoldNeutralization();
        BgStaleNeutralizePlan nPlan;
        if (pad)
        {
            // WS-G0: gamepad mode is taken here (whole inactive session), then the cursor-hidden gate.
            if (this.PadAdmission(req, id, key, kind, bindingValid, chainAdmitted, latchFieldAdmitted, out BgAdmissionFacts padFacts, reflex) is string padRefusal)
                return Reject(padRefusal);
            nPlan = BgStaleNeutralize.PlanForPadKey(holdTable, padFacts, key, this.SampleRetained(true), this.SampleHistory(true));
        }
        else
        {
            BgAdmissionFacts facts = SampleAdmissionFacts(bindingValid, chainAdmitted, latchFieldAdmitted);
            nPlan = BgStaleNeutralize.PlanForKey(holdTable, facts, key, this.SampleRetained(false), this.SampleHistory(false));
        }
        if (!nPlan.Admitted)
            return Reject(nPlan.Label ?? BgReason.PreconditionUnavailable("ADMISSION"));

        List<SButton> suppressButtons = new();
        foreach (BgButton b in nPlan.SuppressRetained)
        {
            if (BackgroundInputNormalizer.TryToSButton(b, out SButton s))
                suppressButtons.Add(s);
        }

        if (this.hooks is null)
        {
            this.hooksInstallAttempted = true;
            this.hooks = ArtiControlInputHooks.TryInstall(this, fieldAccess, out string installFailure);
            if (this.hooks is null)
                return Reject(BgReason.PreconditionUnavailable(installFailure));
            this.monitor.Log("background: ArtiControlInputHooks installed (ARTI -> SModHooks -> vanilla ModHooks).", LogLevel.Info);
        }

        BackgroundContainmentPolicy newPolicy = new(BgContainmentLimits.FrozenV4);
        if (!this.watchdog.TryPublish(newPolicy, req.RequestId ?? "", this.runtimeInstanceId))
            return Reject("COMMAND_BUSY");

        this.ResetRequestState();
        this.padTakeSettleTicks = pad && this.padJustTook ? PadTakeSettleTicks : 0;  // FIXB16 D-B14b-2
        this.padJustTook = false;
        this.policy = newPolicy;
        this.request = req;
        this.ExecutionSlot.AttachClient(req, this, this.RequestRelease, busy: () => this.Busy);
        this.runId = id;
        this.directionName = "";
        this.bindings = holdTable;
        this.syntheticKey = key;
        this.suppress = suppressButtons;
        this.nrSuppress = nPlan.SuppressRetained.ToList();
        this.holdKind = kind;
        this.holdXnaKey = xnaKey;
        this.padRequest = pad;
        this.reflexPress = reflex;
        this.holdPins = CaptureHoldPins();
        this.holdChest = chest;
        this.holdTarget = kind == BgHoldKind.Action ? target : null;
        this.holdHarmGone = null;
        this.eventAdmissionTarget = null;
        // Sole issuer: one session, one pinned key, armed before its single Press (R2).
        this.pressSession = new BgPressSession(key, newPolicy, this.port, BackgroundContainmentWatchdog.NowMs,
            Context.IsMainPlayer, Context.IsSplitScreen, req.RequestId ?? "", this.runtimeInstanceId);
        this.EnterPhase(Phase.NeutralizeRetained);
        this.ApplySuppress();
        this.monitor.Log(
            $"background HOLD ARM request_id={req.RequestId} run={id} kind={kind} toolKind={toolKind?.ToString() ?? "-"} target={this.HoldActionKind} ticks={req.Ticks} key={key} pad={pad} reflex={reflex?.ToString() ?? "-"} pins={this.holdPins} "
                + $"N-R suppress=[{string.Join(",", nPlan.SuppressRetained)}]",
            LogLevel.Info);
        return null;
    }

    /// <summary>STOP / lease / caller loss / deadline: steer toward R/N only. Never disarms containment.</summary>
    public void RequestRelease(string reason)
    {
        if (this.request is null)
            return;
        reason = reason switch { "stop" => "STOP_LATCHED", "lease" => "LEASE_INVALID", _ => reason };
        this.releaseReason ??= reason;
        if (this.phase == Phase.NeutralizeRetained)
        {
            // No latch, no ARTI frame yet: nothing to release. W1 B2: a named harm that withheld it is a refusal, not
            // an uncertain outcome (nothing was pressed, so nothing can have changed by her hand).
            if (GenericHarm.IsHarmGone(reason))
                this.Finish(BackgroundTerminalKind.Rejected, BgReason.PreconditionUnavailable(reason));
            else
                this.Finish(BackgroundTerminalKind.Uncertain, "RELEASED_BEFORE_LATCH:" + reason);
        }
    }

    /// <summary>ReturnedToTitle: ModEntry writes the existing interrupted result. Containment stays armed.</summary>
    public void AbortForLifecycle(string reason)
    {
        // Stage 2: an owned press override is released on this exit path too (2.2 N2).
        this.pressSession?.ForceRelease("LIFECYCLE:" + reason);
        this.chestDebt.Clear();
        this.debtChest = null;
        if (this.latch is not null)
            this.RemoveLatch();
        if (this.request is not null)
            this.monitor.Log($"background aborted by lifecycle {reason}; request_id={this.request.Value.RequestId}", LogLevel.Warn);
        this.request = null;
        this.RestoreOwnerInput(); // review R5 S1
        this.tickStop = null;
        this.terminal = null;
        this.phase = Phase.Idle;
    }

    public BackgroundMoveTerminal? TakeTerminal()
    {
        BackgroundMoveTerminal? t = this.terminal;
        this.terminal = null;
        return t;
    }

    // ------------------------------------------------------------------
    // Hook entry (game thread, inside Game1.UpdateControlInput).
    // ------------------------------------------------------------------

    private void ServePreH(ArtiControlInputHooks from, ref KeyboardState k, ref MouseState m, ref GamePadState p, Action action)
    {
        KeyboardState kNow = k;
        MouseState mNow = m;
        GamePadState pNow = p;
        string? diverged;
        KeyboardState sk = default;
        MouseState sm = default;
        BgHistoryBranch observed = BgHistoryBranch.Unpinned;
        try
        {
            diverged = this.CheckPreH(from, kNow, mNow, pNow, out observed);
            if (diverged is null)
            {
                sk = BackgroundInputNormalizer.ToKeyboard(this.n0Frame!);
                sm = BackgroundInputNormalizer.ToMouse(this.n0Frame!, this.n0MouseRaw);
            }
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

        k = sk;
        m = sm;
        p = new GamePadState();
        this.tokenInFlight = true;
        bool padNeutralBefore = BgPadNeutralTickGate.IsNeutral(this.SampleHistory());
        // C2: nothing is credited until Parent returns; a throwing Parent gets zero credit.
        BackgroundFrameCommit.InvokeThenCommit(
            ref k, ref m, ref p,
            (ref KeyboardState kk, ref MouseState mm, ref GamePadState pp) => from.InvokeParent(ref kk, ref mm, ref pp, action),
            () => this.CommitPreH(observed, padNeutralBefore),
            () => this.OnParentFault(this.preHConsumed == 0 ? "N0" : "PRE_H_NEUTRAL"),
            ex => this.OnCommitFault(ex));
    }

    private void CommitPreH(BgHistoryBranch observed, bool padNeutralBefore)
    {
        this.tokenInFlight = false;
        // PLAN 1.5 d: a pre-H UpdateControlInput tick with no ARTI Press outstanding and a neutral oldPadState.
        this.padNeutralTick.Observe(this.PadLedger.Outstanding.Count > 0, padNeutralBefore);
        if (this.preHBranch == BgHistoryBranch.Unpinned && observed != BgHistoryBranch.Unpinned)
            this.preHBranch = observed;
        this.preHConsumed++;
        this.tokenConsumed = true;
        this.substitutedThisTick = true;
        this.lastConsumedMs = BackgroundContainmentWatchdog.NowMs();
        this.policy?.OnFrameConsumed(this.lastConsumedMs);
        this.lastHookReceipt = (this.preHConsumed == 1 ? "N0" : "PRE_H_NEUTRAL") + " seq=" + this.updateSeq
            + " branch=" + this.preHBranch;
    }

    private string? CheckPreH(ArtiControlInputHooks from, KeyboardState k, MouseState m, GamePadState p, out BgHistoryBranch observed)
    {
        observed = BgHistoryBranch.Unpinned;
        if (!this.LatchFieldHolds(this.latch)) return "LATCH_NOT_OWNED";
        if (!from.IsExactChain()) return "HOOK_NOT_OWNED";
        if (this.tokenSeq != this.updateSeq || this.tokenConsumed || this.tokenInFlight) return "FRAME_TOKEN_MISMATCH";
        // W1 B2: a harm the pin re-check found (it also fails the binding) is the settled zero-press TARGET_GONE skip,
        // checked BEFORE the binding so the hook's exit label is the one BgHoldCompletion settles as a named rejection.
        if (this.holdHarmGone is not null) return BgHoldReason.TargetGone;
        if (!this.BindingValid()) return "REQUEST_BINDING_INVALID";
        if (!this.GatesValid()) return "GAME_GATE_CLOSED";
        BgInputState retainedNow = BackgroundInputNormalizer.Normalize(k, m, p, this.padRequest);
        if (!retainedNow.Equals(this.retainedSnapshot)) return BgReason.ForeignHistory + ":RETAINED_CHANGED";

        BgInputState historyNow = this.SampleHistory();
        if (this.preHConsumed == 0)
            return historyNow.Equals(this.historyBeforeN0) ? null : BgReason.ForeignHistory;
        bool isB = historyNow.Equals(this.historyBeforeN0);
        bool isS = historyNow.Equals(this.n0Frame);
        if (!isB && !isS) return BgReason.ForeignHistory;
        if (isB && isS) return null;
        observed = isS ? BgHistoryBranch.Yes : BgHistoryBranch.No;
        if (this.preHBranch != BgHistoryBranch.Unpinned && this.preHBranch != observed)
            return BgReason.ForeignHistory + ":BRANCH_SWITCH";
        return null;
    }

    /// <summary>
    /// Stage 2 VERIFY role. The retained frame is compared, in full, with the exact
    /// expected owned frame; on any difference Parent/action is skipped (dispatch
    /// impossible). On admission the consumer sees the ORIGINAL refs: ARTI
    /// substitutes nothing and writes no old* state at or after latch install.
    /// </summary>
    private void ServeHolding(ArtiControlInputHooks from, ref KeyboardState k, ref MouseState m, ref GamePadState p, Action action)
    {
        KeyboardState kNow = k;
        MouseState mNow = m;
        GamePadState pNow = p;
        BgHoldFramePlan hold = this.holdPlan!;
        BgHistoryInvariant inv = this.invariant!;
        bool suppressedBefore = this.pressSession?.SuppressIssued == true;
        string? diverged = null;
        BgHookDecision? decision = null;
        BgHookFacts? sampled = null;
        try
        {
            decision = BgHookEntry.Decide(() => sampled = this.SampleHoldHookFacts(from, kNow, mNow, pNow), hold, inv);
            switch (decision.Action)
            {
                case BgHookAction.SkipDivergence:
                    // W1b B2: the harm re-check (HoldTargetPresent, sampled above) fails the slot, so the binding test
                    // answers first; a found harm is the settled zero-press TARGET_GONE skip, named at the terminal.
                    if (this.holdHarmGone is not null && decision.Reason == "REQUEST_BINDING_INVALID")
                    {
                        diverged = BgHoldReason.TargetGone;
                        break;
                    }
                    diverged = decision.Reason ?? BgReason.PreconsumerHistoryDivergence;
                    diverged = NameForeignInput(diverged, this.LogVerifyMismatch(diverged, hold, inv, sampled, kNow, mNow, pNow));
                    break;
                case BgHookAction.PassThroughForeground:
                case BgHookAction.VerifyHeld:
                case BgHookAction.VerifyNeutral:
                    break;
                default:
                    diverged = BgReason.PreconsumerHistoryDivergence + ":NO_GRANT_UNDER_ARTI_LATCH";
                    break;
            }
            // A held frame is never consumed unless press containment is armed (R2).
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
            () => this.CommitHolding(hold, inv, admitted, suppressedBefore),
            () => this.OnParentFault(admittedFrame.ToString()),
            ex => this.OnCommitFault(ex));
    }

    private void CommitHolding(BgHoldFramePlan hold, BgHistoryInvariant inv, BgHookDecision decision, bool suppressedBefore)
    {
        this.tokenInFlight = false;
        BackgroundFrameCommit.CommitMovingFrame(hold, inv, decision);
        long now = BackgroundContainmentWatchdog.NowMs();
        this.tokenConsumed = true;
        this.substitutedThisTick = true;
        this.lastConsumedMs = now;
        this.policy?.OnFrameConsumed(now);
        // R1: a VERIFY-admitted neutral frame after Suppress is the key-absent receipt.
        if (decision.Frame != BgFrameKind.H && suppressedBefore)
            this.keyAbsentReceipt = true;
        this.lastHookReceipt = "VERIFY " + decision.Frame + " seq=" + this.updateSeq + " h=" + hold.ConsumedH + "/" + hold.TargetHFrames
            + " branch=" + inv.Branch + (decision.Reason is null ? "" : " reason=" + decision.Reason);
        // Verified consumer returned and H bookkeeping succeeded. Sample BEFORE Game1's
        // UpdateLocations can run end or another command (Game1.cs:4288,4315).
        if (decision.Frame == BgFrameKind.H && this.holdTarget?.EventScopeValid is not null)
        {
            this.holdTarget.SeatMotion?.Begin(now);
            this.ObserveActionEffect();
            this.monitor.Log($"background EVENT H effect run={this.runId} before="
                + System.Text.Json.JsonSerializer.Serialize(this.effectBefore?.Event)
                + " after=" + System.Text.Json.JsonSerializer.Serialize(this.effectNow?.Event), LogLevel.Debug);
        }
        this.EndInputConsumption();
    }

    private bool OwnMenuEnd => (this.holdPlan?.ConsumedH ?? 0) > 0
        && this.IsExpectedHoldReceiver(Game1.activeClickableMenu)
        && this.executionAttempt?.Validity is { } v && this.ReceiverEpoch?.Invoke() == v.ReceiverEpoch + 1;

    private void EndInputConsumption()
    {
        if (this.OwnMenuEnd) return;
        if (this.request is not { } req || this.SlotValid(req,
            this.BindingValidFor(req, requirePlayerFree: this.holdKind is null && !this.ThroughWarpStarted))) return;
        this.RequestRelease("foreign_epoch");
        this.pressSession?.ForceRelease("foreign_epoch"); // removal only, never dispatch button-up into a successor
        if (this.padRequest) PadHistoryHygiene.WriteNeutral();
    }

    private BgHookFacts SampleHoldHookFacts(ArtiControlInputHooks from, KeyboardState k, MouseState m, GamePadState p)
    {
        long nowUnix = NowUnixMs();
        MenuStepCommandRequest req = this.request!.Value;
        // VERIFY role: once the hold is in progress, StopLatched / LeaseValid are this tick's Ticking snapshot
        // (same view as the press decision). Before its first H the deadline / lease are the snapshot's too, while a
        // hook-time owner STOP / release still refuses the press (Live7 containment follow-up).
        return BgTickStopSnapshot.ApplyForTick(this.tickStop, req.RequestId, this.holdPlan?.ConsumedH ?? 0, new BgHookFacts(
            GameActive: Game1.game1.IsActive,
            LatchOwned: this.latch is not null && this.LatchFieldHolds(this.latch),
            HookOwned: from.IsExactChain(),
            FrameTokenSeq: this.tokenSeq,
            CurrentUpdateSeq: this.updateSeq,
            FrameTokenConsumed: this.tokenConsumed || this.tokenInFlight,
            HistoryNow: this.SampleHistory(),
            RetainedNow: BackgroundInputNormalizer.Normalize(k, m, p, this.padRequest),
            // Identity/session/player/location/screen/runtime/context only; player-free and
            // can-move are judged by the hold gate (strict before the press, relaxed after).
            RequestBindingValid: this.SlotValid(req,
                this.BindingValidFor(req, requirePlayerFree: false), this.HoldTargetPresent()),
            GameGatesValid: true,
            LeaseValid: this.LeaseValidFor(req, nowUnix),
            StopLatched: this.releaseReason is not null || this.owner.StopLatched || nowUnix > req.DeadlineUnixMs,
            Hold: this.SampleHoldGateFacts()), liveStopOrRelease: this.releaseReason is not null || this.owner.StopLatched);
    }

    private BgHoldHookFacts SampleHoldGateFacts() =>
        BgReflexPress.Relax(this.SampleHoldGateFactsStrict(), this.reflexPress, BgReflexRuntime.RodHeld(), (this.holdPlan?.ConsumedH ?? 0) > 0);

    private BgHoldHookFacts SampleHoldGateFactsStrict()
    {
        Farmer? player = Context.IsWorldReady ? Game1.player : null;
        HoldPins now = CaptureHoldPins();
        HoldPins pin = this.holdPins;
        IClickableMenu? menu = Game1.activeClickableMenu;
        return new BgHoldHookFacts(
            Kind: this.holdKind ?? BgHoldKind.Tool,
            IsMainPlayer: Context.IsMainPlayer,
            IsSplitScreen: Context.IsSplitScreen,
            WorldReady: Context.IsWorldReady && player?.currentLocation is not null,
            MenuOpen: menu is not null || Game1.dialogueUp,
            MenuIsPinnedChest: this.IsExpectedHoldReceiver(menu), // WS3b: chest menu or the npc / eat DialogueBox
            EventOrMinigame: !this.EventActionValid() && !(this.request is { } fish && this.FestivalFishingValid(fish))
                && (Game1.eventUp || Game1.CurrentEvent is not null || Game1.currentMinigame is not null),
            Chatting: Game1.chatBox?.isActive() == true,
            UsingTool: player?.UsingTool == true,
            UsingSlingshot: player?.usingSlingshot == true,
            Fishing: player?.CurrentTool is FishingRod rod && rod.inUse(),
            CanMove: player?.CanMove == true,
            Paused: Game1.paused || Game1.HostPaused,
            // The existing scope proves an exact consumed action's native end/continuation.
            // It never admits a foreign fade or a fresh H into an unowned cutscene.
            GlobalFade: (Game1.globalFade || Game1.fadeToBlack && !this.EventActionValid()) && !this.DoorWarpRelease(),
            FreezeControls: Game1.freezeControls,
            PanMode: Game1.panMode,
            GamepadMode: Game1.options?.gamepadControls == true,
            MovementDirections: MovementCount(),
            SlotUnchanged: now.Slot == pin.Slot,
            ToolItemUnchanged: string.Equals(now.ToolItem, pin.ToolItem, StringComparison.Ordinal),
            TileUnchanged: now.TileX == pin.TileX && now.TileY == pin.TileY,
            FacingUnchanged: now.Facing == pin.Facing,
            LocationUnchanged: string.Equals(now.Location, pin.Location, StringComparison.Ordinal),
            Cursor: SampleCursorFacts(),
            VillagerAtToolTile: this.holdKind == BgHoldKind.Tool && BackgroundToolTileProbe.VillagerAtToolPoint(player),

            TargetPresent: this.HoldTargetPresent(),
            PadMode: this.padRequest,
            CursorHidden: this.padRequest ? this.SamplePadCursorForRequest() : null,
            ToolUseAfterPress: this.holdKind == BgHoldKind.Action && this.holdTarget?.ToolUseAfterPress == true,
            PoseMayChangeAfterPress: this.holdKind == BgHoldKind.Action && this.holdTarget?.PoseMayChangeAfterPress == true,
            GenericWorldPress: this.holdKind == BgHoldKind.Action && this.holdTarget?.Kind == BgActionKind.Generic);
    }

    /// <summary>
    /// Read-only CursorInert facts (2.1 section 2 / N6) pinned to the installed Game1
    /// MVID; any other module, or any read fault, is "unreadable" (never inert).
    /// No cursor warp, no mouse-state write. Large terrain features use a conservative
    /// box around their tile (wider than the installed bush bounds; fail-closed).
    /// </summary>
    internal static BgCursorFacts? SampleCursorFacts()
    {
        try
        {
            if (GameModuleVersionId() != PinnedGameMvid)
                return null;
            Farmer? player = Context.IsWorldReady ? Game1.player : null;
            GameLocation? location = player?.currentLocation;
            if (player is null || location is null)
                return null;
            bool visible = Game1.wasMouseVisibleThisFrame && Game1.mouseCursorTransparency != 0f;
            int px = Game1.getOldMouseX() + Game1.viewport.X;
            int py = Game1.getOldMouseY() + Game1.viewport.Y;
            int cx = px / 64;
            int cy = py / 64;
            Vector2 cursorTile = new(cx, cy);
            Vector2 playerTile = player.Tile;
            int uiX = Game1.getMouseX(true);
            int uiY = Game1.getMouseY(true);
            bool overMenu = false;
            foreach (IClickableMenu onScreen in Game1.onScreenMenus)
            {
                if (onScreen is not null && onScreen.isWithinBounds(uiX, uiY))
                {
                    overMenu = true;
                    break;
                }
            }
            // Furniture only: beyond radius 1 the keyboard presses reach an object under the cursor only
            // through GameLocation.leftClick -> Object.clicked, which only Furniture overrides (decompiled
            // 1.6.15; live 2026-10-07 attempt 10z17: spring weeds made every parked cursor "not inert").
            bool hasObject = location.objects.TryGetValue(cursorTile, out var obj)
                && obj is StardewValley.Objects.Furniture;
            // HoeDirt only: the one terrain kind the game can act on through the cursor (see BgCursorInert).
            bool hasTerrain = location.terrainFeatures.TryGetValue(cursorTile, out var terrain)
                && terrain is StardewValley.TerrainFeatures.HoeDirt;
            bool hasLarge = location.largeTerrainFeatures.Any(f => f is not null
                && cx >= (int)f.Tile.X - 1 && cx <= (int)f.Tile.X + 3 && Math.Abs(cy - (int)f.Tile.Y) <= 1);
            bool hasClump = location.resourceClumps.Any(c => c is not null
                && cx >= (int)c.Tile.X && cx < (int)c.Tile.X + Math.Max(1, c.width.Value)
                && cy >= (int)c.Tile.Y && cy < (int)c.Tile.Y + Math.Max(1, c.height.Value));
            bool hasBuilding = location.buildings.Any(b => b is not null
                && cx >= b.tileX.Value && cx < b.tileX.Value + b.tilesWide.Value
                && cy >= b.tileY.Value && cy < b.tileY.Value + b.tilesHigh.Value);
            var underCursor = location.furniture.Where(f => f is not null && f.GetBoundingBox().Contains(px, py)).ToList();
            bool furniture = underCursor.Count > 0;
            // Walkable floor furniture only (rug = furniture_type 12, or isPassable()), holding nothing.
            bool allPassable = furniture && underCursor.All(f =>
                (f.furniture_type.Value == StardewValley.Objects.Furniture.rug || f.isPassable()) && f.heldObject.Value is null);
            return new BgCursorFacts(visible, cx, cy, (int)playerTile.X, (int)playerTile.Y, overMenu,
                hasObject, hasTerrain, hasLarge, hasClump, hasBuilding, furniture, allPassable);
        }
        catch
        {
            return null;
        }
    }

    private readonly record struct HoldPins(int Slot, string ToolItem, int TileX, int TileY, int Facing, string Location);

    private static HoldPins CaptureHoldPins()
    {
        Farmer? player = Context.IsWorldReady ? Game1.player : null;
        if (player is null)
            return new HoldPins(-1, "", -1, -1, -1, "");
        Vector2 tile = player.Tile;
        return new HoldPins(player.CurrentToolIndex, player.CurrentTool?.QualifiedItemId ?? "", (int)tile.X, (int)tile.Y,
            player.FacingDirection, player.currentLocation?.NameOrUniqueName ?? "");
    }

    private void ServeMoving(ArtiControlInputHooks from, ref KeyboardState k, ref MouseState m, ref GamePadState p, Action action)
    {
        if (this.plan?.Verify == true)
        {
            this.ServePadMoving(from, ref k, ref m, ref p, action); // WS-G0: VERIFY role, nothing substituted
            return;
        }
        KeyboardState kNow = k;
        MouseState mNow = m;
        GamePadState pNow = p;
        BgMoveFramePlan movePlan = this.plan!;
        BgHistoryInvariant inv = this.invariant!;
        KeyboardState sk = default;
        MouseState sm = default;
        string? diverged = null;
        BgHookDecision? decision = null;
        try
        {
            decision = BgHookEntry.Decide(() => this.SampleHookFacts(from, kNow, mNow, pNow), movePlan, inv);
            switch (decision.Action)
            {
                case BgHookAction.SkipDivergence:
                    diverged = decision.Reason ?? BgReason.PreconsumerHistoryDivergence;
                    break;
                case BgHookAction.PassThroughForeground:
                    break;
                case BgHookAction.PassThroughNoGrant:
                    // Latch owned + inactive but no grant: fail closed, never leak real input.
                    diverged = BgReason.PreconsumerHistoryDivergence + ":NO_GRANT_UNDER_ARTI_LATCH";
                    break;
                default:
                {
                    BgInputState substitute = decision.Substitute!;
                    BgFrameKind frame = decision.Frame!.Value;
                    sk = BackgroundInputNormalizer.ToKeyboard(substitute);
                    sm = BackgroundInputNormalizer.ToMouse(substitute, this.baselineMouseRaw);
                    long now = BackgroundContainmentWatchdog.NowMs();
                    if (frame == BgFrameKind.H && this.policy?.Armed != true)
                    {
                        // Containment is armed before the first H frame reaches the consumer.
                        if (this.policy is null
                            || !this.policy.TryArmForConsumedH(now, Context.IsMainPlayer, Context.IsSplitScreen,
                                this.request?.RequestId ?? "", this.runtimeInstanceId))
                        {
                            diverged = BgReason.PreconsumerHistoryDivergence + ":CONTAINMENT_NOT_ARMED";
                            break;
                        }
                    }
                    // C2: consumption bookkeeping is committed only after Parent returns.
                    break;
                }
            }
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

        if (decision?.Action == BgHookAction.PassThroughForeground)
        {
            from.InvokeParent(ref k, ref m, ref p, action);
            return;
        }

        BgHookDecision admitted = decision!;
        BgFrameKind admittedFrame = admitted.Frame!.Value;
        k = sk;
        m = sm;
        p = new GamePadState();
        this.tokenInFlight = true;
        BackgroundFrameCommit.InvokeThenCommit(
            ref k, ref m, ref p,
            (ref KeyboardState kk, ref MouseState mm, ref GamePadState pp) => from.InvokeParent(ref kk, ref mm, ref pp, action),
            () => this.CommitMoving(movePlan, inv, admitted),
            () => this.OnParentFault(admittedFrame.ToString()),
            ex => this.OnCommitFault(ex));
    }

    private void CommitMoving(BgMoveFramePlan movePlan, BgHistoryInvariant inv, BgHookDecision decision)
    {
        this.tokenInFlight = false;
        BackgroundFrameCommit.CommitMovingFrame(movePlan, inv, decision);
        long now = BackgroundContainmentWatchdog.NowMs();
        this.tokenConsumed = true;
        this.substitutedThisTick = true;
        this.lastConsumedMs = now;
        this.policy?.OnFrameConsumed(now);
        this.lastHookReceipt = decision.Frame + " seq=" + this.updateSeq + " h=" + movePlan.ConsumedH + "/" + movePlan.TargetHFrames
            + " branch=" + inv.Branch + (decision.Reason is null ? "" : " reason=" + decision.Reason);
        this.EndInputConsumption();
    }

    /// <summary>
    /// Parent/action threw on an admitted substituted frame (C2). Zero consumed credit;
    /// the request can never complete from it. Fail closed: no further frame is served
    /// (no neutral replacement that could manufacture an edge), the latch is removed on
    /// the next UpdateTicking, and containment is kept/armed conservatively because an
    /// H frame may have partially affected movement. Runs inside a finally while the
    /// exception propagates; it must not throw.
    /// </summary>
    private void OnParentFault(string frame)
    {
        try
        {
            this.tokenInFlight = false;
            this.dispatchImpossible = true;
            this.divergence ??= "UNCERTAIN(PARENT_CONSUMER_EXCEPTION:" + frame + ")";
            this.lastHookReceipt = "PARENT_FAULT frame=" + frame + " seq=" + this.updateSeq + " (not consumed)";
            long now = BackgroundContainmentWatchdog.NowMs();
            int md = MovementCount();
            if (md != 0 && this.policy?.Armed != true)
                this.policy?.TryArmForPreHUnresolved(now, Context.IsMainPlayer, Context.IsSplitScreen,
                    this.request?.RequestId ?? "", this.runtimeInstanceId);
            this.ReportDispatchImpossible("PARENT_FAULT", now, gameActive: false, md);
        }
        finally
        {
            this.phase = Phase.Closing;
            this.latchRemovalDue = true;
        }
    }

    /// <summary>Parent returned but bookkeeping failed: consumed-but-unrecorded, so UNCERTAIN, fail closed.</summary>
    private void OnCommitFault(Exception ex)
    {
        this.tokenInFlight = false;
        this.dispatchImpossible = true;
        this.divergence ??= "UNCERTAIN(CONSUMED_FRAME_COMMIT_FAULT:" + ex.GetType().Name + ")";
        this.lastHookReceipt = "COMMIT_FAULT seq=" + this.updateSeq;
        this.ReportDispatchImpossible("COMMIT_FAULT", BackgroundContainmentWatchdog.NowMs(), Game1.game1?.IsActive == true, MovementCount());
        this.phase = Phase.Closing;
        this.latchRemovalDue = true;
    }

    private BgHookFacts SampleHookFacts(ArtiControlInputHooks from, KeyboardState k, MouseState m, GamePadState p)
    {
        long nowUnix = NowUnixMs();
        MenuStepCommandRequest req = this.request!.Value;
        return new BgHookFacts(
            GameActive: Game1.game1.IsActive,
            LatchOwned: this.latch is not null && this.LatchFieldHolds(this.latch),
            HookOwned: from.IsExactChain(),
            FrameTokenSeq: this.tokenSeq,
            CurrentUpdateSeq: this.updateSeq,
            // In-flight is folded in only as "token unavailable"; it is never counted as consumed.
            FrameTokenConsumed: this.tokenConsumed || this.tokenInFlight,
            HistoryNow: this.SampleHistory(),
            RetainedNow: BackgroundInputNormalizer.Normalize(k, m, p, this.padRequest),
            // WS3a move_through: once the expected warp started the fade holds CanMove false;
            // only R/N remain, so player-free / can-move relax exactly like a hold after its press.
            RequestBindingValid: this.SlotValid(req, this.ThroughWarpStarted
                ? MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(req, this.identity(), Context.IsWorldReady, true, true, out _)
                : BindingValidFor(req)),
            GameGatesValid: this.NativeRouteValid(req) && (this.ThroughWarpStarted ? this.GatesValidIgnoringCanMove() : this.GatesValid()),
            LeaseValid: this.LeaseValidFor(req, nowUnix),
            StopLatched: this.releaseReason is not null || this.owner.StopLatched || nowUnix > req.DeadlineUnixMs);
    }

    /// <summary>
    /// Q1 (ADR 244 wording): the hook ran without the ARTI latch while an owned press was not yet
    /// suppressed (only an active window calls the hook without the latch). Parent is skipped for this
    /// frame; the request can only end uncertain. Must not throw (runs inside the hook).
    /// </summary>
    private void OnOwnedPressForegroundSkip()
    {
        const string reason = "OWNED_PRESS_UNSUPPRESSED_WITHOUT_LATCH";
        if (this.request is not null && this.phase == Phase.Holding)
        {
            this.OnDivergence(reason);
            return;
        }
        if (this.request is not null)
        {
            this.dispatchImpossible = true;
            this.divergence ??= BgReason.PreconsumerHistoryDivergence + ":" + reason;
        }
        this.lastHookReceipt = "SKIP " + reason + " seq=" + this.updateSeq;
    }

    private void OnDivergence(string detail)
    {
        string reason = detail.StartsWith(BgReason.PreconsumerHistoryDivergence, StringComparison.Ordinal)
            ? detail
            : BgReason.PreconsumerHistoryDivergence + ":" + detail;
        this.dispatchImpossible = true;
        this.divergence ??= reason;
        this.lastHookReceipt = "SKIP " + reason + " seq=" + this.updateSeq;
        long now = BackgroundContainmentWatchdog.NowMs();
        int md = MovementCount();
        bool preH = this.phase is Phase.AwaitN0 or Phase.PreHNeutral;
        if (preH && this.preHConsumed > 0 && md != 0 && this.policy?.Armed != true)
            this.policy?.TryArmForPreHUnresolved(now, Context.IsMainPlayer, Context.IsSplitScreen,
                this.request?.RequestId ?? "", this.runtimeInstanceId);
        // T2 is only requested for inactive unresolved movement (policy decides). ADR 244: the hook now also
        // runs with the window active; there SMAPI's TrueUpdate drops every ARTI override each tick, so an
        // active divergence cannot leave her walking and is reported as active (no T2).
        this.ReportDispatchImpossible("DIVERGENCE", now, Game1.game1?.IsActive == true, md);
        this.phase = Phase.Closing;
        this.latchRemovalDue = true;
    }

    // ------------------------------------------------------------------
    // SMAPI ticks (game thread).
    // ------------------------------------------------------------------

    /// <summary>Before SMAPI ApplyOverrides and Game1.Update: advisory checks, latch removal, frame token.</summary>
    public void OnUpdateTicking()
    {
        // WS-G0: the pad override ledger starts every tick here (an active tick's TrueUpdate already
        // cleared every earlier SMAPI override), before any ARTI Press/Suppress of this tick.
        try { this.PadLedger.BeginTick(Game1.game1.IsActive); } catch { this.PadLedger.BeginTick(false); }
        try
        {
            this.TickingCore();
        }
        catch (Exception ex)
        {
            bool pad = this.padRequest;
            this.FailClosed("TICKING_EXCEPTION:" + ex.GetType().Name);
            this.monitor.Log($"background ticking fault (fail closed): {ex}", LogLevel.Error);
            if (pad)
                this.PadPanicRequest ??= "TICKING_EXCEPTION"; // PLAN 1.5 e: an exception escaping a pad handler runs pad panic
        }
    }

    /// <summary>WS-G0: a pad-path fault asks ModEntry to run pad panic (consumed by ModEntry.Pad.cs).</summary>
    public string? PadPanicRequest { get; set; }

    private void TickingCore()
    {
        this.substitutedThisTick = false;
        this.updateSeq++;
        // W1 S2: a press that ended behind hands the owner's still-held buttons back on the first active idle tick.
        if (this.request is null && this.foreignSuppressed.Count > 0 && Game1.game1.IsActive)
            this.RestoreOwnerInput();
        if (this.request is null && this.latch is null && this.pressSession is null)
            return;
        if (!this.OwnMenuEnd && this.request is { } req && !this.SlotValid(req, this.ExecutionSlot?.Owns(req) == true,
            this.holdKind != BgHoldKind.Action || this.HoldTargetPresent(),
            this.holdKind is null || CaptureHoldPins() == this.holdPins))
            // W1 B2: when the slot fails because the harm re-check found a named harm, the release says which.
            this.RequestRelease(this.holdHarmGone is { } harmNow ? BgHoldReason.TargetGone + ":" + harmNow : "SLOT_VALIDITY_CHANGED");

        bool active = Game1.game1.IsActive;
        bool bodyCompleted = false;
        try
        {
            this.TickingBody(active);
            bodyCompleted = true;
        }
        finally
        {
            // Stage 2 (2.2 N2): the owned press override is held only while the hold plan
            // expects H; every other state, and any fault above, Suppresses here, in the
            // same UpdateTicking (before SMAPI ApplyOverrides / Game1.Update).
            this.TickPressSession(active, bodyCompleted);
        }
    }

    private void TickingBody(bool active)
    {
        if (this.request is null && this.latch is null)
            return;

        // ADR 244: window focus gained or lost mid-request is not an exit. The same path keeps serving.

        if (this.latch is not null && this.latchRemovalDue)
            this.RemoveLatch();

        if (this.request is not { } req || this.phase is Phase.Closing or Phase.HoldSettle)
            return;

        // FIXB53: a menu/warp can suppress UpdateControlInput entirely. Event pins must
        // be checked before every retained Press tick too, not only inside the hook.
        if (req.EventToken is not null && !this.PinnedEventValid(req))
        {
            this.OnDivergence("EVENT_CONTROL_CHANGED");
            return;
        }

        if (this.holdTarget?.EventScopeValid is not null && !this.EventActionValid())
        {
            this.OnDivergence("EVENT_ACTION_CHANGED");
            return;
        }

        long nowUnix = NowUnixMs();
        if (nowUnix > req.DeadlineUnixMs)
            this.SignalRelease("DEADLINE", p => p.OnDeadlineExpired());
        if (this.owner.StopLatched)
            this.SignalRelease("STOP_LATCHED", p => p.OnStopLatched());
        bool leaseValid = this.LeaseValidFor(req, nowUnix);
        if (!leaseValid)
            this.SignalRelease("LEASE_INVALID", p => p.OnLeaseExpired());
        // A HOST pause ends this bounded input through ordinary release proof.
        // Do not enter FREEZE_PAUSE divergence / no-callback containment merely
        // because Game1 intentionally stops consuming movement. STOP, lease loss
        // and every physical cleanup check retain their existing precedence.
        if ((Game1.paused || Game1.HostPaused) && this.releaseReason is null)
        {
            this.releaseReason = "HOST_PAUSED";
            this.Close(BackgroundTerminalKind.Uncertain, "NOT_COMPLETED:RELEASED_EARLY:HOST_PAUSED");
            return;
        }
        // Live7 rehearsal 9: decided ONCE per tick, before this tick's press decision (TickPressSession).
        this.tickStop = new BgTickStopSnapshot(this.updateSeq, req.RequestId, nowUnix,
            this.releaseReason is not null, this.owner.StopLatched, req.DeadlineUnixMs, leaseValid);

        // N-R release set: re-applied every tick. Inactive, SMAPI keeps it anyway (idempotent); active, its
        // TrueUpdate drops a Suppress once the real button is up and a held one needs it again (ADR 244).
        this.ApplySuppress();
        if (active)
            this.SuppressForeignInput();

        if (this.latch is not null && !this.LatchFieldHolds(this.latch))
        {
            // Escape / foreign clear / replacement: fail closed, never reinstall for this request.
            this.OnDivergence(ReasonLatchUnreachable);
            return;
        }

        // WS3a move_through: a warp seen here releases the plan before this tick's hook (no H while warping).
        this.ObserveThroughStartTicking();

        if (this.latch is not null
            && this.phase is Phase.AwaitN0 or Phase.PreHNeutral or Phase.Moving or Phase.Holding)
        {
            this.tokenSeq = this.updateSeq;
            this.tokenConsumed = false;
            this.tokenInFlight = false;
        }
    }

    private void TickPressSession(bool active, bool bodyCompleted)
    {
        BgPressSession? session = this.pressSession;
        // Before POST_N0_CANONICALIZE there is no hold plan and nothing can have been
        // pressed: the session is not ticked (a pre-hold Release would latch it released).
        // WS-G0: the pad move's VERIFY plan plays the hold plan's role.
        BgMoveFramePlan? padMove = this.plan is { Verify: true } verifyMove ? verifyMove : null;
        if (session is null || (this.holdPlan is null && padMove is null && !session.PressIssued))
            return;
        session.Tick(() =>
        {
            // Review minor 3: the v1 short-circuit order is kept (PrePressTargetCheck runs only after
            // bodyCompleted / !active / request / Holding / KeyShouldBeDown, exactly as before).
            // ADR 244: no focus term; an active tick renews the held press (SMAPI TrueUpdate cleared it).
            bool hold = bodyCompleted
                && this.request is not null
                && ((this.phase == Phase.Holding && this.holdPlan is { KeyShouldBeDown: true } && this.PrePressTargetCheck(session))
                    || (this.phase == Phase.Moving && padMove is { KeyShouldBeDown: true }))
                && this.PrePressPadCheck(session)
                && this.releaseReason is null && !this.dispatchImpossible && !this.owner.StopLatched
                && this.latch is not null && this.LatchFieldHolds(this.latch);
            string why = this.releaseReason ?? (bodyCompleted ? "RELEASE" : "TICKING_EXCEPTION");
            if (!hold && this.holdPlan is { } plan && plan.Next == BgFrameKind.H)
                plan.RequestRelease(why);
            if (!hold && padMove is { } move && move.Next == BgFrameKind.H)
                move.RequestRelease(why);
            return hold ? BgPressIntent.Hold : BgPressIntent.Release;
        }, renew: active);
        if (session.ArmFailure is { } armFailure && !session.PressIssued && this.request is not null && !this.holdSettling)
        {
            // R2: containment could not be armed, so no Press was issued.
            this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(armFailure));
        }
    }

    /// <summary>After Game1.Update: readbacks, phase transitions, containment heartbeat.</summary>
    public void OnUpdateTicked()
    {
        try
        {
            this.TickedCore();
        }
        catch (Exception ex)
        {
            bool pad = this.padRequest;
            this.FailClosed("TICKED_EXCEPTION:" + ex.GetType().Name);
            this.monitor.Log($"background ticked fault (fail closed): {ex}", LogLevel.Error);
            if (pad)
                this.PadPanicRequest ??= "TICKED_EXCEPTION";
        }
        finally
        {
            this.PublishContainmentTick();
        }
    }

    private void TickedCore()
    {
        if (this.lastHookReceipt is { } receipt)
        {
            this.monitor.Log($"background receipt run={this.runId} {receipt} latch_receipts={this.latch?.Receipts ?? -1}", LogLevel.Debug);
            this.lastHookReceipt = null;
        }

        this.TickHoldObservation();

        if (this.request is null)
            return;
        this.phaseTicks++;
        switch (this.phase)
        {
            case Phase.NeutralizeRetained:
                this.TickNeutralizeRetained();
                break;
            case Phase.AwaitN0:
                if (this.preHConsumed > 0)
                {
                    this.EnterPhase(Phase.PreHNeutral);
                    this.TickPreH();
                }
                else if (this.phaseTicks > AwaitN0BoundTicks)
                {
                    this.Close(BackgroundTerminalKind.Rejected, ReasonN0NotConsumed);
                }
                break;
            case Phase.PreHNeutral:
                this.TickPreH();
                break;
            case Phase.Moving:
                this.TickMoving();
                break;
            case Phase.VerifyRelease:
                this.TickVerifyRelease();
                break;
            case Phase.Holding:
                this.TickHolding();
                break;
            case Phase.HoldSettle:
                this.TickHoldSettle();
                break;
            case Phase.Closing:
                if (this.dispatchImpossible && this.terminal is null && this.request is not null)
                    this.Finish(BackgroundTerminalKind.Uncertain, this.divergence ?? BgReason.PreconsumerHistoryDivergence);
                break;
        }
    }

    private void TickNeutralizeRetained()
    {
        if (this.padTakeSettleTicks > 0)
        {
            // FIXB16 D-B14b-2: one whole update with gamepadMode ForceOn before the first frame of this request.
            this.padTakeSettleTicks--;
            this.monitor.Log($"background PAD take_settle run={this.runId} remaining={this.padTakeSettleTicks}", LogLevel.Debug);
            return;
        }
        BgBindingTable table = this.bindings!;
        BgInputState retained = SampleRetained();
        if (BgStaleNeutralize.VerifyRetainedNeutral(table, retained) is not null)
        {
            if (this.phaseTicks > RetainedNeutralBoundTicks)
                this.Finish(BackgroundTerminalKind.Rejected, BgReason.PreconditionUnavailable(BgReason.RetainedNotNeutral));
            return;
        }

        // N-H (before the latch): removal-only alignment of Game1.old* history.
        MenuStepCommandRequest req = this.request!.Value;
        bool chainExact = this.hooks?.IsExactChain() == true;
        bool latchField = this.LatchFieldEmpty();
        bool binding = BindingValidFor(req) && this.LeaseValidFor(req, NowUnixMs()) && this.releaseReason is null;
        BgAdmissionFacts facts = SampleAdmissionFacts(binding, chainExact, latchField, this.padRequest);
        facts = BgReflexPress.Relax(facts, this.reflexPress, BgReflexRuntime.RodHeld()); // WS-G4 tap: the rod is in use
        BgInputState history = SampleHistory();
        string? nhGate = this.holdKind is not null
            ? BgHoldGate.Reject(this.SampleHoldGateFacts(), false, BgFrameKind.H)
            : this.padRequest ? this.PadCursorRequestHiddenReason() : null; // WS-G0 pad move: cursor-hidden, no relaxation
        if (nhGate is not null)
        {
            // Wait boundedly for a native hidden draw or FIXB54 eligible reconciliation;
            // an ineligible visibility assignment still refuses without Press/latch.
            if (this.padRequest && BgPadFade.Pending(nhGate) && this.phaseTicks <= BgPadFade.WaitTicks)
                return;
            if (this.padRequest && nhGate.StartsWith(BgCursorHidden.Prefix + ":", StringComparison.Ordinal))
                this.LogPadCursorRefusal(nhGate, req, this.runId, "N-H");
            this.Finish(BackgroundTerminalKind.Rejected, BgReason.PreconditionUnavailable(nhGate));
            return;
        }
        BgStaleNeutralizePlan nh = this.padRequest
            ? BgStaleNeutralize.PlanForPadKey(table, facts, this.syntheticKey, retained, history)
            : this.holdKind is null
                ? BgStaleNeutralize.Plan(table, facts, this.direction, retained, history)
                : BgStaleNeutralize.PlanForKey(table, facts, this.syntheticKey, retained, history);
        if (!nh.Admitted || !nh.SyntheticKey.Equals(this.syntheticKey))
        {
            this.Finish(BackgroundTerminalKind.Rejected, nh.Label ?? BgReason.PreconditionUnavailable(BgReason.AmbiguousBinding));
            return;
        }

        // FIXB54: W5 succeeded but retained no successful same-binding draw diagnostics.
        // One read-only receipt at the actual guarded N-H boundary; never an admission grant.
        if (this.padRequest)
            this.LogPadCursorReady(req, this.runId);

        WriteHistoryRemoval(nh.RemoveFromHistory, nh.ResetPadHistory);
        BgInputState after = SampleHistory();
        this.monitor.Log(
            $"background N-H run={this.runId} before={history.Describe()} removed=[{string.Join(",", nh.RemoveFromHistory)}] "
                + $"reset_pad={nh.ResetPadHistory} kept_move_only=[{string.Join(",", nh.StaleMoveOnlyKept)}] after={after.Describe()}",
            LogLevel.Info);
        if (!after.Equals(nh.HistoryAfterNH))
        {
            this.Finish(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(ReasonNhWriteUnconfirmed));
            return;
        }

        if (!this.LatchFieldEmpty() || this.latchEverInstalled)
        {
            this.Finish(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(BgReason.LatchFieldNotAdmitted));
            return;
        }

        this.retainedSnapshot = retained;
        this.historyBeforeN0 = after;
        this.n0Frame = BgSyntheticFrame.Build(after, null);
        this.n0MouseRaw = Game1.oldMouseState;
        this.preN0Proxy = ActionProxy.Sample();
        ArtiReachabilityLatch owned = new(req.RequestId ?? "", req.SessionId ?? "", this.runtimeInstanceId,
            req.OwnerId ?? "", req.Generation, this.runId, () => this.updateSeq);
        // C3 install: field was re-read empty just above; write the owned latch through
        // the reflected field, then re-read and require exact identity. The attempt
        // counts as "ever installed" (no silent reinstall for this request).
        this.latch = owned;
        this.latchEverInstalled = true;
        if (this.fields?.TryWriteInputSimulator(owned) != true || !this.LatchFieldHolds(owned))
        {
            // Nothing is overwritten here; compare-before-restore (RemoveLatch) runs on
            // the next UpdateTicking and clears only if the field still holds `owned`.
            this.monitor.Log($"background LATCH install unconfirmed run={this.runId}; fail closed, no retry", LogLevel.Warn);
            this.Finish(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(ReasonLatchInstallUnconfirmed));
            return;
        }
        this.EnterPhase(Phase.AwaitN0);
        this.monitor.Log(
            $"background LATCH installed run={this.runId} retained_snapshot={retained.Describe()} n0={this.n0Frame.Describe()}",
            LogLevel.Info);
    }

    private void TickPreH()
    {
        Capture c = Capture.Now();
        this.captures.Add(c);
        if (this.captures.Count == 1)
            this.actionEdgeObserved = !ActionProxy.Sample().Equals(this.preN0Proxy);

        bool stopped = this.StoppedOverLast(2);
        if (this.n0Result is null)
        {
            if (!stopped && this.captures.Count < N0ReadbackCaptures)
                return;
            BgN0Observation observation = new(this.actionEdgeObserved, c.MovementDirections, stopped);
            BgStepResult result = BgN0.Evaluate(this.bindings!, this.historyBeforeN0!, observation);
            this.n0Result = result;
            this.monitor.Log(
                $"background N0 run={this.runId} result={result.Label} md={c.MovementDirections} stopped={stopped} "
                    + $"action_edge={this.actionEdgeObserved} captures={this.captures.Count}",
                result.IssueH ? LogLevel.Info : LogLevel.Warn);

            if (result.Kind == BgStepKind.ContainmentRequired)
            {
                // CT override: no H, keep only bounded neutral cleanup, arm pre-H containment.
                bool armed = this.policy?.TryArmForPreHUnresolved(BackgroundContainmentWatchdog.NowMs(),
                    Context.IsMainPlayer, Context.IsSplitScreen, this.request?.RequestId ?? "", this.runtimeInstanceId) == true;
                if (!armed)
                    this.FailClosed("PRE_H_CONTAINMENT_NOT_ARMED");
                return;
            }
            if (!result.IssueH)
            {
                this.Close(BackgroundTerminalKind.Uncertain, result.Label);
                return;
            }
            if (this.releaseReason is { } why)
            {
                this.Close(BackgroundTerminalKind.Uncertain, "RELEASED_BEFORE_H:" + why);
                return;
            }
            this.Canonicalize();
            return;
        }

        // Pre-H containment cleanup: neutral frames continue until verified stop or the watchdog bound.
        if (this.StoppedOverLast(BgMoveCompletion.RequiredStableCaptures + 1)
            && this.policy?.OnNeutralVerified(BackgroundContainmentWatchdog.NowMs(), 0, true) == true)
        {
            this.Close(BackgroundTerminalKind.Uncertain, BgReason.PreconditionUnavailable(BgReason.StaleMotionUnresolved));
        }
    }

    private void Canonicalize()
    {
        MenuStepCommandRequest req = this.request!.Value;
        bool ownershipExact = this.LatchFieldHolds(this.latch)
            && this.hooks?.IsExactChain() == true
            && BindingValidFor(req)
            && this.LeaseValidFor(req, NowUnixMs())
            && this.releaseReason is null
            && !this.owner.StopLatched;
        BgCanonicalizeFacts facts = new(Game1.game1.IsActive, ownershipExact, this.n0Result!.Value,
            SampleRetained(), this.retainedSnapshot!, SampleHistory());
        BgCanonicalizeResult planned = BgPostN0Canonicalize.Plan(this.bindings!, facts);
        if (!planned.Step.IssueH)
        {
            this.Close(planned.Step.Kind == BgStepKind.ContainmentRequired ? BackgroundTerminalKind.Uncertain : BackgroundTerminalKind.NotCompleted,
                planned.Step.Label);
            return;
        }

        WriteHistoryRemoval(planned.Removed, resetPad: false);
        BgInputState resampled = SampleHistory();
        BgStepResult confirmed = BgPostN0Canonicalize.ConfirmWrite(planned, resampled);
        this.monitor.Log(
            $"background POST_N0_CANONICALIZE run={this.runId} before={planned.HistoryBefore?.Describe()} "
                + $"removed=[{string.Join(",", planned.Removed)}] B={resampled.Describe()} result={confirmed.Label}",
            confirmed.IssueH ? LogLevel.Info : LogLevel.Warn);
        if (!confirmed.IssueH)
        {
            this.Close(BackgroundTerminalKind.NotCompleted, confirmed.Label);
            return;
        }

        if (!BgHistoryInvariant.TryCreate(this.bindings!, planned.Baseline!, this.retainedSnapshot!, out BgHistoryInvariant? inv, out string? reason)
            || inv is null)
        {
            this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(reason ?? BgReason.PostN0HistoryNotCanonical));
            return;
        }

        this.invariant = inv;
        this.baselineMouseRaw = Game1.oldMouseState;
        if (this.padRequest && !this.padNeutralTick.Seen)
        {
            // PLAN 1.5 d: never a world pad frame before one neutral UpdateControlInput tick.
            this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(BgHoldReason.PadNeutralTickUnseen));
            return;
        }
        if (this.padRequest && this.holdKind is null)
        {
            if (BgHoldBaseline.Coherence(this.retainedSnapshot!, planned.Baseline!) is string padIncoherent)
            {
                this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(padIncoherent));
                return;
            }
            this.nrSuppress = BgHoldBaseline.SuppressSetAfterN0(this.nrSuppress, this.syntheticKey).ToList();
            this.suppress = this.SButtonsOf(this.nrSuppress);
            if (!this.TryArmSegmentGuard(req))
                return;
            this.plan = BgMoveFramePlan.PadVerify(this.direction, req.Ticks, segmentHold: this.segmentGuard is not null,
                pathHold: this.pathStops is not null); // fix92A / ADR 162
            this.startCapture = Capture.Now();
            this.EnterPhase(Phase.Moving);
            this.monitor.Log(
                $"background PAD MOVE ready run={this.runId} dir={this.directionName} key={this.syntheticKey} ticks={req.Ticks} "
                    + $"B={resampled.Describe()} neutral_tick_seen=true n_r_after_n0=[{string.Join(",", this.nrSuppress)}]",
                LogLevel.Info);
            return;
        }
        if (this.holdKind is { } holdKindNow)
        {
            // VERIFY exactness: retained snapshot and B must be the same button-free state.
            if (BgHoldBaseline.Coherence(this.retainedSnapshot!, planned.Baseline!) is string incoherent)
            {
                this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(incoherent));
                return;
            }
            // R5: N0 verified -> the pinned key leaves the per-tick N-R suppress set.
            this.nrSuppress = BgHoldBaseline.SuppressSetAfterN0(this.nrSuppress, this.syntheticKey).ToList();
            List<SButton> kept = new();
            foreach (BgButton b in this.nrSuppress)
            {
                if (BackgroundInputNormalizer.TryToSButton(b, out SButton s))
                    kept.Add(s);
            }
            this.suppress = kept;
            this.holdPlan = this.reflexPress is { } reflexKind
                ? new BgHoldFramePlan(holdKindNow, this.syntheticKey, req.Ticks, BgReflexPress.MaxTicks(reflexKind))
                : new BgHoldFramePlan(holdKindNow, this.syntheticKey, req.Ticks);
            this.lastUsingTool = false;
            this.EnterPhase(Phase.Holding);
            this.monitor.Log(
                $"background HOLD ready run={this.runId} kind={holdKindNow} key={this.syntheticKey} ticks={req.Ticks} B={resampled.Describe()} "
                    + $"n_r_after_n0=[{string.Join(",", this.nrSuppress)}]" + (this.padRequest ? " pad=true neutral_tick_seen=true" : ""),
                LogLevel.Info);
            return;
        }
        if (!this.TryArmSegmentGuard(req))
            return;
        if (this.pathStops is not null)
        {
            // fix92A / ADR 162: never reached (admission refuses a path off the pad path); fail closed before any H.
            this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(MoveSegmentGuard.ReasonInvalid + ":PATH_NEEDS_PAD"));
            return;
        }
        this.plan = new BgMoveFramePlan(this.direction, this.syntheticKey, req.Ticks, segmentHold: this.segmentGuard is not null);
        this.startCapture = Capture.Now();
        this.EnterPhase(Phase.Moving);
    }

    /// <summary>
    /// fix89D / ADR 158: a move_cardinal with segment_stop is held only with a position guard built from the
    /// actor's pose right before the first H. An invalid stop (off the line, behind, too far, geometry mismatch)
    /// closes NOT_COMPLETED before any H: nothing was pressed, so nothing needs releasing.
    /// </summary>
    private bool TryArmSegmentGuard(MenuStepCommandRequest req)
    {
        this.segmentGuard = null;
        if (req.SegmentStop is not { } stop)
            return true;
        Farmer? player = Context.IsWorldReady ? Game1.player : null;
        Capture start = Capture.Now();
        Point tile = player?.TilePoint ?? new Point(-1, -1);
        if (player is null || req.EventToken is not null || MenuStepCommandInbox.IsMoveThrough(req)
            || !MoveSegmentGuard.TryCreate(this.directionName,
                // fix92A / ADR 162: a path's tick budget covers all its segments; each guard keeps the 1..120 bound.
                req.PathNext is null ? req.Ticks : Math.Min(req.Ticks, MoveSegmentGuard.MaxTicks), stop, start.Location, start.X, start.Y,
                tile.X, tile.Y, out MoveSegmentGuard? guard, out string? why) || guard is null)
        {
            this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(MoveSegmentGuard.ReasonInvalid));
            return false;
        }
        if (req.PathNext is { } path)
        {
            // fix92A / ADR 162: the whole polyline is checked before any H; an invalid path never presses.
            if (!MovePath.Validate(this.directionName, guard.Tiles, stop, path, req.Ticks, out _))
            {
                this.Close(BackgroundTerminalKind.NotCompleted, BgReason.PreconditionUnavailable(MoveSegmentGuard.ReasonInvalid + ":PATH"));
                return false;
            }
            this.pathStops = path.Stops;
            this.pathStartHealth = player.health;
        }
        this.segmentGuard = guard;
        this.monitor.Log(
            $"background SEGMENT armed run={this.runId} dir={this.directionName} stop={stop.X},{stop.Y} tiles={guard.Tiles} ticks={req.Ticks}"
                + (this.pathStops is { } next ? $" path_next={string.Join(";", next.Select(s => s.X + "," + s.Y))}" : ""),
            LogLevel.Info);
        return true;
    }

    /// <summary>
    /// fix92A / ADR 162: at a reached segment stop, after the R frame, turn toward the next stop inside the same
    /// request. Runs in UpdateTicked right after TickHoldObservation, which proves the old pin released (Suppress
    /// issued, IsSuppressed, pad read up, VERIFY receipt with it absent). Every condition below must hold;
    /// otherwise the path ends here and the plan goes on to N and the ordinary release verification.
    /// The next Press is the new session's single Press, armed before it (R2), in the next UpdateTicking.
    /// </summary>
    private void TryTurnPath(BgMoveFramePlan movePlan)
    {
        if (this.pathStops is not { } stops || this.pathEnded || !movePlan.PathHold || !movePlan.RConsumed || movePlan.NConsumed
            || !string.Equals(movePlan.ReleaseReason, MoveSegmentGuard.ReasonReached, StringComparison.Ordinal))
            return;
        this.pathEnded = true; // one decision per R frame; set back only on a successful turn
        string? why = this.PathTurnRefusal(movePlan, stops, out MoveSegmentGuard? next, out BgDirection dir, out string dirName);
        if (why is null && next is not null)
        {
            BgButton key = BgPadButtons.MovePin(dir);
            if (!movePlan.BeginNextSegment(dir, key))
            {
                why = MovePath.ReasonNextInvalid + ":PLAN";
            }
            else
            {
                MenuStepCommandRequest req = this.request!.Value;
                this.segmentGuard = next;
                this.pathIndex++;
                this.pathSegmentStartH = movePlan.ConsumedH;
                this.syntheticKey = key;
                this.direction = dir;
                // R5: the newly pinned key never stays in the per-tick N-R suppress set.
                this.nrSuppress = BgHoldBaseline.SuppressSetAfterN0(this.nrSuppress, key).ToList();
                this.suppress = this.SButtonsOf(this.nrSuppress);
                this.keyAbsentReceipt = false;
                // One key, one Press per session: the old session is release-proved; the new one arms before its Press.
                this.pressSession = new BgPressSession(key, this.policy!, this.port, BackgroundContainmentWatchdog.NowMs,
                    Context.IsMainPlayer, Context.IsSplitScreen, req.RequestId ?? "", this.runtimeInstanceId);
                this.pathEnded = false;
                this.monitor.Log(
                    $"background PATH turn run={this.runId} seg={this.pathIndex + 1}/{stops.Count + 1} dir={dirName} key={key} "
                        + $"stop={next.Stop.X},{next.Stop.Y} tiles={next.Tiles} h={movePlan.ConsumedH}/{movePlan.TargetHFrames} "
                        + $"containment={this.policy?.ResultLabel}",
                    LogLevel.Info);
                return;
            }
        }
        this.pathEnd = why ?? MovePath.ReasonNextInvalid;
        this.monitor.Log(
            $"background PATH end run={this.runId} reason={this.pathEnd} reached={this.pathReached}/{stops.Count + 1} "
                + $"h={movePlan.ConsumedH}/{movePlan.TargetHFrames}",
            this.pathEnd == MovePath.ReasonComplete ? LogLevel.Info : LogLevel.Warn);
    }

    private string? PathTurnRefusal(BgMoveFramePlan movePlan, IReadOnlyList<MoveSegmentStop> stops,
        out MoveSegmentGuard? next, out BgDirection dir, out string dirName)
    {
        next = null;
        dir = default;
        dirName = "";
        if (this.pathIndex >= stops.Count)
            return MovePath.ReasonComplete;
        if (this.request is not { } req)
            return MovePath.ReasonStopped + ":NO_REQUEST";
        // The old pin: release proved by the existing proof, and no ARTI pad override is still pressed.
        if (this.pressSession is not { PressIssued: true, ReleaseProved: true } || this.PadLedger.Outstanding.Count != 0
            || this.policy is not { } policy || policy.PressOutstanding)
            return MovePath.ReasonTurnUnproved;
        long nowUnix = NowUnixMs();
        if (this.releaseReason is not null || this.owner.StopLatched || nowUnix > req.DeadlineUnixMs || !this.LeaseValidFor(req, nowUnix))
            return MovePath.ReasonStopped + ":" + (this.releaseReason ?? (this.owner.StopLatched ? "STOP_LATCHED"
                : nowUnix > req.DeadlineUnixMs ? "DEADLINE" : "LEASE_INVALID"));
        if (this.dispatchImpossible || this.divergence is not null || this.terminal is not null
            || policy.State == BgContainmentState.Contained)
            return MovePath.ReasonStopped + ":FAULT";
        if (this.latch is null || this.latchRemovalDue || !this.LatchFieldHolds(this.latch))
            return MovePath.ReasonStopped + ":LATCH"; // ADR 244: focus is not a reason to stop a path
        if (movePlan.ConsumedH >= movePlan.TargetHFrames)
            return MovePath.ReasonFramesSpent;
        Farmer? player = Context.IsWorldReady ? Game1.player : null;
        if (player is null || Game1.activeClickableMenu is not null || Game1.dialogueUp || Game1.eventUp || !player.CanMove)
            return MovePath.ReasonEvent;
        if (player.health < this.pathStartHealth)
            return MovePath.ReasonDamage;
        Capture at = Capture.Now();
        if (!string.Equals(at.Location, this.startCapture.Location, StringComparison.Ordinal))
            return MoveSegmentGuard.ReasonLocationChanged;
        if (at.MovementDirections != 0)
            return MovePath.ReasonTurnUnproved + ":MOVEMENT";
        MoveSegmentStop from = this.segmentGuard?.Stop ?? new MoveSegmentStop(-1, -1);
        Point tile = player.TilePoint;
        MoveSegmentStop to = stops[this.pathIndex];
        if (tile.X != from.X || tile.Y != from.Y || MovePath.DirectionBetween(from, to) is not { } name
            || !BackgroundInputNormalizer.TryParseDirection(name, out dir))
            return MovePath.ReasonNextInvalid + ":POSE";
        dirName = name;
        int remaining = Math.Min(MoveSegmentGuard.MaxTicks, movePlan.TargetHFrames - movePlan.ConsumedH);
        if (!MoveSegmentGuard.TryCreate(name, remaining, to, at.Location, at.X, at.Y, tile.X, tile.Y, out next, out _) || next is null)
            return MovePath.ReasonNextInvalid + ":GUARD";
        return null;
    }

    /// <summary>
    /// fix92A / ADR 162: while a path holds, damage taken or an event / frozen farmer ends H at once
    /// (RequestRelease → R → N, the ordinary release proof). Menus and dialogue keep their existing rule below.
    /// </summary>
    private static string? PathHoldInterrupt(int startHealth)
    {
        Farmer? player = Context.IsWorldReady ? Game1.player : null;
        if (player is null || Game1.eventUp || !player.CanMove)
            return MovePath.ReasonEvent;
        return player.health < startHealth ? MovePath.ReasonDamage : null;
    }

    private MovePathReport? PathReport() => this.pathStops is { } stops
        ? new MovePathReport(stops.Count + 1, this.pathReached, this.plan?.Turns ?? 0,
            this.pathEnd ?? this.plan?.ReleaseReason
                ?? (this.plan is { } p && p.ConsumedH >= p.TargetHFrames ? MovePath.ReasonFramesSpent : MovePath.ReasonStopped))
        : null;

    private void TickMoving()
    {
        BgMoveFramePlan movePlan = this.plan!;
        this.ObserveThroughStart();
        if (this.TickBedThrough()) // WS3b: the Sleep question opened mid-walk (no R / N will follow)
            return;
        if (movePlan.NConsumed)
        {
            this.nCapture = Capture.Now();
            this.latchRemovalDue = true;
            this.EnterPhase(Phase.VerifyRelease);
            this.captures.Add(this.nCapture.Value);
            this.monitor.Log(
                $"background N consumed run={this.runId} h={movePlan.ConsumedH}/{movePlan.TargetHFrames} r={movePlan.RConsumed} "
                    + $"md={this.nCapture.Value.MovementDirections} release_reason={movePlan.ReleaseReason ?? "null"}",
                LogLevel.Info);
            return;
        }

        // fix92A / ADR 162: a reached stop of a path, R consumed and the old pin's release just proved by
        // TickHoldObservation: turn toward the next stop, or end the path (the plan then goes on to N).
        if (this.pathStops is not null && movePlan.RConsumed && !movePlan.NConsumed)
            this.TryTurnPath(movePlan);

        // fix89D / ADR 158: a segment hold checks the actor after every update. Its reason only ends H
        // (RequestRelease → R → N); release verification and every stall / STOP rule below stay as they are.
        // fix92A / ADR 162: on a path the guard counts the frames of its own segment (0 for the first segment).
        int segmentH = movePlan.ConsumedH - this.pathSegmentStartH;
        if (this.segmentGuard is { } guard && movePlan.Next == BgFrameKind.H && segmentH > 0
            && movePlan.ReleaseReason is null)
        {
            Capture at = Capture.Now();
            string? segmentWhy = guard.Observe(at.Location, at.X, at.Y, segmentH);
            if (segmentWhy is null && this.pathStops is not null)
                segmentWhy = PathHoldInterrupt(this.pathStartHealth);
            if (segmentWhy == MoveSegmentGuard.ReasonReached)
                this.pathReached++;
            if (segmentWhy is not null)
            {
                movePlan.RequestRelease(segmentWhy);
                this.monitor.Log(
                    $"background SEGMENT release run={this.runId} reason={segmentWhy} h={movePlan.ConsumedH}/{movePlan.TargetHFrames} "
                        + $"pos={at.X:0.#},{at.Y:0.#} stop={guard.Stop.X},{guard.Stop.Y}",
                    LogLevel.Info);
            }
        }

        // Live7 smoke T5: an owned pad press is outstanding but no callback can occur (menu up), or
        // none came for HoldProgressBoundMs. Release now: Close -> settle Suppresses, removes the latch
        // (no callback possible => the effective pad read proves the release) and disarms containment,
        // well inside the T5 bounds (release 3 s / stall 6 s). A move_through run keeps its warp rules
        // only once its warp started (no callbacks under the fade by design); before that it is a walk.
        long now = BackgroundContainmentWatchdog.NowMs();
        long since = Math.Max(this.lastConsumedMs, this.phaseStartedMs);
        // PM-1 fix3: a seat crossing (Q14 route under pad config) gets the same early release.
        if (this.padRequest && this.pressSession is { PressIssued: true }
            || !this.padRequest && this.request is { } crossing && MenuStepCommandInbox.IsMoveThrough(crossing))
        {
            string? stall = Context.IsWorldReady && (Game1.activeClickableMenu is not null || Game1.dialogueUp)
                ? ReasonMenuOpenedMidMove
                : !this.ThroughWarpStarted && now - since > HoldProgressBoundMs ? ReasonNoCallbackProgress : null;
            if (stall is not null)
            {
                movePlan.RequestRelease(stall);
                this.monitor.Log(
                    $"background PAD MOVE released run={this.runId} reason={stall} h={movePlan.ConsumedH}/{movePlan.TargetHFrames} "
                        + $"menu={(Context.IsWorldReady ? Game1.activeClickableMenu?.GetType().Name : null) ?? "-"} since_ms={now - since}",
                    LogLevel.Warn);
                this.Close(BackgroundTerminalKind.Uncertain, "UNCERTAIN(" + stall + ")");
                return;
            }
        }

        // Bounded stall (no owned callback progress). Armed containment is NOT disarmed by
        // this: the watchdog stays the owner of any unresolved movement.
        if (now - since > ProgressBoundMs)
            this.Close(BackgroundTerminalKind.Uncertain, "UNCERTAIN(" + ReasonNoCallbackProgress + ")");
    }

    private void TickVerifyRelease()
    {
        Capture c = Capture.Now();
        this.captures.Add(c);
        Capture n = this.nCapture!.Value;
        bool stable = this.StoppedOverLast(2);
        this.stableAfterN = stable ? this.stableAfterN + 1 : 0;

        long now = BackgroundContainmentWatchdog.NowMs();
        if (!this.neutralVerified && n.MovementDirections == 0 && stable)
        {
            // Disarm path (a): verified N receipt, movementDirections == 0, stopped readback.
            this.neutralVerified = this.policy is null || !this.policy.Armed
                || this.policy.OnNeutralVerified(now, 0, true);
        }

        // WS3a move_through: a warp seen during VerifyRelease also ends further H (none remain).
        this.ObserveThroughStart();
        if (this.TickBedThrough())
            return;
        BgMoveFramePlan throughPlan = this.plan!;
        bool throughReleaseEvidence = this.neutralVerified && this.PadReleaseSettled && this.latch is null && !this.latchRemovalDue
            && this.latchRestoredExact && n.MovementDirections == 0 && throughPlan.NConsumed
            && (throughPlan.ConsumedH == 0 || throughPlan.RConsumed)
            && (this.stableAfterN >= BgMoveCompletion.RequiredStableCaptures || this.ThroughWarpStarted);
        if (this.TickThroughVerify(throughReleaseEvidence))
            return;

        if (this.stableAfterN >= BgMoveCompletion.RequiredStableCaptures && this.latch is null && !this.latchRemovalDue
            && this.PadReleaseSettled)
        {
            BgMoveFramePlan movePlan = this.plan!;
            double displacement = string.Equals(c.Location, this.startCapture.Location, StringComparison.Ordinal)
                ? Math.Sqrt(Math.Pow(c.X - this.startCapture.X, 2) + Math.Pow(c.Y - this.startCapture.Y, 2))
                : 0;
            // Release is attributable to ARTI only through disarm path (a): movementDirections == 0
            // right after the N update plus stopped readback. Anything else is game-side / UNCERTAIN.
            bool releasedByGame = !this.neutralVerified;
            BgMoveCompletionFacts facts = new(
                movePlan.TargetHFrames,
                movePlan.ConsumedH,
                movePlan.RConsumed,
                movePlan.NConsumed,
                displacement,
                n.MovementDirections,
                this.stableAfterN,
                this.latchRestoredExact,
                Contained: false,
                ReleasedByGame: releasedByGame,
                InterruptedFocusRegained: false,
                movePlan.ReleaseReason);
            string outcome = BgMoveCompletion.Evaluate(facts);
            if (this.request is { EventToken: not null } req && (!this.PinnedEventValid(req)
                || (outcome == BgMoveCompletion.Completed && !EventControlMove.Displacement(this.directionName, req.Ticks,
                    c.X - this.startCapture.X, c.Y - this.startCapture.Y,
                    this.EventRouteAuthority is not null ? EventControlAction.SegmentTicks : EventControlMove.MaxTicks,
                    this.EventRouteAuthority is not null ? 0 : EventControlMove.NativeSlidePxPerTick))))
                outcome = "UNCERTAIN/EVENT_CONTROL_EFFECT";
            this.monitor.Log(
                $"background COMPLETION run={this.runId} outcome={outcome} displacement_px={displacement:0.##} "
                    + $"h={movePlan.ConsumedH}/{movePlan.TargetHFrames} r={movePlan.RConsumed} n={movePlan.NConsumed} "
                    + $"stable={this.stableAfterN} latch_restored_exact={this.latchRestoredExact} containment={this.policy?.ResultLabel}",
                LogLevel.Info);
            this.Finish(ClassifyOutcome(outcome), outcome == BgMoveCompletion.Completed ? MenuStepCommandInbox.PressCompleted : outcome, facts);
            return;
        }

        if (now - this.phaseStartedMs > ProgressBoundMs)
        {
            // Containment (if armed) stays armed; the watchdog remains the owner of unresolved movement.
            this.Finish(BackgroundTerminalKind.Uncertain, "UNCERTAIN(" + ReasonReleaseUnverified + ")");
        }
    }

    // ------------------------------------------------------------------
    // Stage 2 hold readback (UpdateTicked, game thread).
    // ------------------------------------------------------------------

    /// <summary>
    /// Runs every UpdateTicked, also after the request's terminal: menu-debt refresh,
    /// R1 release proof (effective keyboard state read here, never SMAPI IsDown),
    /// verified containment disarm, and dropping a fully released session.
    /// </summary>
    private void TickHoldObservation()
    {
        long now = BackgroundContainmentWatchdog.NowMs();
        IClickableMenu? menu = Context.IsWorldReady ? Game1.activeClickableMenu : null;
        this.chestDebt.Observe(IsPinnedChestMenu(menu, this.debtChest), now, Game1.ticks); // ADR 244: focus no longer clears it
        if (!this.chestDebt.Active)
            this.debtChest = null;

        BgPressSession? session = this.pressSession;
        if (session is null)
            return;
        Capture c = Capture.Now();
        bool stableSinceLast = this.lastObservationCapture is Capture last
            && string.Equals(last.Location, c.Location, StringComparison.Ordinal) && last.X == c.X && last.Y == c.Y;
        this.lastObservationCapture = c;
        if (session.PressIssued && !session.ReleaseProved && session.SuppressIssued)
        {
            // Review minor 3: the keyboard read is the v1 expression (a read fault propagates to the
            // TICKED_EXCEPTION fail-closed path); the pad read is the same contract for pad pins.
            bool keyDown = session.Key.Device == BgDevice.Keyboard
                ? Game1.input.GetKeyboardState().IsKeyDown(this.holdXnaKey)
                : this.EffectivePadDown(session.Key);
            // WS-G4: a menu opened by a reflex press (inventory-full catch ItemGrabMenu) stops UpdateControlInput, so no
            // callback can carry the key-absent receipt; the effective pad read above is the proof.
            bool noCallbackPossible = this.latch is null || this.IsExpectedHoldReceiver(menu) || (this.reflexPress is not null && menu is not null);
            if (session.TryProveRelease(now, keyDown, this.keyAbsentReceipt || noCallbackPossible))
                this.monitor.Log($"background HOLD release proved run={this.runId} key={session.Key} suppress_calls={session.SuppressCalls} "
                    + $"receipt={this.keyAbsentReceipt} no_callback={noCallbackPossible}", LogLevel.Info);
        }
        bool expectedSeatMotion = this.ObserveSeatMotion(now);
        if (session.ReleaseProved && this.policy is { Armed: true } p && !p.PressOutstanding
            && !expectedSeatMotion && this.holdTarget?.SeatMotion?.Faulted != true)
            p.OnNeutralVerified(now, c.MovementDirections, c.MovementDirections == 0 && stableSinceLast);
        if (this.request is null && !session.Outstanding && this.policy?.Armed != true)
            this.pressSession = null;
    }

    private void ObserveHoldCycle()
    {
        // Refresh the post-H effect during release; the exact transition was already
        // captured in CommitHolding, before native location/event continuation.
        if (this.holdTarget?.EventScopeValid is not null)
            this.ObserveActionEffect();
        bool usingTool = Context.IsWorldReady && Game1.player?.UsingTool == true;
        if ((this.holdPlan?.ConsumedH ?? 0) > 0 && usingTool && !this.lastUsingTool)
            this.usingToolRisingEdges++;
        this.lastUsingTool = usingTool;
        if (this.FishingCatchReceiver(Context.IsWorldReady ? Game1.activeClickableMenu : null, capture: true))
            this.pinnedMenuObserved = true;
        if (this.holdKind == BgHoldKind.Action && this.pressSession?.PressIssued == true
            && this.IsExpectedHoldReceiver(Context.IsWorldReady ? Game1.activeClickableMenu : null))
        {
            this.pinnedMenuObserved = true;
            this.observedMenu ??= Game1.activeClickableMenu;
        }
    }

    private void TickHolding()
    {
        this.ObserveHoldCycle();
        BgHoldFramePlan hold = this.holdPlan!;
        // WS3b: npc / eat open their DialogueBox inside the H frame, so R is never consumed.
        bool menuNoCallback = this.holdKind == BgHoldKind.Action && (hold.RConsumed || this.SyncReceiverFramesDone(hold))
            && !hold.NConsumed && this.pinnedMenuObserved;
        if (this.FishingCatchReceiver(Game1.activeClickableMenu))
        {
            this.NeutralizeController("FISH_CATCH_MENU");
            menuNoCallback = true;
        }
        if (hold.NConsumed || menuNoCallback)
        {
            this.menuOpenedNoCallback |= menuNoCallback;
            this.latchRemovalDue = true;
            this.BeginHoldSettle(null, null);
            this.monitor.Log(
                $"background HOLD frames done run={this.runId} h={hold.ConsumedH}/{hold.TargetHFrames} r={hold.RConsumed} n={hold.NConsumed} "
                    + $"menu_no_callback={menuNoCallback} swings={this.usingToolRisingEdges}",
                LogLevel.Info);
            return;
        }

        // Bounded: a press may never stay outstanding without VERIFY progress.
        long now = BackgroundContainmentWatchdog.NowMs();
        long since = Math.Max(this.lastConsumedMs, this.phaseStartedMs);
        if (now - since > HoldProgressBoundMs)
            this.Close(BackgroundTerminalKind.Uncertain, "UNCERTAIN(" + ReasonNoCallbackProgress + ")");
    }

    private long? hostPauseSettleAtMs;

    private void BeginHoldSettle(BackgroundTerminalKind? kind, string? label)
    {
        this.holdSettling = true;
        this.holdExitKind ??= kind;
        this.holdExitLabel ??= label;
        this.phase = Phase.HoldSettle;
        this.phaseTicks = 0;
        this.holdSettleStartMs = BackgroundContainmentWatchdog.NowMs();
        this.hostPauseSettleAtMs = null;
        if (this.latch is not null)
            this.latchRemovalDue = true;
    }

    /// <summary>
    /// Settle before any hold terminal: latch removed (or unresolved), release proved,
    /// verified disarm, UsingTool false (TOOL), pinned menu or the 1500 ms monotonic
    /// menu wait (ACTION, 2.2 N3). Bounded; whatever is unproved stays unproved.
    /// </summary>
    private void TickHoldSettle()
    {
        this.ObserveHoldCycle();
        long now = BackgroundContainmentWatchdog.NowMs();
        BgPressSession? s = this.pressSession;
        bool pressed = s?.PressIssued == true;
        bool latchSettled = this.latch is null || this.latchRestorationUnresolved;
        bool releaseSettled = !pressed || s!.ReleaseProved;
        bool containmentSettled = !pressed || this.policy?.Armed != true;
        // No synthetic input remains, but pause can freeze movementDirections /
        // tool animation before the normal stopped/effect proof is observable.
        // Keep containment armed; only its progress timer may wait, never its
        // heartbeat / unproved-release timers. No terminal is fabricated here.
        if ((Game1.paused || Game1.HostPaused) && latchSettled && releaseSettled
            && !this.dispatchImpossible && this.holdExitLabel == "NOT_COMPLETED:RELEASED_EARLY:HOST_PAUSED")
        {
            if (this.hostPauseSettleAtMs is long last)
                this.holdSettleStartMs += Math.Max(0, now - last);
            this.hostPauseSettleAtMs = now;
            return;
        }
        if (this.hostPauseSettleAtMs is long pausedAt)
        {
            this.holdSettleStartMs += Math.Max(0, now - pausedAt);
            this.hostPauseSettleAtMs = null;
        }
        // WS-G4: a reflex press leaves the rod in use (cast in flight, fishing, hit, catch pose) by design.
        bool toolSettled = this.holdKind != BgHoldKind.Tool || this.reflexPress is not null
            || !(Context.IsWorldReady && Game1.player?.UsingTool == true);
        this.ObserveActionEffect();
        bool menuSettled = this.holdKind != BgHoldKind.Action || this.holdExitKind is not null
            || (this.holdPlan?.ConsumedH ?? 0) == 0
            || (this.HoldActionKind == BgActionKind.Chest
                ? this.pinnedMenuObserved || (s?.FirstSuppressMs is long first && now - first >= BgHoldCompletion.MenuWaitMs)
                : this.ActionEffectSettled(now));
        if ((latchSettled && releaseSettled && containmentSettled && toolSettled && menuSettled)
            || now - this.holdSettleStartMs > this.HoldSettleBoundFor())
        {
            this.Finish(this.holdExitKind ?? BackgroundTerminalKind.Completed, this.holdExitLabel ?? "HOLD_SETTLED");
        }
    }

    private BackgroundHoldResult BuildHoldResult(BgHoldKind kind, BackgroundTerminalKind exitKind, string label)
    {
        BgPressSession? s = this.pressSession;
        BgHoldFramePlan? hold = this.holdPlan;
        bool exited = exitKind != BackgroundTerminalKind.Completed;
        BgInputState history = SampleHistory();
        BgHoldCompletionFacts facts = new(
            kind,
            this.request?.Ticks ?? 0,
            hold?.ConsumedH ?? 0,
            s?.PressIssued == true,
            hold?.RConsumed == true,
            hold?.NConsumed == true,
            s?.ReleaseProved == true,
            this.policy?.ResultLabel ?? "NOT_ARMED",
            this.latchEverInstalled,
            this.latchRestoredExact,
            this.dispatchImpossible,
            this.HoldHistoryAcceptable(history), // WS3b: baseline, or the consumed H frame under a sync receiver
            exitKind == BackgroundTerminalKind.Uncertain,
            exited ? label : null,
            this.releaseReason ?? hold?.ReleaseReason,
            this.usingToolRisingEdges,
            Context.IsWorldReady && Game1.player?.UsingTool == true,
            CaptureHoldPins() == this.holdPins,
            MovementCount(),
            this.pinnedMenuObserved,
            this.menuOpenedNoCallback,
            this.HoldActionKind,
            CaptureHoldPins() is var slotNow && slotNow.Slot == this.holdPins.Slot
                && (this.HoldActionKind == BgActionKind.Generic
                    || string.Equals(slotNow.ToolItem, this.holdPins.ToolItem, StringComparison.Ordinal)),
            kind == BgHoldKind.Action ? this.ActionEffectVerdict() : null,
            SynchronousToolReceiver: this.FishingCatchReceiver(Game1.activeClickableMenu));
        this.LastHoldTarget = this.holdTarget;
        this.LastExtraFacts = this.effectNow?.Extra; // WS-G6 verdict detail
        this.LastEventBefore = this.effectBefore?.Event;
        this.LastEventAfter = this.effectNow?.Event;
        this.LastGenericDelta = this.HoldActionKind == BgActionKind.Generic
            ? GenericWorld.Detail(this.effectBefore?.Generic, this.effectNow?.Generic) : null;
        BgHoldOutcome outcome = BgHoldCompletion.Evaluate(facts);
        // ADR 246 review B2: the zero-press TARGET_GONE rejection says which harm appeared (pre-press or at the hook).
        if (GenericHarm.GoneLabel(outcome.Reason, this.holdHarmGone) is { } harmNamed)
            outcome = outcome with { Reason = harmNamed };
        this.monitor.Log(
            $"background HOLD COMPLETION run={this.runId} kind={kind} outcome={outcome.Outcome} press_count={outcome.PressCount} "
                + $"cleanup={outcome.CleanupConfirmed} reason={outcome.Reason ?? "null"} swings={this.usingToolRisingEdges} "
                + $"opened={outcome.Opened} press_calls={(s?.PressIssued == true ? 1 : 0)} suppress_calls={s?.SuppressCalls ?? 0} "
                + $"release_proved={s?.ReleaseProved == true} latch_restored_exact={this.latchRestoredExact} containment={this.policy?.ResultLabel}",
            outcome.Outcome is BgHoldCompletion.InputCompleted or BgHoldCompletion.ContainerOpened
                or BgHoldCompletion.LocationChanged or BgHoldCompletion.ItemPickedUp ? LogLevel.Info : LogLevel.Warn);
        return new BackgroundHoldResult(kind, outcome, this.observedMenu, facts);
    }

    private static BackgroundTerminalKind ClassifyOutcome(string outcome)
    {
        if (string.Equals(outcome, BgMoveCompletion.Completed, StringComparison.Ordinal))
            return BackgroundTerminalKind.Completed;
        if (outcome.StartsWith("UNCERTAIN", StringComparison.Ordinal)
            || string.Equals(outcome, BgReason.InterruptedFocusRegained, StringComparison.Ordinal)
            || outcome.StartsWith("NOT_COMPLETED:RELEASE_UNVERIFIED", StringComparison.Ordinal)
            || outcome.StartsWith("NOT_COMPLETED:RELEASED_EARLY", StringComparison.Ordinal))
            return BackgroundTerminalKind.Uncertain;
        return BackgroundTerminalKind.NotCompleted;
    }

    // ADR 244: active-window hygiene. The foreign set already named this request (one log line per change).
    private string? foreignInputLogged;
    private int foreignInputSuppressTicks;
    /// <summary>Review R5 S1: owner buttons suppressed during this press (handed back when it ends).</summary>
    private readonly HashSet<SButton> foreignSuppressed = new();
    private bool foreignRestoreDeferred;

    /// <summary>
    /// ADR 244, UpdateTicking with the window active (SMAPI's TrueUpdate has just read the real devices):
    /// every real key, mouse button and pad button that is down while one of her requests is in flight is
    /// Suppressed for HER game through the single port, except her own pinned button while she holds it.
    /// SMAPI keeps such a Suppress while the real button stays down, so her game never sees the owner's
    /// keys or clicks in this window; a mouse move or wheel step cannot be suppressed and reaches VERIFY,
    /// which then names it. Nothing is ever sent to another window or to the OS.
    /// </summary>
    private void SuppressForeignInput()
    {
        BgButton? own = this.pressSession is { PressIssued: true, ReleaseLatched: false } session ? session.Key
            : (this.phase is Phase.Holding or Phase.Moving) && this.releaseReason is null ? this.syntheticKey : (BgButton?)null;
        BgInputState now = BackgroundInputNormalizer.Normalize(Game1.input.GetKeyboardState(), Game1.input.GetMouseState(),
            Game1.input.GetGamePadState(), padPath: false);
        IReadOnlyList<BgButton> foreign = BgForeignInput.ForeignButtons(now, own);
        if (foreign.Count == 0)
            return;
        foreach (BgButton b in foreign)
        {
            if (BackgroundInputNormalizer.TryToSButton(b, out SButton button) && button != SButton.None)
            {
                this.port.SuppressRetained(button);
                this.foreignSuppressed.Add(button); // R5 S1: handed back when her press ends (RestoreOwnerInput)
            }
        }
        this.foreignInputSuppressTicks++;
        string names = string.Join("+", foreign.Select(BgForeignInput.Name));
        if (!string.Equals(names, this.foreignInputLogged, StringComparison.Ordinal))
        {
            this.foreignInputLogged = names;
            this.monitor.Log($"background {BgForeignInput.Prefix} suppressed run={this.runId} request_id={this.request?.RequestId} "
                + $"phase={this.phase} buttons={names} window_active=true ticks={this.foreignInputSuppressTicks}", LogLevel.Info);
        }
    }

    /// <summary>ADR 244: a VERIFY mismatch while the window is active carries the foreign input's name.</summary>
    private static string NameForeignInput(string diverged, string? described) =>
        described is null || Game1.game1?.IsActive != true ? diverged : diverged + ":" + described;

    /// <summary>
    /// Live7 rehearsal 9: every dispatch-impossible report writes one SMAPI line on the game thread FIRST.
    /// The policy may request T2 inside OnDispatchImpossible and the watchdog then FailFasts with no
    /// logging of its own, so this line is the only in-log record of why. Never throws before the report.
    /// </summary>
    private void ReportDispatchImpossible(string site, long nowMs, bool gameActive, int movementDirections)
    {
        BackgroundContainmentPolicy? p = this.policy;
        try
        {
            bool armed = p?.Armed == true;
            bool t2 = armed && !gameActive && movementDirections != 0;
            this.monitor.Log($"background DISPATCH_IMPOSSIBLE site={site} run={this.runId} request_id={this.request?.RequestId} "
                + $"reason={this.divergence ?? "none"} phase={this.phase} armed={armed} active={gameActive} movement={movementDirections} "
                + $"press_outstanding={p?.PressOutstanding == true} t2_contain={t2} receipt={this.lastHookReceipt} {this.StopComponents(null)}",
                t2 ? LogLevel.Error : LogLevel.Warn);
        }
        catch
        {
            // diagnostics only; containment below must still run
        }
        p?.OnDispatchImpossible(nowMs, gameActive, movementDirections);
    }

    /// <summary>Live7 rehearsal 9 diagnostics: the stop / deadline / lease components, live and as sampled. Never throws.</summary>
    private string StopComponents(BgHookFacts? facts)
    {
        try
        {
            long nowUnix = NowUnixMs();
            string deadline = this.request is { } r ? (r.DeadlineUnixMs - nowUnix).ToString() : "na";
            string lease = this.request is { } q ? this.LeaseValidFor(q, nowUnix).ToString() : "na";
            return $"stop=[release_reason={this.releaseReason ?? "none"} owner_stop={this.owner.StopLatched} deadline_minus_now_ms={deadline} "
                + $"lease_live={lease} lease_expires_minus_now_ms={this.owner.LeaseExpiresUnixMs - nowUnix} "
                + $"hook_stop={(facts is { } f ? f.StopLatched.ToString() : "na")} hook_lease={(facts is { } g ? g.LeaseValid.ToString() : "na")} "
                + $"tick_snapshot=[{this.tickStop?.Describe() ?? "none"}] seq={this.updateSeq}]";
        }
        catch
        {
            return "stop=[unreadable]";
        }
    }

    private void SignalRelease(string reason, Action<BackgroundContainmentPolicy> signal)
    {
        this.releaseReason ??= reason;
        if (this.signaled.Add(reason) && this.policy is not null)
            signal(this.policy);
        if (this.phase == Phase.NeutralizeRetained)
            this.Finish(BackgroundTerminalKind.Uncertain, "RELEASED_BEFORE_LATCH:" + reason);
    }

    private void PublishContainmentTick()
    {
        BackgroundContainmentPolicy? p = this.policy;
        if (p is null || !p.Armed)
            return;
        try
        {
            Farmer? player = Context.IsWorldReady ? Game1.player : null;
            Vector2 pos = player?.Position ?? Vector2.Zero;
            p.OnUpdateTicked(new BgTickFacts(
                BackgroundContainmentWatchdog.NowMs(),
                Game1.game1.IsActive,
                this.substitutedThisTick,
                player?.currentLocation?.NameOrUniqueName ?? "",
                pos.X,
                pos.Y,
                MovementCount(),
                GateClosed: player is not null && !this.GatesValid(),
                ExpectedSeatMotion: this.ObserveSeatMotion(BackgroundContainmentWatchdog.NowMs()),
                ReleasedGamePause: (Game1.paused || Game1.HostPaused) && this.pressSession?.ReleaseProved == true
                    && this.latch is null && !this.latchRestorationUnresolved && !this.dispatchImpossible));
        }
        catch (Exception ex)
        {
            // No heartbeat this tick; T3 remains the containment backstop.
            this.monitor.Log($"background containment tick fault: {ex.GetType().Name}", LogLevel.Warn);
        }
    }

    private bool ObserveSeatMotion(long now)
    {
        if (this.holdTarget is not { SeatMotion: { Started: true } motion, SampleSeatMotion: { } sample })
            return false;
        bool expected;
        try { expected = motion.Sample(now, sample()); }
        catch { expected = motion.Sample(now, new(false, false, false, 0, 0, double.NaN, double.NaN)); }
        if (motion.Faulted) this.policy?.OnUnexpectedSeatMotion();
        return expected;
    }

    // ------------------------------------------------------------------
    // Latch / request closing.
    // ------------------------------------------------------------------

    private void Close(BackgroundTerminalKind kind, string label)
    {
        this.phase = Phase.Closing;
        if (this.latch is not null)
            this.latchRemovalDue = true;
        this.Finish(kind, label);
    }

    private void Finish(BackgroundTerminalKind kind, string label, BgMoveCompletionFacts? movementReadback = null)
    {
        if (this.request is not { } req)
            return;
        label = GenericHarm.GoneTerminalLabel(label, this.holdHarmGone); // ADR 246 review B2: the harm names itself
        BackgroundHoldResult? holdResult = null;
        // FIXB53: a VERIFY pad move owns the same retained Press as a hold. A divergent
        // hook must settle its Suppress/readback/latch/containment before the terminal too.
        if (this.padRequest && this.holdKind is null && !this.holdSettling && this.pressSession?.PressIssued == true
            && (!this.PadReleaseSettled || this.latch is not null || this.policy?.Armed == true))
        {
            this.BeginHoldSettle(kind, label);
            return;
        }
        if (this.holdKind is { } holdKindNow)
        {
            // Stage 2: after the latch exists (or a press was issued) every exit settles
            // first (Suppress / proof / latch removal / readback), then reports once.
            if (!this.holdSettling && (this.latchEverInstalled || this.pressSession?.PressIssued == true))
            {
                this.BeginHoldSettle(kind, label);
                return;
            }
            try
            {
                holdResult = this.BuildHoldResult(holdKindNow, kind, label);
            }
            catch (Exception ex)
            {
                // Readback itself failed: never a success, never a confirmed cleanup.
                holdResult = new BackgroundHoldResult(holdKindNow,
                    new BgHoldOutcome(holdKindNow == BgHoldKind.Tool ? BgHoldCompletion.InputUncertain : BgHoldCompletion.InteractionUncertain,
                        this.holdPlan?.ConsumedH ?? 0, false, "UNCERTAIN(HOLD_READBACK_FAULT:" + ex.GetType().Name + ")", false, false),
                    null, null);
            }
            if (holdKindNow == BgHoldKind.Action && this.pressSession?.PressIssued == true && this.holdChest is not null)
            {
                this.debtChest = this.holdChest;
                this.chestDebt.Begin(IsPinnedChestMenu(Context.IsWorldReady ? Game1.activeClickableMenu : null, this.holdChest),
                    BackgroundContainmentWatchdog.NowMs(), req.VerbEnvelope?.GetProperty("attempt_id").GetString() ?? req.RequestId, NowUnixMs(), Game1.ticks);
            }
        }
        this.terminal = new BackgroundMoveTerminal(kind, req, label, this.holdPlan?.ConsumedH ?? this.plan?.ConsumedH ?? 0,
            this.directionName, this.runId, holdResult, this.ThroughResultLocation(), movementReadback, this.PathReport());
        this.request = null;
        // Live7 containment follow-up: the closed request's tick view never outlives it.
        this.tickStop = null;
        this.RestoreOwnerInput(); // review R5 S1: the owner's suppression ends with her press
        if (this.latch is not null)
        {
            this.phase = Phase.Closing;
            this.latchRemovalDue = true;
        }
        else
        {
            this.phase = Phase.Idle;
        }
        this.monitor.Log($"background TERMINAL run={this.runId} request_id={req.RequestId} kind={kind} label={label} "
            + $"containment={this.policy?.ResultLabel}", kind == BackgroundTerminalKind.Completed ? LogLevel.Info : LogLevel.Warn);
    }

    private void FailClosed(string reason)
    {
        this.dispatchImpossible = true;
        this.divergence ??= reason;
        if (this.policy is { Armed: true })
            this.ReportDispatchImpossible("FAIL_CLOSED", BackgroundContainmentWatchdog.NowMs(), Game1.game1?.IsActive == true, MovementCount());
        if (this.latch is not null)
            this.latchRemovalDue = true;
        if (this.request is not null)
            this.Finish(BackgroundTerminalKind.Uncertain, "UNCERTAIN(" + reason + ")");
        else if (this.latch is not null)
            this.phase = Phase.Closing;
    }

    private void ApplySuppress()
    {
        // N-R: existing SMAPI Suppress, release-only. Never presses, never desktop/OS input.
        // WS-G0: through the single port so pad releases land in the override ledger.
        foreach (SButton b in this.suppress)
            this.port.SuppressRetained(b);
    }

    private void EnterPhase(Phase next)
    {
        this.phase = next;
        this.phaseTicks = 0;
        this.phaseStartedMs = BackgroundContainmentWatchdog.NowMs();
        this.captures.Clear();
    }

    /// <summary>
    /// Review R5 S1: the owner's input suppressed during her press does not stay suppressed after it. With her window
    /// active each still-held suppressed owner button is handed back (HelperPressPort.RestoreOwner). In the background no
    /// Press is sent, because SMAPI would retain it; SMAPI drops the Suppress when the owner releases the button.
    /// </summary>
    private void RestoreOwnerInput()
    {
        if (this.foreignSuppressed.Count == 0)
            return;
        bool active = Game1.game1?.IsActive == true;
        // W1 S2: behind, no Press is sent (SMAPI would retain it), and the set is KEPT, so the handback happens on the
        // first active idle tick (TickingCore), not never.
        if (!active)
        {
            if (!this.foreignRestoreDeferred)
                this.monitor.Log($"background {BgForeignInput.Prefix} handback deferred until her window is active run={this.runId}", LogLevel.Info);
            this.foreignRestoreDeferred = true;
            return;
        }
        this.foreignRestoreDeferred = false;
        var restored = new List<string>();
        // Keyboard and mouse only: a pad override would make SMAPI build a connected virtual pad (pad mode, ADR 244), so a
        // suppressed owner pad button is left for SMAPI to drop when the owner releases it (named below as not restored).
        if (active)
            foreach (SButton b in this.foreignSuppressed)
                if (!b.TryGetController(out _) && this.port.RestoreOwner(b))
                    restored.Add(b.ToString());
        this.monitor.Log($"background {BgForeignInput.Prefix} suppression ended with her press run={this.runId} "
            + $"restored=[{string.Join(",", restored)}] window_active={active}", LogLevel.Info);
        this.foreignSuppressed.Clear();
    }

    private void ResetRequestState()
    {
        this.RestoreOwnerInput();
        this.phase = Phase.Idle;
        this.request = null;
        this.foreignInputLogged = null;
        this.foreignInputSuppressTicks = 0;
        this.bindings = null;
        this.suppress = Array.Empty<SButton>();
        this.retainedSnapshot = null;
        this.historyBeforeN0 = null;
        this.n0Frame = null;
        this.preHConsumed = 0;
        this.preHBranch = BgHistoryBranch.Unpinned;
        this.n0Result = null;
        this.actionEdgeObserved = false;
        this.captures.Clear();
        this.plan = null;
        this.segmentGuard = null;
        this.pathStops = null;
        this.pathIndex = 0;
        this.pathSegmentStartH = 0;
        this.pathReached = 0;
        this.pathStartHealth = 0;
        this.pathEnded = false;
        this.pathEnd = null;
        this.invariant = null;
        this.nCapture = null;
        this.stableAfterN = 0;
        this.neutralVerified = false;
        this.lastConsumedMs = 0;
        this.releaseReason = null;
        this.signaled.Clear();
        this.dispatchImpossible = false;
        this.divergence = null;
        this.terminal = null;
        this.latch = null;
        this.latchRemovalDue = false;
        this.latchRestoredExact = false;
        this.latchEverInstalled = false;
        this.tokenSeq = -1;
        this.tokenConsumed = true;
        this.tokenInFlight = false;
        this.tickStop = null;
        // Stage 2 (Busy already refuses admission while a press is outstanding).
        this.holdKind = null;
        this.holdPlan = null;
        this.pressSession = null;
        this.holdXnaKey = Keys.None;
        this.nrSuppress = new List<BgButton>();
        this.holdPins = default;
        this.holdChest = null;
        this.holdSettling = false;
        this.holdExitKind = null;
        this.holdExitLabel = null;
        this.holdSettleStartMs = 0;
        this.keyAbsentReceipt = false;
        this.usingToolRisingEdges = 0;
        this.lastUsingTool = false;
        this.pinnedMenuObserved = false;
        this.menuOpenedNoCallback = false;
        this.observedMenu = null;
        this.lastObservationCapture = null;
        this.padRequest = false;
        this.reflexPress = null;
        this.padNeutralTick.Reset();
        this.ResetInteractionState();
    }

    private bool StoppedOverLast(int count)
    {
        if (this.captures.Count < count)
            return false;
        Capture first = this.captures[this.captures.Count - count];
        for (int i = this.captures.Count - count; i < this.captures.Count; i++)
        {
            Capture c = this.captures[i];
            if (c.MovementDirections != 0
                || !string.Equals(c.Location, first.Location, StringComparison.Ordinal)
                || c.X != first.X || c.Y != first.Y)
                return false;
        }
        return true;
    }

    // ------------------------------------------------------------------
    // Sampling helpers (game thread).
    // ------------------------------------------------------------------

    private bool BindingValid() => this.request is { } req && BindingValidFor(req);

    /// <summary>Exact admitted identity pins. Admission requires mobility; hold gates own mobility after the edge,
    /// and a committed walk-through warp permits only R/N through its fade. Neither changes the request's pins.</summary>
    private bool BindingValidFor(in MenuStepCommandRequest req, bool requirePlayerFree = true) =>
        this.executionAttempt?.Controller is { } controller && this.ExecutionSlot?.Owns(req) == true
            ? controller.Check() || this.releaseReason is not null && controller.CanNeutralize() :
        (req.EventToken is null || this.PinnedEventValid(req)) &&
        MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(
            req, this.identity(), Context.IsWorldReady,
            !requirePlayerFree || Context.IsPlayerFree || this.PinnedEventValid(req) || this.EventActionValid(),
            !requirePlayerFree || Context.CanPlayerMove || this.PinnedEventValid(req) || this.EventActionValid(), out _);

    private bool LeaseValidFor(in MenuStepCommandRequest req, long nowUnixMs) =>
        this.BodyGuardHolds(req)
            // ADR 181: the mod's body guard does not borrow the runtime's per-action lease (a73 SMAPI 9727-9748: the
            // runtime's routine release retired it mid-hold). STOP and a recorded fault still end it at once.
            ? !this.owner.StopLatched && !this.owner.RecordFaulted
              && (this.owner.BoundRuntimeId.Length == 0
                  || string.Equals(this.owner.BoundRuntimeId, req.RuntimeInstanceId, StringComparison.Ordinal))
            : this.owner.IsArmed
        && !this.owner.StopLatched
        && !this.owner.RecordFaulted
        && !this.owner.IsReleasing
        && nowUnixMs <= this.owner.LeaseExpiresUnixMs
        && string.Equals(this.owner.OwnerId, req.OwnerId, StringComparison.Ordinal)
        && this.owner.Generation == req.Generation
        && (this.owner.BoundRuntimeId.Length == 0
            || string.Equals(this.owner.BoundRuntimeId, req.RuntimeInstanceId, StringComparison.Ordinal));

    private bool BodyGuardHolds(in MenuStepCommandRequest req) =>
        this.ExecutionSlot?.ActiveAttempt is { Client: ExecutionSlot.BodyGuardClient } && this.ExecutionSlot.Owns(req);

    /// <summary>Only release/neutral frames of an already consumed warp may cross its fade.</summary>
    private bool GatesValidIgnoringCanMove()
    {
        BgAdmissionFacts f = SampleAdmissionFacts(true, true, true, this.padRequest) with { GameActive = false };
        if (this.ThroughWarpStarted && this.plan is { ConsumedH: > 0, Next: not BgFrameKind.H })
            f = f with { CanMove = true, GlobalFade = false, FreezePause = Game1.paused || Game1.HostPaused };
        // Events, menus, freezeControls, identity, lease, STOP and input ownership stay strict.
        return BgAdmission.Reject(f) is null;
    }

    /// <summary>Frozen stage-1 game gates (binding/hook/latch evaluated separately).</summary>
    private bool GatesValid()
    {
        BgAdmissionFacts f = SampleAdmissionFacts(true, true, true, this.padRequest) with { GameActive = false };
        f = BgReflexPress.Relax(f, this.reflexPress, BgReflexRuntime.RodHeld()); // WS-G4 tap: the rod is in use
        return BgAdmission.Reject(f) is null;
    }

    private BgAdmissionFacts SampleAdmissionFacts(bool requestBindingValid, bool hookChainAdmitted, bool latchFieldAdmitted,
        bool padModeRequired = false)
    {
        Farmer? player = Context.IsWorldReady ? Game1.player : null;
        return this.EventMoveFacts(new BgAdmissionFacts(
            IsMainPlayer: Context.IsMainPlayer,
            IsSplitScreen: Context.IsSplitScreen,
            GameActive: Game1.game1.IsActive,
            WorldReady: Context.IsWorldReady && player?.currentLocation is not null,
            MenuOpen: Game1.activeClickableMenu is not null || Game1.dialogueUp,
            EventOrMinigame: Game1.eventUp || Game1.CurrentEvent is not null || Game1.currentMinigame is not null,
            Chatting: Game1.chatBox?.isActive() == true,
            UsingTool: player?.UsingTool == true,
            UsingSlingshot: player?.usingSlingshot == true,
            Fishing: player?.CurrentTool is FishingRod rod && rod.inUse(),
            CanMove: player?.CanMove == true,
            FreezePause: Game1.paused || Game1.HostPaused || player?.freezePause > 0,
            GlobalFade: Game1.globalFade || Game1.fadeToBlack,
            FreezeControls: Game1.freezeControls,
            PanMode: Game1.panMode,
            GamepadMode: Game1.options?.gamepadControls == true,
            RequestBindingValid: requestBindingValid,
            HookChainAdmitted: hookChainAdmitted,
            LatchFieldAdmitted: latchFieldAdmitted,
            PadModeRequired: padModeRequired));
    }

    /// <summary>R: retained effective (post-SMAPI-override) input the game reads as current state.</summary>
    private BgInputState SampleRetained(bool? padPath = null) =>
        BackgroundInputNormalizer.Normalize(Game1.input.GetKeyboardState(), Game1.input.GetMouseState(), Game1.input.GetGamePadState(),
            padPath ?? this.padRequest);

    /// <summary>H: Game1.old* history.</summary>
    private BgInputState SampleHistory(bool? padPath = null) =>
        BackgroundInputNormalizer.Normalize(Game1.oldKBState, Game1.oldMouseState, Game1.oldPadState, padPath ?? this.padRequest);

    /// <summary>Removal-only write of Game1.old*. Never adds a button; mouse X/Y/scroll preserved.</summary>
    internal static void WriteHistoryRemoval(IReadOnlyList<BgButton> remove, bool resetPad)
    {
        HashSet<BgButton> r = new(remove);
        KeyboardState oldK = Game1.oldKBState;
        Keys[] pressed = oldK.GetPressedKeys();
        Keys[] keep = pressed.Where(key => !r.Contains(BgButton.Key(key.ToString()))).ToArray();
        if (keep.Length != pressed.Length)
            Game1.oldKBState = new KeyboardState(keep, oldK.CapsLock, oldK.NumLock);

        MouseState oldM = Game1.oldMouseState;
        if (r.Any(b => b.Device == BgDevice.Mouse))
        {
            ButtonState Keep(ButtonState s, string name) => r.Contains(BgButton.Mouse(name)) ? ButtonState.Released : s;
            Game1.oldMouseState = new MouseState(oldM.X, oldM.Y, oldM.ScrollWheelValue,
                Keep(oldM.LeftButton, "Left"), Keep(oldM.MiddleButton, "Middle"), Keep(oldM.RightButton, "Right"),
                Keep(oldM.XButton1, "XButton1"), Keep(oldM.XButton2, "XButton2"), oldM.HorizontalScrollWheelValue);
        }

        if (resetPad || r.Any(b => b.Device == BgDevice.Pad))
            PadHistoryHygiene.WriteNeutral();
    }

    /// <summary>
    /// Farmer.movementDirections.Count. A read fault reports -1 ("outstanding/unknown"),
    /// never 0, so it can neither prove a stop nor disarm containment.
    /// </summary>
    private static int MovementCount()
    {
        try
        {
            if (!Context.IsWorldReady)
                return 0;
            return Game1.player?.movementDirections is { } dirs ? dirs.Count : -1;
        }
        catch
        {
            return -1;
        }
    }

    private static long NowUnixMs() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

    private readonly record struct Capture(string Location, float X, float Y, int MovementDirections)
    {
        public static Capture Now()
        {
            Farmer? player = Context.IsWorldReady ? Game1.player : null;
            Vector2 pos = player?.Position ?? Vector2.Zero;
            return new Capture(player?.currentLocation?.NameOrUniqueName ?? "", pos.X, pos.Y, MovementCount());
        }
    }

    /// <summary>Bounded independent readback proxy for "no unexpected action edge" at N0.</summary>
    private readonly record struct ActionProxy(bool MenuOpen, bool DialogueUp, bool UsingTool, int ToolIndex, bool Chatting)
    {
        public static ActionProxy Sample()
        {
            Farmer? player = Context.IsWorldReady ? Game1.player : null;
            return new ActionProxy(
                Game1.activeClickableMenu is not null,
                Game1.dialogueUp,
                player?.UsingTool == true,
                player?.CurrentToolIndex ?? -1,
                Game1.chatBox?.isActive() == true);
        }
    }
}
