namespace ArtiStardewBridge.Protocol;

// V4 / V4.1 background movement, S1 layer (#278/5791090505 + #278/5791607180).
//
// Pure, SMAPI-free planning logic only. Nothing in this file reads or writes
// game state, installs a hook/latch, calls a native owner, or emits input.
// S2 normalizes Options InputButton[] bindings, KeyboardState/MouseState/
// GamePadState and hook-entry facts into these records and applies the
// decisions. Evidence ceiling of this layer: UNIT_TESTED/CLOUD_VERIFIED.
//
// Stage 2 (#278/5994543545 + 2.1 5994935998 + 2.2 5995067151, dispatch R1-R5):
// IBgFramePlan lets the same pre-consumer decision table serve a move plan
// (SUBSTITUTE role, unchanged) or a BgHoldFramePlan (VERIFY role: the retained
// frame must equal the expected owned frame exactly; Parent then sees the
// original refs). The hold VERIFY facts/gate live in BackgroundHoldVerify.cs.

public enum BgDevice { Keyboard, Mouse, Pad }

public enum BgDirection { Up, Down, Left, Right }

/// <summary>One normalized input button (keyboard key, mouse button or pad button).</summary>
public readonly record struct BgButton(BgDevice Device, string Name)
{
    public static BgButton Key(string name) => new(BgDevice.Keyboard, name);
    public static BgButton Mouse(string name) => new(BgDevice.Mouse, name);
    public static BgButton Pad(string name) => new(BgDevice.Pad, name);

    public bool IsEscape => this.Device == BgDevice.Keyboard && string.Equals(this.Name, "Escape", StringComparison.Ordinal);

    public override string ToString() => this.Device + ":" + this.Name;
}

/// <summary>
/// Normalized immutable input state: the set of buttons down plus the non-button
/// mouse/pad facts that edge logic and history comparison can see. Used for the
/// retained SMAPI layer (R), the Game1.old* history layer (H) and substituted
/// synthetic frames (S). Equality is exact value equality.
/// </summary>
public sealed class BgInputState : IEquatable<BgInputState>
{
    private readonly HashSet<BgButton> down;

    public BgInputState(IEnumerable<BgButton> down, int mouseX, int mouseY, int scrollWheel, bool padAnalogActive)
        : this(down, mouseX, mouseY, scrollWheel, padAnalogActive, 0, 0)
    {
    }

    /// <summary>
    /// Primitives v2 WS-G0: the exact quantized left-stick vector (BgStickAxis: -1/0/+1, +-2 for a
    /// partial deflection) is part of equality so VERIFY can expect a stick frame. Keyboard-path
    /// frames always carry (0, 0), so every v1 comparison is unchanged.
    /// </summary>
    public BgInputState(IEnumerable<BgButton> down, int mouseX, int mouseY, int scrollWheel, bool padAnalogActive,
        int leftStickX, int leftStickY)
    {
        this.down = new HashSet<BgButton>(down);
        this.MouseX = mouseX;
        this.MouseY = mouseY;
        this.ScrollWheel = scrollWheel;
        this.PadAnalogActive = padAnalogActive;
        this.LeftStickX = leftStickX;
        this.LeftStickY = leftStickY;
    }

    public static BgInputState Neutral(int mouseX = 0, int mouseY = 0, int scrollWheel = 0) =>
        new(Array.Empty<BgButton>(), mouseX, mouseY, scrollWheel, false);

    public IReadOnlyCollection<BgButton> Down => this.down;
    public int MouseX { get; }
    public int MouseY { get; }
    public int ScrollWheel { get; }
    /// <summary>Any pad thumbstick/trigger away from default. Always treated as game-bound.</summary>
    public bool PadAnalogActive { get; }
    /// <summary>WS-G0: quantized left-stick X (BgStickAxis). 0 for every keyboard-path frame.</summary>
    public int LeftStickX { get; }
    /// <summary>WS-G0: quantized left-stick Y (BgStickAxis; +1 = up, the XNA convention). 0 for every keyboard-path frame.</summary>
    public int LeftStickY { get; }

    public bool IsDown(BgButton button) => this.down.Contains(button);

    /// <summary>
    /// Removal-only transform. Never adds a button. Removing any pad input resets
    /// the whole pad to default (V4 N-H: "Pad: default if any pad button is present").
    /// </summary>
    public BgInputState Without(IEnumerable<BgButton> remove, bool resetPad)
    {
        HashSet<BgButton> removeSet = new(remove);
        bool padTouched = resetPad || removeSet.Any(b => b.Device == BgDevice.Pad);
        IEnumerable<BgButton> kept = this.down.Where(b => !removeSet.Contains(b) && !(padTouched && b.Device == BgDevice.Pad));
        return new BgInputState(kept, this.MouseX, this.MouseY, this.ScrollWheel, padTouched ? false : this.PadAnalogActive,
            padTouched ? 0 : this.LeftStickX, padTouched ? 0 : this.LeftStickY);
    }

    public bool Equals(BgInputState? other) =>
        other is not null
        && this.MouseX == other.MouseX
        && this.MouseY == other.MouseY
        && this.ScrollWheel == other.ScrollWheel
        && this.PadAnalogActive == other.PadAnalogActive
        && this.LeftStickX == other.LeftStickX
        && this.LeftStickY == other.LeftStickY
        && this.down.SetEquals(other.down);

    public override bool Equals(object? obj) => obj is BgInputState other && this.Equals(other);

    public override int GetHashCode()
    {
        int hash = HashCode.Combine(this.MouseX, this.MouseY, this.ScrollWheel, this.PadAnalogActive, this.down.Count,
            this.LeftStickX, this.LeftStickY);
        foreach (BgButton b in this.down.OrderBy(b => b.Device).ThenBy(b => b.Name, StringComparer.Ordinal))
            hash = HashCode.Combine(hash, b);
        return hash;
    }

    public string Describe() =>
        "[" + string.Join(",", this.down.OrderBy(b => b.Device).ThenBy(b => b.Name, StringComparer.Ordinal))
        + "] m=" + this.MouseX + "," + this.MouseY + " s=" + this.ScrollWheel + " pad=" + (this.PadAnalogActive ? "active" : "default")
        + (this.LeftStickX != 0 || this.LeftStickY != 0 ? " stick=" + this.LeftStickX + "," + this.LeftStickY : "");
}

/// <summary>Stable result/reason vocabulary for the S1 background layer.</summary>
public static class BgReason
{
    public const string BindingsIncomplete = "BINDINGS_INCOMPLETE";
    public const string AmbiguousBinding = "AMBIGUOUS_BINDING";
    public const string NotFarmhand = "NOT_FARMHAND";
    public const string SplitScreen = "SPLIT_SCREEN";
    public const string GameActive = "GAME_ACTIVE";
    public const string WorldNotReady = "WORLD_NOT_READY";
    public const string MenuOpen = "MENU_OPEN";
    public const string EventOrMinigame = "EVENT_OR_MINIGAME";
    public const string Chatting = "CHATTING";
    public const string ToolInteraction = "TOOL_INTERACTION";
    public const string UsingSlingshot = "USING_SLINGSHOT";
    public const string Fishing = "FISHING";
    public const string CannotMove = "CANNOT_MOVE";
    public const string FreezePause = "FREEZE_PAUSE";
    public const string GlobalFade = "GLOBAL_FADE";
    public const string FreezeControls = "FREEZE_CONTROLS";
    public const string PanMode = "PAN_MODE";
    public const string GamepadMode = "GAMEPAD_MODE";
    /// <summary>WS-G0 pad path: gamepadControls must be on (ARTI owns ForceOn) for every pad request.</summary>
    public const string GamepadModeNotOwned = "GAMEPAD_MODE_NOT_OWNED";
    public const string RequestBindingInvalid = "REQUEST_BINDING_INVALID";
    public const string HookChainNotAdmitted = "HOOK_CHAIN_NOT_ADMITTED";
    public const string LatchFieldNotAdmitted = "LATCH_FIELD_NOT_ADMITTED";
    public const string RetainedNotNeutral = "RETAINED_NOT_NEUTRAL";
    public const string StaleMotionUnresolved = "STALE_MOTION_UNRESOLVED";
    public const string UnexpectedActionEdge = "UNEXPECTED_ACTION_EDGE";
    public const string N0PlanViolation = "N0_PLAN_VIOLATION";
    public const string PostN0HistoryNotCanonical = "POST_N0_HISTORY_NOT_CANONICAL";
    public const string ForeignHistory = "FOREIGN_HISTORY";
    public const string PreconsumerHistoryDivergence = "PRECONSUMER_HISTORY_DIVERGENCE";
    public const string InterruptedFocusRegained = "INTERRUPTED_FOCUS_REGAINED";

    public static string PreconditionUnavailable(string reason) => "PRECONDITION_UNAVAILABLE(" + reason + ")";
}

/// <summary>
/// V4 C1 binding classification over every Options InputButton[] field.
/// MOVE_BOUND = the four movement fields; NONMOVE_BOUND = every other field plus
/// ALL mouse and pad buttons; MOVE_ONLY = keyboard keys in MOVE_BOUND, in no
/// non-movement field, and in exactly one movement direction.
/// </summary>
public sealed class BgBindingTable
{
    public const string MoveUpField = "moveUpButton";
    public const string MoveDownField = "moveDownButton";
    public const string MoveLeftField = "moveLeftButton";
    public const string MoveRightField = "moveRightButton";

    private readonly Dictionary<BgDirection, IReadOnlyList<BgButton>> moveByDirection;
    private readonly HashSet<BgButton> moveBound;
    private readonly HashSet<BgButton> nonMoveFieldBound;
    private readonly IReadOnlyDictionary<string, IReadOnlyList<BgButton>> fields;
    private readonly bool allKeysBound;

    private BgBindingTable(Dictionary<BgDirection, IReadOnlyList<BgButton>> moveByDirection, HashSet<BgButton> nonMoveFieldBound,
        IReadOnlyDictionary<string, IReadOnlyList<BgButton>> fields, bool allKeysBound)
    {
        this.moveByDirection = moveByDirection;
        this.moveBound = new HashSet<BgButton>(moveByDirection.Values.SelectMany(v => v));
        this.nonMoveFieldBound = nonMoveFieldBound;
        this.fields = fields;
        this.allKeysBound = allKeysBound;
    }

    public static string FieldFor(BgDirection direction) => direction switch
    {
        BgDirection.Up => MoveUpField,
        BgDirection.Down => MoveDownField,
        BgDirection.Left => MoveLeftField,
        _ => MoveRightField,
    };

    /// <summary>Fields = every Options InputButton[] field, enumerated by reflection in S2.</summary>
    public static bool TryCreate(IReadOnlyDictionary<string, IReadOnlyList<BgButton>> fields, out BgBindingTable? table, out string? reason)
    {
        table = null;
        reason = null;
        Dictionary<BgDirection, IReadOnlyList<BgButton>> move = new();
        foreach (BgDirection d in new[] { BgDirection.Up, BgDirection.Down, BgDirection.Left, BgDirection.Right })
        {
            if (!fields.TryGetValue(FieldFor(d), out IReadOnlyList<BgButton>? buttons) || buttons is null)
            {
                reason = BgReason.BindingsIncomplete;
                return false;
            }
            move[d] = buttons.ToArray();
        }
        HashSet<BgButton> nonMove = new();
        Dictionary<string, IReadOnlyList<BgButton>> copy = new(StringComparer.Ordinal);
        foreach (KeyValuePair<string, IReadOnlyList<BgButton>> field in fields)
        {
            copy[field.Key] = (field.Value ?? Array.Empty<BgButton>()).ToArray();
            if (field.Key is MoveUpField or MoveDownField or MoveLeftField or MoveRightField)
                continue;
            foreach (BgButton b in field.Value ?? Array.Empty<BgButton>())
                nonMove.Add(b);
        }
        table = new BgBindingTable(move, nonMove, copy, allKeysBound: false);
        return true;
    }

    /// <summary>
    /// Stage 2 hold plans only: the same table, but EVERY keyboard key counts as
    /// game-bound for N-R / N-H / retained-neutral / bound-free-B purposes. The
    /// installed UpdateControlInput hardcodes keys outside Options (review m5), and
    /// VERIFY can only be exact when the retained frame and B carry no button at
    /// all. Release-only (Suppress) and removal-only (old*) semantics are unchanged;
    /// MOVE_ONLY classification (and therefore the N0 stale-movement release) is unchanged.
    /// </summary>
    public BgBindingTable ForHoldNeutralization() =>
        new(this.moveByDirection, this.nonMoveFieldBound, this.fields, allKeysBound: true);

    /// <summary>True for a table produced by <see cref="ForHoldNeutralization"/>.</summary>
    public bool AllKeysBound => this.allKeysBound;

    /// <summary>The normalized buttons of one Options field (empty when absent).</summary>
    public IReadOnlyList<BgButton> Field(string name) =>
        this.fields.TryGetValue(name, out IReadOnlyList<BgButton>? v) ? v : Array.Empty<BgButton>();

    /// <summary>Every Options field (by name) whose binding contains <paramref name="b"/>.</summary>
    public IReadOnlyList<string> FieldsContaining(BgButton b) =>
        this.fields.Where(kv => kv.Value.Contains(b)).Select(kv => kv.Key).OrderBy(n => n, StringComparer.Ordinal).ToArray();

    public IReadOnlyCollection<BgButton> MoveBound => this.moveBound;

    public IReadOnlyList<BgButton> MoveButtons(BgDirection direction) => this.moveByDirection[direction];

    /// <summary>Kb membership: any bound keyboard key, and every mouse/pad button.</summary>
    public bool IsBound(BgButton b) =>
        this.allKeysBound || b.Device != BgDevice.Keyboard || this.moveBound.Contains(b) || this.nonMoveFieldBound.Contains(b);

    public bool IsMoveBound(BgButton b) => this.moveBound.Contains(b);

    public bool IsNonMoveBound(BgButton b) => b.Device != BgDevice.Keyboard || this.nonMoveFieldBound.Contains(b)
        || (this.allKeysBound && !this.moveBound.Contains(b));

    public bool IsMoveOnly(BgButton b) =>
        b.Device == BgDevice.Keyboard
        && this.moveBound.Contains(b)
        && !this.nonMoveFieldBound.Contains(b)
        && this.moveByDirection.Count(kv => kv.Value.Contains(b)) == 1;

    public BgDirection? MoveOnlyDirection(BgButton b)
    {
        if (!this.IsMoveOnly(b))
            return null;
        return this.moveByDirection.First(kv => kv.Value.Contains(b)).Key;
    }

    /// <summary>Synthetic key rule: MOVE_ONLY, keyboard, requested direction only, not Escape.</summary>
    public string? ValidateSyntheticKey(BgDirection direction, BgButton key)
    {
        if (key.Device != BgDevice.Keyboard || key.IsEscape)
            return BgReason.AmbiguousBinding;
        if (!this.IsMoveOnly(key) || this.MoveOnlyDirection(key) != direction)
            return BgReason.AmbiguousBinding;
        return null;
    }

    /// <summary>Deterministic choice: first valid key in the direction's Options field order.</summary>
    public bool TrySelectSyntheticKey(BgDirection direction, out BgButton key, out string? reason)
    {
        foreach (BgButton candidate in this.moveByDirection[direction])
        {
            if (this.ValidateSyntheticKey(direction, candidate) is null)
            {
                key = candidate;
                reason = null;
                return true;
            }
        }
        key = default;
        reason = BgReason.AmbiguousBinding;
        return false;
    }
}

/// <summary>
/// Normalized admission / N-pre facts sampled by S2. <c>GameActive</c> is a recorded fact only:
/// window focus is not an admission term (ADR 244); the same verified path serves both states.
/// </summary>
public sealed record BgAdmissionFacts(
    bool IsMainPlayer,
    bool IsSplitScreen,
    bool GameActive,
    bool WorldReady,
    bool MenuOpen,
    bool EventOrMinigame,
    bool Chatting,
    bool UsingTool,
    bool UsingSlingshot,
    bool Fishing,
    bool CanMove,
    bool FreezePause,
    bool GlobalFade,
    bool FreezeControls,
    bool PanMode,
    bool GamepadMode,
    bool RequestBindingValid,
    bool HookChainAdmitted,
    bool LatchFieldAdmitted,
    // WS-G0: the pad path REQUIRES gamepad mode (ARTI-owned ForceOn); the keyboard path refuses it.
    bool PadModeRequired = false);

public static class BgAdmission
{
    /// <summary>First failing stage-1 admission reason, or null. Residuals are NOT YET DELIVERED, not final refusals.</summary>
    public static string? Reject(BgAdmissionFacts f)
    {
        if (f.IsMainPlayer) return BgReason.NotFarmhand;
        if (f.IsSplitScreen) return BgReason.SplitScreen;
        // ADR 244: no GameActive term. The window being the active one is never a refusal by itself.
        if (!f.WorldReady) return BgReason.WorldNotReady;
        if (f.MenuOpen) return BgReason.MenuOpen;
        if (f.EventOrMinigame) return BgReason.EventOrMinigame;
        if (f.Chatting) return BgReason.Chatting;
        if (f.UsingTool) return BgReason.ToolInteraction;
        if (f.UsingSlingshot) return BgReason.UsingSlingshot;
        if (f.Fishing) return BgReason.Fishing;
        if (!f.CanMove) return BgReason.CannotMove;
        if (f.FreezePause) return BgReason.FreezePause;
        if (f.GlobalFade) return BgReason.GlobalFade;
        if (f.FreezeControls) return BgReason.FreezeControls;
        if (f.PanMode) return BgReason.PanMode;
        if (f.PadModeRequired ? !f.GamepadMode : f.GamepadMode)
            return f.PadModeRequired ? BgReason.GamepadModeNotOwned : BgReason.GamepadMode;
        if (!f.RequestBindingValid) return BgReason.RequestBindingInvalid;
        if (!f.HookChainAdmitted) return BgReason.HookChainNotAdmitted;
        if (!f.LatchFieldAdmitted) return BgReason.LatchFieldNotAdmitted;
        return null;
    }
}

/// <summary>V4 B2 + C1 STALE_NEUTRALIZE plan (N-R suppress set, N-H removal-only history set).</summary>
public sealed record BgStaleNeutralizePlan(
    string? Unavailable,
    BgButton SyntheticKey,
    IReadOnlyList<BgButton> SuppressRetained,
    IReadOnlyList<BgButton> RemoveFromHistory,
    bool ResetPadHistory,
    IReadOnlyList<BgButton> StaleMoveOnlyKept,
    IReadOnlyList<BgButton> UnboundIgnored,
    BgInputState? HistoryBefore,
    BgInputState? HistoryAfterNH)
{
    public bool Admitted => this.Unavailable is null;
    public string? Label => this.Unavailable is null ? null : BgReason.PreconditionUnavailable(this.Unavailable);
}

public static class BgStaleNeutralize
{
    private static IReadOnlyList<BgButton> Sorted(IEnumerable<BgButton> buttons) =>
        buttons.OrderBy(b => b.Device).ThenBy(b => b.Name, StringComparer.Ordinal).ToArray();

    public static BgStaleNeutralizePlan Plan(
        BgBindingTable bindings, BgAdmissionFacts facts, BgDirection direction, BgInputState retained, BgInputState history)
    {
        string? rejection = BgAdmission.Reject(facts);
        BgButton key = default;
        if (rejection is null && !bindings.TrySelectSyntheticKey(direction, out key, out rejection))
            rejection ??= BgReason.AmbiguousBinding;
        return Build(bindings, rejection, key, retained, history);
    }

    /// <summary>
    /// WS-G0 pad variant: identical N-R / N-H construction for a fixed pad pin (X / A / stick / D-pad).
    /// Use with <see cref="BgBindingTable.ForHoldNeutralization"/>: every retained keyboard key and
    /// mouse button is released (retained keys re-show the cursor in gamepad mode, G1:12670-12673)
    /// and the pad history is reset (N-H clears oldPadState edges, PLAN section 1.5 d).
    /// </summary>
    public static BgStaleNeutralizePlan PlanForPadKey(
        BgBindingTable bindings, BgAdmissionFacts facts, BgButton padKey, BgInputState retained, BgInputState history)
    {
        string? rejection = BgAdmission.Reject(facts);
        if (rejection is null && !BgPadButtons.IsHoldPin(padKey))
            rejection = BgReason.AmbiguousBinding;
        return Build(bindings, rejection, padKey, retained, history);
    }

    /// <summary>
    /// Stage 2 hold variant: identical N-R / N-H construction for an already
    /// derived TOOL_ONLY / ACTION_ONLY keyboard key (no movement-key selection).
    /// </summary>
    public static BgStaleNeutralizePlan PlanForKey(
        BgBindingTable bindings, BgAdmissionFacts facts, BgButton key, BgInputState retained, BgInputState history)
    {
        string? rejection = BgAdmission.Reject(facts);
        if (rejection is null && (key.Device != BgDevice.Keyboard || key.IsEscape))
            rejection = BgReason.AmbiguousBinding;
        return Build(bindings, rejection, key, retained, history);
    }

    private static BgStaleNeutralizePlan Build(
        BgBindingTable bindings, string? rejection, BgButton key, BgInputState retained, BgInputState history)
    {
        if (rejection is not null)
            return new BgStaleNeutralizePlan(rejection, default, Array.Empty<BgButton>(), Array.Empty<BgButton>(), false,
                Array.Empty<BgButton>(), Array.Empty<BgButton>(), history, null);

        // N-R: every retained game-bound input is released through Suppress (release-only).
        IReadOnlyList<BgButton> suppress = Sorted(retained.Down.Where(bindings.IsBound));

        // N-H: remove every stale bound input from old* except exclusive MOVE_ONLY keys.
        IReadOnlyList<BgButton> remove = Sorted(history.Down.Where(b => bindings.IsBound(b) && !bindings.IsMoveOnly(b)));
        IReadOnlyList<BgButton> keptMoveOnly = Sorted(history.Down.Where(bindings.IsMoveOnly));
        IReadOnlyList<BgButton> unbound = Sorted(history.Down.Concat(retained.Down).Distinct().Where(b => !bindings.IsBound(b)));
        bool resetPad = history.PadAnalogActive || history.LeftStickX != 0 || history.LeftStickY != 0
            || history.Down.Any(b => b.Device == BgDevice.Pad);
        BgInputState after = history.Without(remove, resetPad);

        return new BgStaleNeutralizePlan(null, key, suppress, remove, resetPad, keptMoveOnly, unbound, history, after);
    }

    /// <summary>Retained layer after N-R: no bound input down and pad at default.</summary>
    public static string? VerifyRetainedNeutral(BgBindingTable bindings, BgInputState retainedAfterSuppress) =>
        retainedAfterSuppress.PadAnalogActive || retainedAfterSuppress.LeftStickX != 0 || retainedAfterSuppress.LeftStickY != 0
            || retainedAfterSuppress.Down.Any(bindings.IsBound)
            ? BgReason.RetainedNotNeutral
            : null;

    /// <summary>Release edges N0 (all-neutral current vs history) will produce. Pressed edges are impossible.</summary>
    public static IReadOnlyList<BgButton> ExpectedN0ReleaseEdges(BgBindingTable bindings, BgInputState historyBeforeN0) =>
        Sorted(historyBeforeN0.Down.Where(bindings.IsBound));
}

public enum BgStepKind { Proceed, PreconditionUnavailable, ContainmentRequired, Uncertain }

/// <summary>Outcome of a pre-H lifecycle step. No outcome other than Proceed may issue an H frame.</summary>
public readonly record struct BgStepResult(BgStepKind Kind, string? Reason)
{
    public static BgStepResult Ok => new(BgStepKind.Proceed, null);
    public bool IssueH => this.Kind == BgStepKind.Proceed;
    public bool ArmPreHContainment => this.Kind == BgStepKind.ContainmentRequired;
    public bool KeepNeutralCleanup => this.Kind == BgStepKind.ContainmentRequired;

    public string Label => this.Kind switch
    {
        BgStepKind.Proceed => "PROCEED",
        BgStepKind.PreconditionUnavailable => BgReason.PreconditionUnavailable(this.Reason ?? "UNKNOWN"),
        // CT override: unresolved stale motion is PRECONDITION_UNAVAILABLE with the
        // neutral-cleanup path kept and pre-H containment armed. Never a completion.
        BgStepKind.ContainmentRequired => BgReason.PreconditionUnavailable(this.Reason ?? "UNKNOWN") + "+CONTAINMENT_REQUIRED",
        _ => "UNCERTAIN(" + (this.Reason ?? "UNKNOWN") + ")",
    };
}

/// <summary>Independent readback after the N0 neutral frame.</summary>
public readonly record struct BgN0Observation(bool UnexpectedActionEdgeObserved, int MovementDirectionsCount, bool NoContinuingMotion);

public static class BgN0
{
    public static BgStepResult Evaluate(BgBindingTable bindings, BgInputState historyBeforeN0, BgN0Observation observed)
    {
        bool planViolation = BgStaleNeutralize.ExpectedN0ReleaseEdges(bindings, historyBeforeN0).Any(b => !bindings.IsMoveOnly(b))
            || historyBeforeN0.PadAnalogActive;
        bool stopped = observed.MovementDirectionsCount == 0 && observed.NoContinuingMotion;
        if (!stopped)
            return new BgStepResult(BgStepKind.ContainmentRequired, BgReason.StaleMotionUnresolved);
        if (planViolation)
            return new BgStepResult(BgStepKind.Uncertain, BgReason.N0PlanViolation);
        if (observed.UnexpectedActionEdgeObserved)
            return new BgStepResult(BgStepKind.Uncertain, BgReason.UnexpectedActionEdge);
        return BgStepResult.Ok;
    }
}

/// <summary>Facts sampled for POST_N0_CANONICALIZE (V4.1 D1), after N0 and before H1.</summary>
public sealed record BgCanonicalizeFacts(
    bool GameActive,
    bool OwnershipExact,
    BgStepResult N0Result,
    BgInputState RetainedNow,
    BgInputState RetainedSnapshot,
    BgInputState HistoryNow);

public sealed record BgCanonicalizeResult(
    BgStepResult Step,
    IReadOnlyList<BgButton> Removed,
    BgInputState? HistoryBefore,
    BgInputState? Baseline);

public static class BgPostN0Canonicalize
{
    public static BgCanonicalizeResult Plan(BgBindingTable bindings, BgCanonicalizeFacts f)
    {
        // H1 never precedes a proved N0 stop. Unresolved motion keeps the frozen pre-H containment path.
        if (f.N0Result.Kind == BgStepKind.ContainmentRequired)
            return Fail(f, f.N0Result);
        if (f.N0Result.Kind != BgStepKind.Proceed)
            return Fail(f, f.N0Result);

        BgStepResult notCanonical = new(BgStepKind.PreconditionUnavailable, BgReason.PostN0HistoryNotCanonical);
        // ADR 244: GameActive is not a term; the retained-frame equality below is what proves a quiet start.
        if (!f.OwnershipExact)
            return Fail(f, notCanonical);
        if (!f.RetainedNow.Equals(f.RetainedSnapshot) || BgStaleNeutralize.VerifyRetainedNeutral(bindings, f.RetainedNow) is not null)
            return Fail(f, notCanonical);

        // Remove-only: drop remaining stale MOVE_ONLY history; add nothing.
        IReadOnlyList<BgButton> removed = f.HistoryNow.Down.Where(bindings.IsMoveOnly)
            .OrderBy(b => b.Name, StringComparer.Ordinal).ToArray();
        BgInputState baseline = f.HistoryNow.Without(removed, resetPad: false);

        // B must be bound-free. Never redefine B around stale bound input.
        if (baseline.PadAnalogActive || baseline.Down.Any(bindings.IsBound))
            return Fail(f, notCanonical);

        return new BgCanonicalizeResult(BgStepResult.Ok, removed, f.HistoryNow, baseline);
    }

    /// <summary>After S2 writes the removal, the re-sampled history must equal the planned B exactly.</summary>
    public static BgStepResult ConfirmWrite(BgCanonicalizeResult planned, BgInputState resampled) =>
        planned.Step.IssueH && planned.Baseline is not null && planned.Baseline.Equals(resampled)
            ? BgStepResult.Ok
            : new BgStepResult(BgStepKind.PreconditionUnavailable, BgReason.PostN0HistoryNotCanonical);

    private static BgCanonicalizeResult Fail(BgCanonicalizeFacts f, BgStepResult step) =>
        new(step, Array.Empty<BgButton>(), f.HistoryNow, null);
}

/// <summary>S_k: KB = {move key} or empty; mouse = B's X/Y, buttons released, B's scroll; pad default.</summary>
public static class BgSyntheticFrame
{
    public static BgInputState Build(BgInputState baseline, BgButton? key) =>
        new(key is null ? Array.Empty<BgButton>() : new[] { key.Value }, baseline.MouseX, baseline.MouseY, baseline.ScrollWheel, false);
}

public enum BgHistoryBranch { Unpinned, Yes, No }

public readonly record struct BgHistoryCheck(bool Allowed, string? Reason, BgHistoryBranch Observed);

/// <summary>
/// V4 B1 ownership-aware history invariant with branch pinning. Before the first
/// consumed frame H must equal B exactly. After consumed frame k, H must equal B
/// (NO / not written) or S_k (YES). The first distinguishing branch is pinned;
/// a switch, any third state, or any retained-layer change is FOREIGN_HISTORY.
/// </summary>
public sealed class BgHistoryInvariant
{
    private readonly BgBindingTable bindings;

    private BgHistoryInvariant(BgBindingTable bindings, BgInputState baseline, BgInputState retainedSnapshot)
    {
        this.bindings = bindings;
        this.Baseline = baseline;
        this.RetainedSnapshot = retainedSnapshot;
    }

    public BgInputState Baseline { get; }
    public BgInputState RetainedSnapshot { get; }
    public BgHistoryBranch Branch { get; private set; } = BgHistoryBranch.Unpinned;
    public BgInputState? PreviousSynthetic { get; private set; }
    public int ConsumedFrames { get; private set; }

    public static bool TryCreate(BgBindingTable bindings, BgInputState baseline, BgInputState retainedSnapshot,
        out BgHistoryInvariant? invariant, out string? reason)
    {
        invariant = null;
        if (baseline.PadAnalogActive || baseline.Down.Any(bindings.IsBound))
        {
            reason = BgReason.PostN0HistoryNotCanonical;
            return false;
        }
        if (BgStaleNeutralize.VerifyRetainedNeutral(bindings, retainedSnapshot) is string retainedReason)
        {
            reason = retainedReason;
            return false;
        }
        reason = null;
        invariant = new BgHistoryInvariant(bindings, baseline, retainedSnapshot);
        return true;
    }

    public BgHistoryCheck Check(BgInputState historyNow, BgInputState retainedNow)
    {
        if (!retainedNow.Equals(this.RetainedSnapshot) || BgStaleNeutralize.VerifyRetainedNeutral(this.bindings, retainedNow) is not null)
            return new BgHistoryCheck(false, BgReason.ForeignHistory, BgHistoryBranch.Unpinned);

        if (this.PreviousSynthetic is null)
        {
            return historyNow.Equals(this.Baseline)
                ? new BgHistoryCheck(true, null, BgHistoryBranch.Unpinned)
                : new BgHistoryCheck(false, BgReason.ForeignHistory, BgHistoryBranch.Unpinned);
        }

        bool isB = historyNow.Equals(this.Baseline);
        bool isS = historyNow.Equals(this.PreviousSynthetic);
        if (!isB && !isS)
            return new BgHistoryCheck(false, BgReason.ForeignHistory, BgHistoryBranch.Unpinned);
        if (isB && isS)
            return new BgHistoryCheck(true, null, BgHistoryBranch.Unpinned);
        BgHistoryBranch observed = isS ? BgHistoryBranch.Yes : BgHistoryBranch.No;
        if (this.Branch != BgHistoryBranch.Unpinned && this.Branch != observed)
            return new BgHistoryCheck(false, BgReason.ForeignHistory, observed);
        return new BgHistoryCheck(true, null, observed);
    }

    /// <summary>
    /// Stage 2 VERIFY role: the same B / S_k branch-pinned history rule without the
    /// retained-snapshot clause. In VERIFY the retained frame is compared, in full,
    /// against the exact expected owned frame instead (see BgHookEntry).
    /// </summary>
    public BgHistoryCheck CheckHistory(BgInputState historyNow)
    {
        if (this.PreviousSynthetic is null)
        {
            return historyNow.Equals(this.Baseline)
                ? new BgHistoryCheck(true, null, BgHistoryBranch.Unpinned)
                : new BgHistoryCheck(false, BgReason.ForeignHistory, BgHistoryBranch.Unpinned);
        }
        bool isB = historyNow.Equals(this.Baseline);
        bool isS = historyNow.Equals(this.PreviousSynthetic);
        if (!isB && !isS)
            return new BgHistoryCheck(false, BgReason.ForeignHistory, BgHistoryBranch.Unpinned);
        if (isB && isS)
            return new BgHistoryCheck(true, null, BgHistoryBranch.Unpinned);
        BgHistoryBranch observed = isS ? BgHistoryBranch.Yes : BgHistoryBranch.No;
        if (this.Branch != BgHistoryBranch.Unpinned && this.Branch != observed)
            return new BgHistoryCheck(false, BgReason.ForeignHistory, observed);
        return new BgHistoryCheck(true, null, observed);
    }

    /// <summary>Record that <paramref name="synthetic"/> was handed to the consumer after an allowed check.</summary>
    public void RecordConsumed(BgInputState synthetic, BgHistoryCheck check)
    {
        if (!check.Allowed)
            throw new InvalidOperationException("history check was not allowed; a divergent frame is never consumed");
        if (this.Branch == BgHistoryBranch.Unpinned && check.Observed != BgHistoryBranch.Unpinned)
            this.Branch = check.Observed;
        this.PreviousSynthetic = synthetic;
        this.ConsumedFrames++;
    }
}

public enum BgFrameKind { H, R, N, Done }

/// <summary>
/// Stage 2 m6: the frame-plan abstraction the pre-consumer decision table accepts.
/// <see cref="Verify"/> false = SUBSTITUTE role (S2 movement). true = VERIFY role
/// (hold plan; the expected frame is compared with the retained frame, never written).
/// </summary>
public interface IBgFramePlan
{
    bool Verify { get; }
    BgButton Key { get; }
    int TargetHFrames { get; }
    int ConsumedH { get; }
    bool RConsumed { get; }
    bool NConsumed { get; }
    string? ReleaseReason { get; }
    BgFrameKind Next { get; }
    bool Active { get; }
    BgFrameKind EffectiveNext(bool stopOrLeaseInvalid);
    void RequestRelease(string reason);
    void OnConsumed(BgFrameKind kind);
    BgInputState FrameFor(BgFrameKind kind, BgInputState baseline);
}

/// <summary>
/// Bounded H/R/N movement frame plan. The consumption clock is actual substituted
/// hook callbacks, never SMAPI update ticks. STOP / lease loss / deadline only
/// move the plan toward R/N; they never skip release verification.
/// </summary>
public sealed class BgMoveFramePlan : IBgFramePlan
{
    public bool Verify => this.verify;

    public const int MinTicks = 1;
    public const int MaxTicks = 60;

    /// <param name="segmentHold">fix89D / ADR 158: a segment-guarded hold (move_cardinal with segment_stop)
    /// may run up to <see cref="MoveSegmentGuard.MaxTicks"/>; every other move keeps 1..60.</param>
    /// <param name="pathHold">fix92A / ADR 162: a guarded path (segment_stop + path_next, pad VERIFY only) may run
    /// up to <see cref="MovePath.MaxTicks"/> H frames over all its segments.</param>
    public BgMoveFramePlan(BgDirection direction, BgButton key, int targetHFrames, bool segmentHold = false, bool pathHold = false)
    {
        if (pathHold && !segmentHold)
            throw new ArgumentException("a path hold is a segment hold", nameof(pathHold));
        int max = pathHold ? MovePath.MaxTicks : segmentHold ? MoveSegmentGuard.MaxTicks : MaxTicks;
        if (targetHFrames < MinTicks || targetHFrames > max)
            throw new ArgumentOutOfRangeException(nameof(targetHFrames), "background movement ticks must stay within 1..60 (segment hold 1..120, path 1..180)");
        this.Direction = direction;
        this.Key = key;
        this.TargetHFrames = targetHFrames;
        this.PathHold = pathHold;
    }

    /// <summary>fix92A / ADR 162: this plan may turn at a reached segment stop (see <see cref="BeginNextSegment"/>).</summary>
    public bool PathHold { get; }

    /// <summary>fix92A: turns taken so far (one per segment after the first).</summary>
    public int Turns { get; private set; }

    /// <summary>
    /// fix92A / ADR 162: the only way H resumes after a release. Allowed exactly when this is a VERIFY path plan,
    /// its last segment ended on SEGMENT_STOP_REACHED, the R frame (button-free, VERIFY-admitted) was consumed,
    /// N was not, and frames remain under the path cap. The new pin is perpendicular to the old one. The caller
    /// must already hold the old pin's release proof and must open a new single-Press session for the new pin;
    /// the plan only changes which pin the next H frame expects. Returns false (plan unchanged, so it goes on
    /// to N) in every other state.
    /// </summary>
    public bool BeginNextSegment(BgDirection direction, BgButton key)
    {
        if (!this.verify || !this.PathHold || this.NConsumed || !this.RConsumed || this.ConsumedH < 1
            || this.ConsumedH >= this.TargetHFrames
            || !string.Equals(this.ReleaseReason, MoveSegmentGuard.ReasonReached, StringComparison.Ordinal)
            || !BgPadButtons.IsHoldPin(key) || key.Equals(this.Key)
            || (direction is BgDirection.Left or BgDirection.Right) == (this.Direction is BgDirection.Left or BgDirection.Right))
            return false;
        this.Direction = direction;
        this.Key = key;
        this.ReleaseReason = null;
        this.RConsumed = false;
        this.Turns++;
        return true;
    }

    /// <summary>
    /// WS-G0 pad move: the same bounded H/R/N plan in the VERIFY role. The runtime holds ONE SMAPI
    /// Press of the left-stick direction while the next expected frame is H; the hook compares the
    /// retained frame with B + {stick vector} and never substitutes. The keyboard move (SUBSTITUTE
    /// role) is untouched.
    /// </summary>
    public static BgMoveFramePlan PadVerify(BgDirection direction, int targetHFrames, bool segmentHold = false, bool pathHold = false) =>
        new(direction, BgPadButtons.MovePin(direction), targetHFrames, segmentHold, pathHold) { verify = true };

    private bool verify;

    public BgDirection Direction { get; private set; }
    public BgButton Key { get; private set; }
    public int TargetHFrames { get; }
    public int ConsumedH { get; private set; }
    public bool RConsumed { get; private set; }
    public bool NConsumed { get; private set; }
    public string? ReleaseReason { get; private set; }

    public BgFrameKind Next
    {
        get
        {
            if (this.NConsumed) return BgFrameKind.Done;
            if (this.ReleaseReason is null && this.ConsumedH < this.TargetHFrames) return BgFrameKind.H;
            if (this.ConsumedH > 0 && !this.RConsumed) return BgFrameKind.R;
            return BgFrameKind.N;
        }
    }

    public bool Active => this.Next != BgFrameKind.Done;

    /// <summary>WS-G0 pad move: the runtime holds the stick Press exactly while the next expected frame is H.</summary>
    public bool KeyShouldBeDown => this.verify && this.Next == BgFrameKind.H;

    /// <summary>Frame kind actually served when STOP / invalid lease is latched at hook entry.</summary>
    public BgFrameKind EffectiveNext(bool stopOrLeaseInvalid)
    {
        BgFrameKind next = this.Next;
        if (next == BgFrameKind.H && stopOrLeaseInvalid)
            return this.ConsumedH > 0 ? BgFrameKind.R : BgFrameKind.N;
        return next;
    }

    public void RequestRelease(string reason) => this.ReleaseReason ??= reason;

    public void OnConsumed(BgFrameKind kind)
    {
        if (kind != this.Next)
            throw new InvalidOperationException("consumed frame " + kind + " does not match plan " + this.Next);
        switch (kind)
        {
            case BgFrameKind.H: this.ConsumedH++; break;
            case BgFrameKind.R: this.RConsumed = true; break;
            case BgFrameKind.N: this.NConsumed = true; break;
            default: throw new InvalidOperationException("plan already done");
        }
    }

    public BgInputState FrameFor(BgFrameKind kind, BgInputState baseline) => this.verify
        ? BgPadFrame.Build(baseline, kind == BgFrameKind.H ? this.Key : null)
        : BgSyntheticFrame.Build(baseline, kind == BgFrameKind.H ? this.Key : null);
}

public enum BgHookAction
{
    PassThroughForeground, PassThroughNoGrant, SubstituteHeld, SubstituteNeutral, SkipDivergence,
    /// <summary>Stage 2 VERIFY: retained frame == expected B + {key}; Parent once with the original refs.</summary>
    VerifyHeld,
    /// <summary>Stage 2 VERIFY: retained frame == expected neutral B; Parent once with the original refs.</summary>
    VerifyNeutral,
}

/// <summary>Facts sampled at ArtiControlInputHooks.OnGame1_UpdateControlInput entry, before substitution/Parent.</summary>
public sealed record BgHookFacts(
    bool GameActive,
    bool LatchOwned,
    bool HookOwned,
    long FrameTokenSeq,
    long CurrentUpdateSeq,
    bool FrameTokenConsumed,
    BgInputState HistoryNow,
    BgInputState RetainedNow,
    bool RequestBindingValid,
    bool GameGatesValid,
    bool LeaseValid,
    bool StopLatched,
    BgHoldHookFacts? Hold = null,
    // WS-G0 pad move (VERIFY role): the first failing pad term at this hook entry (cursor hidden), or null.
    string? PadMoveGate = null);

/// <summary>
/// Live7 rehearsal 9 (T2 FailFast race): the stop / deadline / lease view decided ONCE per tick in
/// UpdateTicking, before the owned press decision. A VERIFY-role hook must judge its frame with the
/// same view the press decision used: re-reading the clock (or a cross-thread STOP / lease) inside
/// the hook could expect R while this tick's press still holds H, which is a self-made mismatch,
/// dispatch impossible and T2 containment. A change seen after the snapshot takes effect on the
/// NEXT tick (Ticking signals release, the press is Suppressed, the hook expects R). The snapshot
/// applies only to the exact tick and request it was taken for. Once the plan has consumed an H frame
/// it supplies StopLatched and LeaseValid. Before the first H (Live7 follow-up) it supplies the
/// deadline and lease this tick's press decision used, so a deadline / lease edge between Ticking and
/// the hook no longer refuses a press Ticking already admitted (it releases cleanly next tick); a
/// hook-time owner STOP or release still refuses the press (no new world effect starts after a STOP,
/// and nothing is moving yet, so no T2). Otherwise the live facts stand (fail closed). Genuine frame
/// mismatches are unaffected and still diverge.
/// </summary>
public readonly record struct BgTickStopSnapshot(
    long UpdateSeq,
    string RequestId,
    long NowUnixMs,
    bool ReleasePending,
    bool OwnerStopLatched,
    long DeadlineUnixMs,
    bool LeaseValid)
{
    public bool DeadlinePassed => this.NowUnixMs > this.DeadlineUnixMs;

    /// <summary>Same composition the hook used to compute live: release || owner STOP || deadline passed.</summary>
    public bool StopLatched => this.ReleasePending || this.OwnerStopLatched || this.DeadlinePassed;

    /// <summary>True when this snapshot was taken for <paramref name="updateSeq"/> and <paramref name="requestId"/>.</summary>
    public bool AppliesTo(long updateSeq, string requestId) =>
        this.UpdateSeq == updateSeq && string.Equals(this.RequestId, requestId, StringComparison.Ordinal);

    /// <summary>
    /// VERIFY-role hook facts: StopLatched / LeaseValid come from this tick's snapshot once the press is
    /// in progress (<paramref name="consumedH"/> &gt; 0). Before the first H, when the caller passes
    /// <paramref name="liveStopOrRelease"/> (the hook-time owner STOP / release view, deadline excluded),
    /// the deadline and lease come from the snapshot and StopLatched = snapshot stop || live STOP / release.
    /// Any other tick or request, or no pre-H stop view, keeps the live facts unchanged.
    /// </summary>
    public static BgHookFacts ApplyForTick(BgTickStopSnapshot? snapshot, string requestId, int consumedH, BgHookFacts live,
        bool? liveStopOrRelease = null)
    {
        if (snapshot is not { } s || !s.AppliesTo(live.CurrentUpdateSeq, requestId))
            return live;
        if (consumedH > 0)
            return live with { StopLatched = s.StopLatched, LeaseValid = s.LeaseValid };
        return liveStopOrRelease is { } stop
            ? live with { StopLatched = s.StopLatched || stop, LeaseValid = s.LeaseValid }
            : live;
    }

    public string Describe() =>
        "seq=" + this.UpdateSeq + " release=" + this.ReleasePending + " owner_stop=" + this.OwnerStopLatched
        + " deadline_minus_now_ms=" + (this.DeadlineUnixMs - this.NowUnixMs) + " lease=" + this.LeaseValid;
}

public sealed record BgHookDecision(
    BgHookAction Action,
    int ParentCalls,
    BgInputState? Substitute,
    BgFrameKind? Frame,
    string? Reason,
    bool DispatchImpossible,
    bool InterruptedFocusRegained,
    BgHistoryCheck? History);

/// <summary>V4 §4 / C2 authoritative pre-consumer decision table.</summary>
public static class BgHookEntry
{
    public static BgHookDecision Decide(Func<BgHookFacts> sample, IBgFramePlan? plan, BgHistoryInvariant? invariant)
    {
        bool grantActive = plan is not null && plan.Active && invariant is not null;
        BgHookFacts facts;
        try
        {
            facts = sample();
        }
        catch
        {
            // An ARTI check exception with an active grant is divergence: skip, never an unverified neutral.
            return grantActive
                ? Divergence("CHECK_EXCEPTION", null)
                : new BgHookDecision(BgHookAction.PassThroughNoGrant, 1, null, null, null, false, false, null);
        }

        // ADR 244: an active window is served by the same decision table (VERIFY / SUBSTITUTE) as an
        // inactive one; a foreign input is caught by the frame comparison, not assumed from focus.
        if (!grantActive)
            return new BgHookDecision(BgHookAction.PassThroughNoGrant, 1, null, null, null, false, false, null);

        try
        {
            if (!facts.LatchOwned) return Divergence("LATCH_NOT_OWNED", null);
            if (!facts.HookOwned) return Divergence("HOOK_NOT_OWNED", null);
            if (facts.FrameTokenSeq != facts.CurrentUpdateSeq || facts.FrameTokenConsumed) return Divergence("FRAME_TOKEN_MISMATCH", null);
            if (!facts.RequestBindingValid) return Divergence("REQUEST_BINDING_INVALID", null);
            if (plan!.Verify)
                return DecideVerify(facts, plan, invariant!);
            if (!facts.GameGatesValid) return Divergence("GAME_GATE_CLOSED", null);

            BgHistoryCheck history = invariant!.Check(facts.HistoryNow, facts.RetainedNow);
            if (!history.Allowed) return Divergence(history.Reason ?? BgReason.ForeignHistory, history);

            BgFrameKind frame = plan!.EffectiveNext(facts.StopLatched || !facts.LeaseValid);
            BgInputState substitute = plan.FrameFor(frame, invariant.Baseline);
            return frame == BgFrameKind.H
                ? new BgHookDecision(BgHookAction.SubstituteHeld, 1, substitute, frame, null, false, false, history)
                : new BgHookDecision(BgHookAction.SubstituteNeutral, 1, substitute, frame,
                    facts.StopLatched ? "STOP_LATCHED" : !facts.LeaseValid ? "LEASE_INVALID" : null, false, false, history);
        }
        catch
        {
            return Divergence("CHECK_EXCEPTION", null);
        }
    }

    /// <summary>
    /// Stage 2 VERIFY table (2.1 section 1, 2.2 section 4 / N4): hold gate (relaxed
    /// facts only after the first consumed press; explicit movement / pin / cursor
    /// checks), branch-pinned history, then FULL BgInputState equality of the
    /// retained frame with the exact expected owned frame. Any difference: skip
    /// Parent, dispatch impossible. Nothing is ever substituted or written.
    /// </summary>
    private static BgHookDecision DecideVerify(BgHookFacts facts, IBgFramePlan plan, BgHistoryInvariant invariant)
    {
        BgFrameKind frame = plan.EffectiveNext(facts.StopLatched || !facts.LeaseValid);
        if (plan is BgMoveFramePlan)
        {
            // WS-G0 pad move: the frozen move gates (GameGatesValid, gamepad mode REQUIRED) plus the
            // cursor-hidden gate sampled into PadMoveGate; movement itself is the expected effect.
            if (!facts.GameGatesValid) return Divergence("GAME_GATE_CLOSED", null);
            if (facts.PadMoveGate is string padGate) return Divergence(padGate, null);
        }
        else
        {
            if (facts.Hold is not { } hold)
                return Divergence(BgHoldReason.HoldFactsMissing, null);
            if (BgHoldGate.Reject(hold, plan.ConsumedH > 0, frame) is string gate)
                return Divergence(gate, null);
        }
        BgHistoryCheck history = invariant.CheckHistory(facts.HistoryNow);
        if (!history.Allowed)
            return Divergence(history.Reason ?? BgReason.ForeignHistory, history);
        BgInputState expected = plan.FrameFor(frame, invariant.Baseline);
        if (!facts.RetainedNow.Equals(expected))
            return Divergence(BgHoldReason.VerifyFrameMismatch + ":" + frame, history);
        return frame == BgFrameKind.H
            ? new BgHookDecision(BgHookAction.VerifyHeld, 1, expected, frame, null, false, false, history)
            : new BgHookDecision(BgHookAction.VerifyNeutral, 1, expected, frame,
                facts.StopLatched ? "STOP_LATCHED" : !facts.LeaseValid ? "LEASE_INVALID" : null, false, false, history);
    }

    /// <summary>True for the two VERIFY admissions (Parent sees the original refs).</summary>
    public static bool IsVerifyAdmission(BgHookAction action) => action is BgHookAction.VerifyHeld or BgHookAction.VerifyNeutral;

    /// <summary>
    /// Applies a decision: Parent is invoked exactly once (admitted / pass-through) or
    /// zero times (divergence). A substituted frame is recorded as consumed even if the
    /// consumer throws, so the allowed history set stays conservative.
    /// </summary>
    public static BgHookDecision Run(Func<BgHookFacts> sample, IBgFramePlan? plan, BgHistoryInvariant? invariant,
        Action<BgInputState?> invokeParent)
    {
        BgHookDecision decision = Decide(sample, plan, invariant);
        if (decision.ParentCalls == 0)
            return decision;
        if (decision.Substitute is null)
        {
            invokeParent(null);
            return decision;
        }
        try
        {
            // VERIFY: nothing is substituted; the consumer sees the original refs (null here).
            invokeParent(IsVerifyAdmission(decision.Action) ? null : decision.Substitute);
        }
        finally
        {
            if (decision.Frame is BgFrameKind frame)
            {
                if (frame != BgFrameKind.H && plan!.Next == BgFrameKind.H)
                    plan.RequestRelease(decision.Reason ?? "RELEASE");
                plan!.OnConsumed(frame);
            }
            invariant!.RecordConsumed(decision.Substitute, decision.History!.Value);
        }
        return decision;
    }

    private static BgHookDecision Divergence(string detail, BgHistoryCheck? history) =>
        new(BgHookAction.SkipDivergence, 0, null, null, BgReason.PreconsumerHistoryDivergence + ":" + detail, true, false, history);
}

/// <summary>Independent completion evidence for one background movement request.</summary>
public sealed record BgMoveCompletionFacts(
    int TargetHFrames,
    int ConsumedHFrames,
    bool RReceipt,
    bool NReceipt,
    double PixelDisplacement,
    int MovementDirectionsAfterN,
    int StableCapturesAfterN,
    bool LatchRestoredExact,
    bool Contained,
    bool ReleasedByGame,
    bool InterruptedFocusRegained,
    string? ReleaseReason);

public static class BgMoveCompletion
{
    public const string Completed = "COMPLETED";
    public const int RequiredStableCaptures = 3;

    /// <summary>No result is completed solely because a synthetic frame was sent.</summary>
    public static string Evaluate(BgMoveCompletionFacts f)
    {
        if (f.Contained) return "UNCERTAIN/CONTAINED";
        if (f.ReleasedByGame) return "UNCERTAIN/RELEASED_BY_GAME";
        if (f.InterruptedFocusRegained) return BgReason.InterruptedFocusRegained;
        bool releaseVerified = (f.ConsumedHFrames == 0 || f.RReceipt) && f.NReceipt
            && f.MovementDirectionsAfterN == 0 && f.StableCapturesAfterN >= RequiredStableCaptures && f.LatchRestoredExact;
        if (!releaseVerified) return "NOT_COMPLETED:RELEASE_UNVERIFIED";
        // fix89D / ADR 158: the segment guard's own stop settles the press like a consumed one (release
        // still verified above); readback stays the arrival oracle. Any other early release is unchanged.
        if (MoveSegmentGuard.SettlesPress(f.ReleaseReason))
            return f.PixelDisplacement > 0 ? Completed : "NOT_COMPLETED:NO_EFFECT";
        if (f.ReleaseReason is not null) return "NOT_COMPLETED:RELEASED_EARLY:" + f.ReleaseReason;
        if (f.ConsumedHFrames != f.TargetHFrames) return "NOT_COMPLETED:FRAMES_NOT_CONSUMED";
        if (!(f.PixelDisplacement > 0)) return "NOT_COMPLETED:NO_EFFECT";
        return Completed;
    }
}
