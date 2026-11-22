namespace ArtiHostHelper;

/// <summary>
/// ADR 255 (rewritten, review M4b, 2026-11-20): there is NO input fence. The owner's keys and mouse always reach his
/// HOST. Instead: while the owner is using the HOST the program does nothing in it; when he has been idle for
/// <see cref="IdleSeconds"/> it takes the HOST back. Pure (no SMAPI types): the mod passes each real input of his and a
/// monotonic clock (Environment.TickCount64), the tests pass their own.
/// </summary>
public sealed class HostOwnerPresence
{
    /// <summary>Three minutes without any input of his: the HOST is the harness's again (the owner's rule).</summary>
    public const double IdleSeconds = 180;

    /// <summary>A pause longer than this between his inputs starts a new burst (named again).</summary>
    public const long BurstGapMs = 2000;

    long? lastMs;
    long burstMs;
    bool burst, wasActive;

    /// <summary>One real input of his (what it was). Returns the line to name now (the first input of a burst), else null.</summary>
    public string? Input(string what, long nowMs)
    {
        bool fresh = !this.burst || nowMs - this.burstMs > BurstGapMs || nowMs < this.burstMs;
        this.burst = true;
        this.burstMs = nowMs;
        this.lastMs = nowMs;
        this.wasActive = true;
        return fresh ? "owner_input:" + what : null;
    }

    /// <summary>Review M4c rule 1: the raw input state sampled once per tick. Keys, mouse buttons, pad buttons and sticks or
    /// triggers count only while his HOST window is active (the same devices drive his other programs); a mouse move counts
    /// whenever the cursor is inside the HOST window.</summary>
    public readonly record struct RawInput(bool WindowActive, bool AnyKeyHeld, bool AnyMouseButtonHeld, bool AnyPadButtonHeld,
        bool StickOrTriggerOffRest, int MouseX, int MouseY, bool MouseInWindow);

    /// <summary>A stick or trigger further than this from its rest counts as held (pads rest a little off zero).</summary>
    public const float PadRest = 0.15f;

    (int X, int Y)? lastMouse;

    /// <summary>Review M4c rule 1: one tick's raw state. A held key or button, a stick or trigger off its rest, or the mouse
    /// moving inside the HOST window is his input (stamped like an event). Returns the line to name now, else null.</summary>
    public string? Sample(RawInput r, long nowMs)
    {
        bool moved = r.MouseInWindow && this.lastMouse is { } last && (last.X != r.MouseX || last.Y != r.MouseY);
        this.lastMouse = r.MouseInWindow ? (r.MouseX, r.MouseY) : null;
        string? what = r.WindowActive && r.AnyKeyHeld ? "key_held" : r.WindowActive && r.AnyMouseButtonHeld ? "mouse_button_held"
            : r.WindowActive && r.AnyPadButtonHeld ? "pad_button_held" : r.WindowActive && r.StickOrTriggerOffRest ? "stick_or_trigger"
            : moved ? "mouse_move" : null;
        return what is null ? null : this.Input(what, nowMs);
    }

    /// <summary>He gave input less than <see cref="IdleSeconds"/> ago. A clock that went back counts as active (doubt = his).</summary>
    public bool Active(long nowMs) => this.lastMs is long last && (nowMs < last || nowMs - last < IdleSeconds * 1000);

    /// <summary>"idle" exactly once, on the first sample after he stopped being active; else null.</summary>
    public string? Ended(long nowMs)
    {
        if (!this.wasActive || this.Active(nowMs)) return null;
        this.wasActive = false;
        return "idle";
    }
}
