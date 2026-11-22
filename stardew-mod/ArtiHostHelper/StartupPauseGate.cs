namespace ArtiHostHelper;

// ADR 65: one isolated pre-game native pause, never a gameplay clock override.
public sealed class StartupPauseGate
{
    readonly int seconds;
    double since;
    public string State { get; private set; }
    public string? Reason { get; private set; }
    public StartupPauseGate(int seconds) { this.seconds = seconds; State = seconds == 0 ? "disabled" : "pending"; }
    string Fail(string reason) { State = "failed"; Reason = reason; return "fail"; }

    public string Advance(double now, bool paused, bool release)
    {
        if (State is "disabled" or "released") return "none";
        if (State == "failed") return "fail";
        if (State == "pending")
        {
            if (paused) return Fail("startup_external_pause");
            since = now; State = "held"; PausedByHelper = true; return "pause";
        }
        if (!paused) { PausedByHelper = false; return Fail("startup_pause_lost"); }
        if (now - since >= seconds) return Fail("startup_pause_timeout"); // still paused by the helper: ReleaseHold lifts it
        if (!release) return "none";
        State = "released"; PausedByHelper = false; return "resume";
    }

    /// <summary>Review M4d item 1: the native pause this gate imposed and has not lifted yet (held, or failed after it).</summary>
    public bool PausedByHelper { get; private set; }

    /// <summary>
    /// Review M4d item 1: the helper gives up its own pause, with or without a release token: on the owner's input, any
    /// stop, the helper becoming inert, or a helper fault. True = the caller must lift the native pause now (once). A held
    /// gate ends as released, so it never pauses again in this launch; a pending gate imposed nothing and stays pending.
    /// </summary>
    public bool ReleaseHold()
    {
        if (State == "held") State = "released";
        if (!PausedByHelper) return false;
        PausedByHelper = false;
        return true;
    }
}
