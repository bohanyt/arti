namespace ArtiStardewBridge.Protocol;

/// <summary>ADR 241 C4 (bridge lead 2026-11-19): a held native progression page names its own cause. Read-only and
/// SMAPI-free: the reporter decides WHEN to say it, the two orderings decide WHICH gate to name. Nothing here changes
/// what the progression watchdog does. 11-18 19:35: a rejoin waited 126 s on a ShippingMenu that the native client
/// never confirmed, and the log could not say why (the watchdog returned silently at a closed gate).</summary>
public sealed class ProgressionHeld
{
    /// <summary>Said only after the page's own dwell plus this grace: a normal advance never produces a report.</summary>
    public const long GraceMs = 5000;
    string occurrence = "", said = "";
    long since;

    /// <summary>The gate to report now, or null. Once per menu occurrence, again only when the closed gate changes,
    /// and never before the page has been up for <paramref name="dwellMs"/> plus <see cref="GraceMs"/>. A null
    /// <paramref name="menuOccurrence"/> (no progression page up) forgets the last one.</summary>
    public string? Note(string? menuOccurrence, long dwellMs, string? closedGate, long now)
    {
        if (menuOccurrence is null) { this.occurrence = ""; return null; }
        if (menuOccurrence != this.occurrence)
        {
            this.occurrence = menuOccurrence; this.since = now; this.said = "";
            return null;
        }
        if (closedGate is null || closedGate == this.said || now - this.since < dwellMs + GraceMs) return null;
        this.said = closedGate;
        return closedGate;
    }

    public long HeldMs(long now) => now - this.since;

    /// <summary>The first closed gate of the watchdog's early return, in its own order (ModEntry.UiMotor
    /// TickGenericUiWatchdog). Null for the watchdog's own advance frame: its edge is in flight, nothing holds it.</summary>
    public static string? EarlyGate(bool padMenusEnabled, bool padReady, bool motorUnsafe, bool progressionAllowed,
        bool recordFaulted, string? haltScope, bool ownAdvanceInFlight) =>
        !padMenusEnabled ? "pad_menus_disabled"
        : !padReady ? "pad_not_ready"
        : motorUnsafe ? "motor_unsafe"
        : !progressionAllowed ? (recordFaulted ? "progression_not_allowed:record_faulted"
            : "progression_not_allowed:halt_latched:" + (haltScope ?? "unknown"))
        : ownAdvanceInFlight ? null
        : "input_in_flight";

    /// <summary>Why ProgressionClient.AdvanceDue did not fire on a page the watchdog reached, in its own order, read
    /// without calling it again (it keeps state). Null while nothing holds it: the dwell is running, or its one edge was
    /// already sent.</summary>
    public static string? NotDueGate(string pageState, string? ineligible, bool attempted, bool leased, bool night,
        bool scripted, bool deciding, bool hasOperation, string? undeliveredGate = null) =>
        pageState is not ("ADVANCE_ONLY" or "NIGHT_SUMMARY") ? "page_state:" + pageState
        : ineligible is not null ? ineligible
        // Review R5 S4: "attempted" with every re-arm spent and no edge delivered is a held page, named by its stage.
        : attempted ? undeliveredGate
        : leased && !night && !scripted ? "arm_lease"
        : deciding && !scripted ? "decision_lease"
        : !hasOperation ? "no_operation"
        : null;
}
