namespace ArtiStardewBridge.Protocol;

// The adapter supplies native facts; this policy never selects a response or an item.
public sealed record GenericUiSafetyFacts(bool Barrier = false, bool Choice = false,
    bool TextEntry = false, bool UnclaimedItems = false, bool CursorItem = false,
    bool AdvanceOnly = false, bool Ready = false, int AdvanceId = -1,
    bool CloseOffered = false, bool MenuOpen = true, bool NightProgression = false);

public static class GenericUiSafety
{
    // Vanilla countdowns may run below zero; equality with zero is not readiness.
    // This is structural policy, shared by every native menu, without calling its callbacks.
    public static bool TimersReady(IEnumerable<string> state) => !state.Any(s =>
        s.EndsWith(".outro=true", StringComparison.Ordinal)
        || (s.Contains("introTimer=", StringComparison.Ordinal) || s.Contains("outroTimer=", StringComparison.Ordinal))
            && (!double.TryParse(s.Split('=').Last(), System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out double time) || time > 0));

    public static bool CurrentTarget(UiPadFacts before, UiPadFacts current) =>
        before.ReceiverToken == current.ReceiverToken && UiPadToken.StateDigest(before) == UiPadToken.StateDigest(current);
    public static bool HasUnclaimedItems(bool foreignInventory, bool essential, bool menuItems,
        IEnumerable<(string Kind, bool HasItem)> controls) => foreignInventory || essential && menuItems
            || controls.Any(c => c.HasItem && c.Kind != "slot");
    public static string State(GenericUiSafetyFacts f) => f.Barrier ? "WAITING_NETWORK"
        : f.Choice || f.TextEntry || f.UnclaimedItems ? "CHOICE_PENDING"
        : f.CursorItem ? "CURSOR_PENDING" : f.NightProgression && f.AdvanceOnly && f.AdvanceId >= 0 ? "NIGHT_SUMMARY"
        : f.AdvanceOnly ? f.Ready ? "ADVANCE_ONLY" : "WAITING_DIALOGUE"
        : f.CloseOffered ? "ORDINARY_MENU" : "CHOICE_PENDING";

    // Native end-of-night state, never a menu-type allowlist. Extra summary navigation
    // (categories, pages) is not a profession/response choice. Only the observed OK is eligible.
    public static GenericUiSafetyFacts NightProgression(GenericUiSafetyFacts f, bool showingEndOfNight,
        IReadOnlyList<UiPadComponent> components)
    {
        var ok = components.Where(c => c.Visible && c.Kind == "button" && c.W > 0 && c.H > 0
            && c.FieldPath?.EndsWith(".okButton", StringComparison.Ordinal) == true).ToArray();
        bool advance = showingEndOfNight && f.MenuOpen && !f.Barrier && !f.Choice && !f.TextEntry
            && !f.UnclaimedItems && !f.CursorItem && ok.Length == 1;
        return f with { NightProgression = showingEndOfNight && f.MenuOpen,
            AdvanceOnly = f.AdvanceOnly || advance, AdvanceId = advance ? ok[0].Id : f.AdvanceId };
    }
    public static string? NightOperation(GenericUiSafetyFacts f) =>
        State(f) == "NIGHT_SUMMARY" && f.Ready && f.AdvanceId >= 0 ? "left" : null;

    public static string? InputGate(bool warp, bool locationRequest, bool fade, GenericUiSafetyFacts f) =>
        warp || locationRequest ? "warp_pending"
        : f.NightProgression && (f.Barrier || !f.Ready) ? "night_transition_pending"
        : fade && NightOperation(f) is null ? "fade_pending" : null;

    public static string? Operation(GenericUiSafetyFacts f, int step) => State(f) switch
    {
        "ADVANCE_ONLY" when step == 0 => f.AdvanceId >= 0 ? "left" : "key_confirm",
        _ => null,
    };

    public static bool Eligible(bool fade, bool warp, bool locationRequest, GenericUiSafetyFacts f) =>
        !warp && !locationRequest && (!fade || f.AdvanceOnly);
}

/// <summary>Learn numeric ambient fields only across distinct, input-free game frames.
/// Text, booleans, components, item ledgers and wallet are protected by the anchor.</summary>
public sealed class GenericUiIdentity
{
    public static string SemanticLedger(IReadOnlyDictionary<string, object> values) => ValuesDigest(values);

    // Readback telemetry is published in full, but cannot invalidate a menu target.
    // In particular native.draw and native.input_idle change without any receiver change.
    public static string ValuesDigest(IReadOnlyDictionary<string, object> values) =>
        StableJson(System.Text.Json.JsonSerializer.SerializeToElement(values
            .Where(p => p.Key is not ("captured_tick" or "rev" or "native" or "authority" or "receiver_epoch" or "stack" or "parked"))
            .ToDictionary(p => p.Key, p => p.Value)));

    static string StableJson(System.Text.Json.JsonElement value) => value.ValueKind switch {
        System.Text.Json.JsonValueKind.Object => "{" + string.Join(",", value.EnumerateObject()
            .OrderBy(p => p.Name, StringComparer.Ordinal).Select(p => System.Text.Json.JsonSerializer.Serialize(p.Name) + ":" + StableJson(p.Value))) + "}",
        System.Text.Json.JsonValueKind.Array => "[" + string.Join(",", value.EnumerateArray().Select(StableJson)) + "]",
        _ => value.GetRawText()
    };

    /// <summary>PM-1 a11 #2: an info-only night summary has one input, its observed OK, so no numeric menu field can
    /// carry a choice there. Every numeric field is ambient, learned or not. a11: weatherX / smokeTimer animate every
    /// frame, ambient_state stayed empty (learning never engaged), 25 inspects published 25 tokens. Booleans, text,
    /// components and the values ledger still fence the target.</summary>
    public static (string[] Semantic, string[] Ambient) InfoOnly((string[] Semantic, string[] Ambient) sample)
    {
        static bool Numeric(string field) => field.Split('=', 2) is { Length: 2 } p
            && double.TryParse(p[1], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out _);
        return (sample.Semantic.Where(f => !Numeric(f)).ToArray(),
            sample.Ambient.Concat(sample.Semantic.Where(Numeric)).ToArray());
    }

    string binding = "", anchor = "";
    long tick = -1;
    Dictionary<string, string> previous = new(StringComparer.Ordinal);
    readonly Dictionary<string, int> changes = new(StringComparer.Ordinal);
    readonly HashSet<string> volatileFields = new(StringComparer.Ordinal);

    public (string[] Semantic, string[] Ambient) Sample(string binding, string anchor,
        IReadOnlyList<string> fields, long tick, bool inputFree)
    {
        if (this.binding != binding)
        {
            this.binding = binding; this.volatileFields.Clear(); this.changes.Clear();
            this.previous.Clear(); this.tick = -1;
        }
        var current = fields.Select(s => s.Split('=', 2)).Where(p => p.Length == 2)
            .GroupBy(p => p[0], StringComparer.Ordinal).ToDictionary(g => g.Key, g => g.Last()[1], StringComparer.Ordinal);
        bool sameAnchor = this.anchor == anchor;
        if (!sameAnchor)
        {
            // A new anchor restarts learning. A field learned idle on this receiver stays ambient: the anchor's own
            // change already fences the target. PM-1 a9 night 11: re-fencing it per anchor flip churned every token.
            this.changes.Clear();
            this.anchor = anchor; this.previous = current; this.tick = tick;
        }
        else if (!inputFree) { this.changes.Clear(); this.previous.Clear(); this.tick = tick; }
        else if (tick != this.tick)
        {
            foreach (var (path, value) in current)
                if (this.previous.TryGetValue(path, out string? prior) && prior != value
                    && double.TryParse(value, System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out _)
                    && double.TryParse(prior, System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out _))
                {
                    this.changes[path] = this.changes.GetValueOrDefault(path) + 1;
                    if (this.changes[path] >= 2) this.volatileFields.Add(path);
                }
            this.previous = current; this.tick = tick;
        }
        return (fields.Where(s => !this.volatileFields.Contains(s.Split('=', 2)[0])).ToArray(),
            fields.Where(s => this.volatileFields.Contains(s.Split('=', 2)[0])).ToArray());
    }
}

/// <summary>PM-1 fix3: an uncertain verdict latches its receiver against re-press. When the SAME receiver
/// re-observes semantically unchanged for SettleMs with no input in flight, the press is known not to be
/// landing; the latch resolves so the page offers its advance/close again (recover, not stop).</summary>
public sealed class UncertainReceiverSettle
{
    public const long SettleMs = 1500, PollMs = 250;
    (string Receiver, string Digest, long Since)? seen;
    long? polled;

    /// <summary>Poll gate (the caller reads the native digest only when due).</summary>
    public bool Due(bool idle, long nowMs)
    {
        if (!idle) { this.seen = null; return false; }
        if (this.polled is { } last && nowMs - last < PollMs) return false;
        this.polled = nowMs;
        return true;
    }

    public bool Resolved(string receiver, string current, long nowMs)
    {
        if (this.seen is not { } s || s.Receiver != receiver || s.Digest != current)
        { this.seen = (receiver, current, nowMs); return false; }
        if (nowMs - s.Since < SettleMs) return false;
        this.seen = null;
        return true;
    }
}
