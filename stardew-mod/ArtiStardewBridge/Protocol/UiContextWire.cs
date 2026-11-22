using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Result of <see cref="UiContextWire.Fit"/>: wire bytes before / after and the names of what was shed.</summary>
public sealed record UiContextFit(int BytesBefore, int BytesAfter, IReadOnlyList<string> Omitted)
{
    public bool Trimmed => this.Omitted.Count > 0;
}

/// <summary>
/// ADR 44 (fix32): every ui.context fits its wire, and says what it shed.
///
/// The receiver drops any NDJSON line over <see cref="SceneObservationCaps.ReceiverMaxLineBytes"/> without a reply,
/// and the inspect_context result file has the same 64 KiB bound on the whole result. The old fit measured the facts
/// alone, and its result was ignored, so a full chest's ui.context (a28 D19 J1420, a27 D18 J2104) was never received:
/// the runtime kept the pre-open record until it aged out and read "ui_context_old" for 600 s. This fit measures the
/// facts against the line bound minus a fixed envelope reserve and always ends within it.
///
/// ADR 139: capture producers register action-input blocks with Require. Those
/// bounded blocks (including unknown future predicates) are never shed. Description
/// goes before the menu ladder. Unregistered legacy captures retain ADR 44's order.
/// ADR 143: call only after the complete capture has registered its action dependencies.
/// Order (never a new admission; each step only removes):
///   1. the nav grid (routing aid);
///   2. with a menu open, optional world-only aids, largest first;
///      on declared captures, remaining optional descriptions before the menu graph;
///   3. the ui_pad block's own ladder (<see cref="UiPadSnapshot.FitToBudget"/>; a generic menu loses its geometry, then
///      keeps only exits plus its native primitive entries, then becomes close-only; ADR 138);
///   4. anything else outside identity and action dependencies, largest first.
/// Shed top-level keys are named in <see cref="OmittedKey"/>.
/// </summary>
public static class UiContextWire
{
    /// <summary>Envelope / result wrapper headroom; the test pins the real wrappers well below it.</summary>
    public const int EnvelopeReserveBytes = 2048;

    public const int BudgetBytes = InspectContextQuery.MaxResultBytes - EnvelopeReserveBytes;

    public const string OmittedKey = "ui_context_omitted";

    // ADR 139: producers declare blocks used to offer/refuse an action. The fitter
    // knows their function, not the names of individual predicates inside them.
    public const string ActionFactsKey = "action_fact_blocks";

    public static void Require(Dictionary<string, object> snapshot, params string[] keys)
    {
        var required = snapshot.GetValueOrDefault(ActionFactsKey) as string[] ?? Array.Empty<string>();
        snapshot[ActionFactsKey] = required.Concat(keys.Where(snapshot.ContainsKey))
            .Distinct(StringComparer.Ordinal).OrderBy(k => k, StringComparer.Ordinal).ToArray();
    }

    /// <summary>Identity and admission core: never shed (InspectContextQuery.BuildSnapshot plus the input fence).</summary>
    static readonly HashSet<string> Core = new(StringComparer.Ordinal)
    {
        "runtime_instance_id", "context_generation", "session_id", "player_id", "screen_id", "ui_revision",
        "viewport_transform_revision", "location_id", "mode", "is_player_free", "can_player_move", "capabilities",
        "interactive", "input_fence", "ui_decision_lease", OmittedKey, ActionFactsKey,
    };

    /// <summary>Blocks that belong to the open menu; kept over world aids while one is open.</summary>
    static readonly HashSet<string> MenuOwned = new(StringComparer.Ordinal)
    {
        UiPadTable.CapabilityKey, "container", ShippingBinSnapshot.BlockKey, CharCustomTable.ProfileKey, "ui_motor",
        "ui_event", "ready_check", "generic_ui_state", "rendered_ui_text", "hud_messages", "money", "toolbar",
        "event_movement", "event_actions", "event_action_receipt", "native", "rev", "captured_tick", "pad",
    };

    public static int Bytes(Dictionary<string, object> snapshot) =>
        JsonSerializer.SerializeToUtf8Bytes(snapshot, ProtocolJson.Options).Length;

    public static bool Fits(Dictionary<string, object> snapshot) => Bytes(snapshot) <= BudgetBytes;

    public static UiContextFit Fit(Dictionary<string, object> snapshot)
    {
        var omitted = snapshot.GetValueOrDefault(OmittedKey) is string[] earlier ? earlier.ToList() : new List<string>();
        int before = Bytes(snapshot);
        if (before <= BudgetBytes)
            return new UiContextFit(before, before, omitted);
        if (snapshot.Remove(UiPadSnapshot.NavGridKey))
            Note(snapshot, omitted, UiPadSnapshot.NavGridKey);
        var required = (snapshot.GetValueOrDefault(ActionFactsKey) as string[] ?? Array.Empty<string>()).ToHashSet(StringComparer.Ordinal);
        bool Optional(string key) => !Core.Contains(key) && !required.Contains(key);
        if (snapshot.GetValueOrDefault("interactive") is not null)
            Shed(snapshot, omitted, key => Optional(key) && !MenuOwned.Contains(key));
        // Spend descriptive menu/world context before reducing native controls.
        // Legacy producers without declarations retain ADR 44's original ladder.
        if (required.Count > 0)
            Shed(snapshot, omitted, key => Optional(key) && key != UiPadTable.CapabilityKey);
        if (!Fits(snapshot) && snapshot.ContainsKey(UiPadTable.CapabilityKey))
        {
            UiPadSnapshot.FitToBudget(snapshot, Fits);
            string state = snapshot.GetValueOrDefault(UiPadTable.CapabilityKey) is Dictionary<string, object> block
                ? block.GetValueOrDefault(UiPadSnapshot.BudgetOmittedKey) is true
                    ? block.GetValueOrDefault(UiPadSnapshot.PrimitivesKeptKey) is true ? "ui_pad.primitives" : "ui_pad.close_only"
                    : block.GetValueOrDefault("components_truncated") is true ? "ui_pad.components"
                    : block.GetValueOrDefault(UiPadSnapshot.ComponentsCompactKey) is true ? "ui_pad.compact" : "ui_pad.trimmed"
                : UiPadTable.CapabilityKey;
            Note(snapshot, omitted, state);
            // ADR 143: the completed capture owns fitting (not EnrichUiPadContext).
            // Keep the capability's budget refusal aligned with the resulting view.
            if ((snapshot.GetValueOrDefault(UiPadTable.CapabilityKey) is not Dictionary<string, object> fitted
                    || fitted.GetValueOrDefault(UiPadSnapshot.BudgetOmittedKey) is true)
                && snapshot.GetValueOrDefault("capabilities") is Dictionary<string, object> caps)
                caps[UiPadTable.CapabilityKey] = UiPadSnapshot.Capability(false, "size_budget");
        }
        Shed(snapshot, omitted, Optional);
        return new UiContextFit(before, Bytes(snapshot), omitted);
    }

    /// <summary>Removes the largest matching top-level key until the snapshot fits (ties: key order).</summary>
    static void Shed(Dictionary<string, object> snapshot, List<string> omitted, Func<string, bool> sheddable)
    {
        if (Fits(snapshot))
            return;
        var sizes = snapshot.Where(p => sheddable(p.Key))
            .Select(p => (p.Key, Size: JsonSerializer.SerializeToUtf8Bytes(p.Value, ProtocolJson.Options).Length))
            .OrderByDescending(p => p.Size).ThenBy(p => p.Key, StringComparer.Ordinal).ToList();
        foreach (var (key, _) in sizes)
        {
            snapshot.Remove(key);
            Note(snapshot, omitted, key);
            if (Fits(snapshot))
                return;
        }
    }

    static void Note(Dictionary<string, object> snapshot, List<string> omitted, string key)
    {
        if (!omitted.Contains(key))
            omitted.Add(key);
        snapshot[OmittedKey] = omitted.ToArray();
    }
}
