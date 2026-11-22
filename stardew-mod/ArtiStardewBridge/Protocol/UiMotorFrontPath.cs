namespace ArtiStardewBridge.Protocol;

/// <summary>Read-only view of the UI motor's input seat (is her staged frame in the game's input slot now?).</summary>
public interface IUiMotorSeatState
{
    bool Seated { get; }
}

/// <summary>
/// ADR 250 (fix145A): one input path for her menu presses whatever the window focus. The UI motor seats her staged
/// frame in the game's own update (UiMotorInputSeatPatch). Vanilla <c>updateActiveMenu</c> takes its inactive branch
/// while the window is not the active one: the deepest child menu's own <c>update(time)</c>, no hover from the cursor,
/// no input callbacks (G1:4842-4849, PROBE_GAMEPAD_2026-10-08.md item 6). The motor was built on that branch: her one
/// edge dispatch is the only delivery. With her window active the game ran its active branch on her seated frame. It
/// hovered at her staged point, so the menu changed under the motor's stale check (stale_component, no call), and it
/// would have delivered her seated edge a second time. While her frame is seated, the menu update now takes the
/// inactive branch in both states.
/// </summary>
public static class UiMotorFrontPath
{
    public enum MenuUpdate { Vanilla, InactiveBranch }

    /// <summary>Named refusal: a generic motor press in front while the menu-update patch is not installed.</summary>
    public const string Unavailable = "UiPadWindowActiveUnsupported";

    /// <summary>Review B4: a motor press whose frame no game update can take, because the input-seat hook is missing.</summary>
    public const string SeatUnavailable = "UiPadSeatUnavailable";

    /// <summary>Review S1: her press is refused before anything is staged while the owner holds input in her window.</summary>
    public const string OwnerHeldPrefix = "owner_input_held:";

    /// <summary>The game's menu update for this frame: the inactive branch whenever her motor frame is seated.</summary>
    public static MenuUpdate For(bool motorFrameSeated) => motorFrameSeated ? MenuUpdate.InactiveBranch : MenuUpdate.Vanilla;

    /// <summary>
    /// Review B4: whether a motor frame may be staged or seated now. It is checked at admission, at every frame where the
    /// frame is staged, and again where it is seated. A missing menu hook with her window active, or a missing seat hook
    /// in front or for a polled-only edge, fails closed by name. In the background, a direct-call press without the seat
    /// hook still delivers its one edge (the motor's own seat). null = stage it.
    /// </summary>
    public static string? FrameRefusal(bool seatInstalled, bool menuInstalled, bool windowActive, string? operation) =>
        !menuInstalled && windowActive ? Unavailable
        : !seatInstalled && (windowActive || PolledOnly(operation)) ? SeatUnavailable
        : null;

    /// <summary>The admission form of <see cref="FrameRefusal"/> for a parsed plan (non-motor plans are not this path's).</summary>
    public static string? Refusal(bool seatInstalled, bool menuInstalled, bool windowActive, UiPadPlan? plan) =>
        GenericMotorPress(plan) ? FrameRefusal(seatInstalled, menuInstalled, windowActive, MotorOperation(plan)) : null;

    /// <summary>
    /// Review B3: whether the frame's history (old) state carries her key. Normally only after the pressed frame (a true
    /// edge). For a press aimed at a running minigame the key reads as already held on the pressed frame too, so vanilla's
    /// minigame loop, which hands NEW key edges to the minigame, has no edge to deliver; her direct call is the one Escape.
    /// </summary>
    public static bool HistoryHoldsKey(bool prior, bool pressed, bool toMinigame) => prior || pressed && toMinigame;

    /// <summary>Edges only the game's own update consumes (no direct call): Start, LB and RB with no menu up.</summary>
    public static bool PolledOnly(string? operation) => operation is "key_menu" or "key_LB" or "key_RB";

    /// <summary>A press whose plan runs through the generic UI motor (seated frames), the path this ADR covers.</summary>
    public static bool GenericMotorPress(UiPadPlan? plan) => plan is { Allowed: true } p && p.Steps.Any(IsGenericInput);

    static string? MotorOperation(UiPadPlan? plan)
    {
        foreach (UiPadStep step in plan?.Steps ?? Array.Empty<UiPadStep>())
            if (IsGenericInput(step))
                return step.G3Action;
        return null;
    }

    static bool IsGenericInput(UiPadStep step) => step.Kind == UiPadStepKind.GenericInput;

    /// <summary>
    /// Review S1: the owner's own input in her window, named as ADR 244 names it in the world ("keyboard:A", "mouse:Left",
    /// "pad:B"). Any key, mouse button or pad button down counts. A move alone is not input. null = none.
    /// </summary>
    public static string? OwnerInput(BgInputState real)
    {
        IReadOnlyList<BgButton> held = BgForeignInput.ForeignButtons(real, null);
        return held.Count == 0 ? null : string.Join("+", held.Select(BgForeignInput.Name));
    }

    /// <summary>W1 S2: wheel-only owner input in her window (the wheel moved since the last sample) is named too.</summary>
    public static string? OwnerInput(BgInputState real, int? lastWheel)
    {
        string? buttons = OwnerInput(real);
        bool wheel = lastWheel is int before && before != real.ScrollWheel;
        return !wheel ? buttons : buttons is null ? "mouse:wheel" : buttons + "+mouse:wheel";
    }

    /// <summary>session.json <c>focus_independent_paths</c>: each hand path on its own.</summary>
    public static FocusPaths Paths(bool world, bool menu) => new(world, menu);

    /// <summary>session.json <c>focus_independent</c>: the weaker value, true only when every path is independent.</summary>
    public static bool Weaker(FocusPaths paths) => paths.World && paths.Menu;
}

/// <summary>
/// ADR 250: per-path focus independence. A value record, so the per-tick session comparison
/// (<c>PublishCommandSession</c>: unchanged record = no write) stays cheap.
/// </summary>
public sealed record FocusPaths(
    [property: System.Text.Json.Serialization.JsonPropertyName("world")] bool World,
    [property: System.Text.Json.Serialization.JsonPropertyName("menu")] bool Menu);
