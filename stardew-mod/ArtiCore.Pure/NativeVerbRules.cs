using System.Text.Json;

namespace ArtiCore.Pure;

// Values are native Observer facts, never copied from pins.row_key.
public sealed record VerbDiscriminators(string TargetKind, bool HeldTool = false, bool HeldPlaceable = false,
    bool HeldGiftable = false, string? ControlRole = null, string? CallbackClass = null);
public sealed record AnswerRule(string AnswerClass, string? PostAnswer, string? Defect, JsonElement? Row);

public sealed class NativeVerbRules
{
    readonly JsonElement verbs, successors;
    readonly System.Collections.Concurrent.ConcurrentDictionary<(string, string, string), byte> unmapped = new();
    NativeVerbRules(JsonElement verbs, JsonElement successors) { this.verbs = verbs; this.successors = successors; }
    static JsonElement Read(string name)
    {
        using var stream = typeof(NativeVerbRules).Assembly.GetManifestResourceStream(name)!;
        using var document = JsonDocument.Parse(stream); return document.RootElement.Clone();
    }
    static readonly System.Text.RegularExpressions.Regex rowKeyPattern = new(Read("verb_phases.schema.json")
        .GetProperty("$defs").GetProperty("RowKey").GetProperty("pattern").GetString()!);
    public static bool ValidRowKey(string key) => rowKeyPattern.IsMatch(key);
    public static NativeVerbRules Load() => new(Read("verb_phases.json").GetProperty("verbs"), Read("successors.json").GetProperty("rows"));
    // ADR-23 legacy_compat: R4 retires these internal rows.
    public JsonElement Row(string verb, string key)
    {
        if (verb == "ui" && key.StartsWith("receiver.legacy_compat:nav_dir:")) key = "receiver.role:page_tab";
        bool route = verb == "interact" && key == "other_world_object.legacy_compat:route";
        var row = verbs.GetProperty(verb).GetProperty("targets").GetProperty(route ? "other_world_object" : key);
        if (!route) return row;
        var copy = System.Text.Json.Nodes.JsonNode.Parse(row.GetRawText())!;
        copy["phases"] = JsonSerializer.SerializeToNode(new[] { "walking", "pre_edge", "post_edge" }
            .Select(p => new { name = p, validity = p }));
        return JsonSerializer.SerializeToElement(copy);
    }
    public string[] Phases(string verb, string key) => Row(verb, key).GetProperty("phases").EnumerateArray()
        .Select(p => p.GetProperty("validity").GetString()!).ToArray();
    public string? Resolve(string verb, JsonElement args, VerbDiscriminators facts)
    {
        if (!verbs.TryGetProperty(verb, out var definition)) return null;
        var rows = definition.GetProperty("targets");
        string? key = verb switch {
            "observe" => "any", "select" => "slot",
            "move" => facts.TargetKind == "bed" ? "bed.arg:bed" : args.TryGetProperty("exit_id", out _) ? "door_warp.arg:exit_id"
                : args.TryGetProperty("target_id", out _) ? "self.arg:target_id" : "self.arg:heading",
            "run_controller" => "self.controller:" + args.GetProperty("kind").GetString(),
            "ui" => facts.CallbackClass is { } callback ? "receiver.callback:" + callback : "receiver.role:" + facts.ControlRole,
            "transfer" => "receiver.role:" + facts.ControlRole,
            "interact" => facts.TargetKind == "npc" ? "npc." + (facts.HeldGiftable ? "held_giftable" : "hand_empty") : facts.TargetKind,
            "use" => facts.TargetKind + (facts.HeldTool ? ".held_tool" : facts.HeldPlaceable ? ".held_placeable" : ""), _ => null };
        if (verb == "ui" && facts.ControlRole?.StartsWith("legacy_compat:nav_dir:") == true)
            return "receiver." + facts.ControlRole;
        if (verb == "interact" && facts.TargetKind == "legacy_compat:route") return "other_world_object.legacy_compat:route";
        if (key is not null && rows.TryGetProperty(key, out _)) return key;
        // Only world target kinds fall back; missing control/callback rows must stay unsupported.
        if (verb == "interact" || verb == "use" && facts.HeldTool)
        {
            key = "other_world_object" + (verb == "use" && facts.HeldTool ? ".held_tool" : "");
            if (rows.TryGetProperty(key, out _)) return key;
        }
        return null;
    }
    public string? CheckRow(string verb, JsonElement args, JsonElement pins, VerbDiscriminators facts)
    {
        if (!pins.TryGetProperty("row_key", out var key) || key.ValueKind != JsonValueKind.String) return "malformed_json";
        string? resolved;
        try { resolved = Resolve(verb, args, facts); }
        catch (Exception e) when (e is KeyNotFoundException or InvalidOperationException) { return "malformed_json"; }
        if (resolved is null || key.GetString() != resolved) return "precondition_stale";
        return Row(verb, resolved).GetProperty("required_pins").EnumerateArray()
            .Any(p => !pins.TryGetProperty(p.GetString()!, out _)) ? "malformed_json" : null;
    }
    public JsonElement ControllerPhases(string kind) => verbs.GetProperty("run_controller").GetProperty("controller_phases").GetProperty(kind);
    public AnswerRule AnswerRow(string receiver, string role, string gesture)
    {
        foreach (var row in successors.EnumerateArray())
        {
            var key = row.GetProperty("key");
            if (key.GetProperty("receiver_class").GetString() == receiver && key.GetProperty("control_role").GetString() == role
                && key.GetProperty("gesture").GetString() == gesture)
                return new(row.GetProperty("answer_class").GetString()!, row.TryGetProperty("post_answer", out var post) ? post.GetString() : null, null, row);
        }
        return new("terminal", null, unmapped.TryAdd((receiver, role, gesture), 0) ? "answer_class_unmapped" : null, null);
    }
}
