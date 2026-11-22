namespace ArtiStardewBridge.Protocol;

using System.Text.Json;
using ArtiCore.Pure;

/// <summary>CSHARP_AUDIT_2026-10-26 §4, reconciled with the parser at 2420f782.
/// Classifies forms only; native arguments, dispatch and evidence stay with their adapters.</summary>
public static class LegacyFormMap
{
    public sealed record Mapping(string? Verb, string? Refusal = null);
    private static readonly Dictionary<(string Op, string Primitive), Mapping> Rows = new() {
        [("menu_step", "")] = new("ui"),
        [("water_once", "")] = new("use"),
        [("world_press", "")] = new("move"),
        [("inspect_tiles", "")] = new("observe"),
        [("inspect_context", "")] = new("observe"),
        [("input_once", "move_cardinal")] = new("move"),
        [("input_once", "use_tool")] = new("use"),
        [("input_once", "select_item")] = new("select"),
        [("input_once", "world_interact")] = new("interact"),
        [("input_once", "move_through")] = new("move"),
        [("input_once", "container_transfer")] = new("transfer"),
        [("input_once", "container_exit")] = new("ui"),
        [("input_once", "ui_key")] = new("ui"),
        [("input_once", "pad_button")] = new("ui"),
        [("input_once", "chat_send")] = new(null, "adapter_unsupported_capability"),
        [("input_once", "ui_nav")] = new("ui"),
        [("input_once", "ui_press")] = new("ui"),
        [("input_once", "reflex_fish")] = new("run_controller"),
        [("input_once", "reflex_combat")] = new("run_controller"),
        [("input_once", "reflex_charge")] = new("run_controller")
    };
    public static bool Enabled(MenuStepCommandRequest r, IReadOnlyDictionary<string, bool> flags)
    {
        string? verb = r.VerbEnvelope?.GetProperty("verb").GetString() ?? Resolve(r).Verb;
        string feature = r.Op is "menu_step" or "water_once" ? r.Op
            : r.Primitive is "container_exit" or "ui_key" or "pad_button" ? r.Primitive
            : verb == "run_controller" ? r.VerbEnvelope?.GetProperty("args").GetProperty("kind").GetString()
                ?? (r.ReflexFish is not null ? "fish" : r.ReflexCombat is not null ? "combat" : "charge")
            : verb ?? "";
        return verb == "observe" || flags.TryGetValue(feature, out bool enabled) && enabled;
    }

    public static JsonElement? Intent(MenuStepCommandRequest r, CommandInboxIdentity identity, VerbAdmissionFacts facts)
    {
        var mapping = Resolve(r);
        if (mapping.Verb is not { } verb || facts.Args is not { } args) return null;
        string? row = NativeVerbRules.Load().Resolve(verb, args, facts.Discriminators);
        if (row is null) return null;
        var pins = facts.Pins?.EnumerateObject().ToDictionary(p => p.Name, p => (object?)p.Value)
            ?? new Dictionary<string, object?>();
        pins["session_id"] = identity.SessionId; pins["player_id"] = identity.PlayerId;
        pins["location"] = identity.LocationId; pins["receiver_epoch"] = facts.ReceiverEpoch; pins["row_key"] = row;
        return JsonSerializer.SerializeToElement(new { verb, args, pins, attempt_id = r.RequestId, actor = "legacy" });
    }

    public static Mapping Resolve(MenuStepCommandRequest request) => Rows.TryGetValue(
        (request.Op ?? "", request.Op == "input_once" ? request.Primitive ?? "" : ""), out var row)
        ? row : new(null, "adapter_unmapped_form");
}
