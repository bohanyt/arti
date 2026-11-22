using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ArtiCore.Pure;

namespace ArtiStardewBridge.Protocol;

/// <summary>Game-thread writer. History consumers read this file directly, even under HALT or with the mod dead.</summary>
public sealed class OccurrenceRing
{
    public const int Capacity = 512, RecordBytes = 1024, FileBytes = 768 * 1024;
    readonly string save, runtime;
    readonly long player;
    readonly Queue<JsonElement> records = new();
    long seq;
    bool dirty;
    public string PathName { get; }
    public object Tail {
        get {
            int bytes = 2;
            var tail = records.Reverse().Take(16).TakeWhile(r => (bytes += Encoding.UTF8.GetByteCount(r.GetRawText()) + 1) <= 8192)
                .Reverse().ToArray();
            return new { save_id = save, player_id = player, runtime_id = runtime,
                first_seq = tail.Length == 0 ? 0 : tail[0].GetProperty("occ_seq").GetInt64(), last_seq = seq,
                tail_truncated = records.Count > tail.Length, tail };
        }
    }
    public OccurrenceRing(string directory, string saveId, long playerId, string runtimeId,
        long frame, ulong tick, long ms)
    {
        if (saveId.Length == 0 || saveId.Any(c => c is not (>= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9' or '.' or '_' or '-')))
            throw new InvalidDataException("Invalid occurrence file identity");
        save = saveId; player = playerId; runtime = runtimeId;
        PathName = Path.Combine(directory, $"occurrences.{save}.{player}.json");
        if (!File.Exists(PathName)) return;
        using var stream = AtomicFileWriter.OpenRead(PathName);
        if (stream.Length > FileBytes) throw new InvalidDataException("Occurrence file exceeds bound");
        using var doc = JsonDocument.Parse(stream);
        stream.Dispose(); // Finish the old snapshot before persisting a repair.
        var root = doc.RootElement;
        var history = JsonNode.Parse(root.GetRawText())!;
        bool repaired = false;
        foreach (var r in history["records"]!.AsArray()) {
            if (NativeSchemas.Valid("occurrence", r!)) continue;
            var facts = r!["facts"]!;
            if (r["kind"]!.GetValue<string>() is "ko_items_lost" or "barrier") {
                foreach (var p in facts.AsObject().ToArray())
                    if (p.Value is JsonObject || p.Value is JsonArray a && a.Any(x => x is JsonObject)) {
                        facts[p.Key + "_count"] = p.Value is JsonArray items ? items.Count : p.Value["levelups"]?.AsArray().Count;
                        facts[p.Key] = "legacy_unknown";
                    }
                r["facts_truncated"] = true; repaired = true; continue;
            }
            string? entry = facts["entry"]?.GetValue<string>();
            if (r["kind"]!.GetValue<string>() is not ("progression_admitted" or "progression_settled")
                || entry is not ("a" or "b" or "c" or "d" or "e")) continue;
            r["facts"] = JsonSerializer.SerializeToNode(ProgressionFacts(entry,
                facts["receiver_epoch"]?.GetValue<long>(), false, "legacy_unknown"));
            r["facts_truncated"] = true; repaired = true;
        }
        if (!NativeSchemas.Valid("occurrence_ring", history)
            || root.GetProperty("save_id").GetString() != save || root.GetProperty("player_id").GetInt64() != player)
            throw new InvalidDataException("Invalid occurrence file");
        long next = root.GetProperty("first_seq").GetInt64();
        foreach (var node in history["records"]!.AsArray()) {
            var record = JsonSerializer.SerializeToElement(node);
            if (record.GetProperty("occ_seq").GetInt64() != next++ || Encoding.UTF8.GetByteCount(record.GetRawText()) > RecordBytes)
                throw new InvalidDataException("Invalid occurrence sequence/size");
            records.Enqueue(record);
        }
        seq = root.GetProperty("last_seq").GetInt64();
        if (records.Count == 0 ? next != 0 || seq != 0 : next != seq + 1)
            throw new InvalidDataException("Invalid occurrence bounds");
        if (repaired) {
            string backup = PathName + ".pre-bfix." + seq;
            if (!File.Exists(backup)) File.Copy(PathName, backup);
            if (!AtomicFileWriter.TryWrite(PathName, history.ToJsonString()).Ok)
                throw new IOException("Occurrence repair failed; original preserved");
        }
        string previous = root.GetProperty("runtime_id").GetString()!;
        if (previous == runtime) return; // returning to the same identity in this process is not a restart
        string exitPath = Path.Combine(directory, "mod_exit.json");
        bool orderly = false;
        if (File.Exists(exitPath)) {
            try { using var exit = JsonDocument.Parse(AtomicFileWriter.ReadAllText(exitPath));
                orderly = exit.RootElement.GetProperty("runtime_id").GetString() == previous; }
            catch (Exception e) when (e is IOException or JsonException or KeyNotFoundException) { }
        }
        Append("native_restart", previous, new { prev_runtime_id = previous, last_persisted_seq = seq,
            last_persisted_wall_ms = records.LastOrDefault().ValueKind == JsonValueKind.Object
                ? records.Last().GetProperty("wall_ms").GetInt64() : 0, prev_exit = orderly ? "orderly" : "unclean" }, frame, tick, ms);
    }
    public static string MenuType(string entry) => entry switch {
        "a" => "DialogueBox", "b" => "LetterViewerMenu", "c" => "ShippingMenu", "d" => "LevelUpMenu", _ => "ItemListMenu" };
    static object ProgressionFacts(string entry, long? epoch, bool accepted, string? fault) => new {
        receiver_epoch = epoch, menu_type = MenuType(entry), op = entry + "_" + (entry switch {
            "a" => "prose_advance", "b" => "info_ok", "c" => "shipping_ok", "d" => "levelup_ok", _ => "item_list_ok" }), accepted, fault };
    public void Progression(ActiveAttempt a, bool terminal, long frame, ulong tick, long ms) =>
        Append(terminal ? "progression_settled" : "progression_admitted", a.AttemptId,
            ProgressionFacts(a.ProgressionEntry!, a.Validity!.ReceiverEpoch,
                !terminal || a.ProgressionAccepted, a.ProgressionFault ?? a.CancelReason), frame, tick, ms, a.AttemptId);
    public void KoLosses(string subject, (string Id, int Qty)[] items, long frame, ulong tick, long ms) =>
        Append("ko_items_lost", subject, new { item_count = items.Length,
            items = items.Take(8).Select(i => i.Id + ":" + i.Qty).ToArray() }, frame, tick, ms, truncated: items.Length > 8);
    public void ChestEffect((string? Attempt, long Frame) effect, ulong tick, long ms) =>
        Append("effect", "chest:" + effect.Attempt, new { effect_kind = "receiver_opened", subject = "chest:" + effect.Attempt,
            effect_frame = effect.Frame }, effect.Frame, tick, ms, effect.Attempt);
    public void Append(string kind, string subject, object facts, long frame, ulong tick, long ms, string? attemptId = null,
        bool truncated = false)
    {
        var fields = new Dictionary<string, object?> { ["occ_seq"] = seq + 1, ["kind"] = kind,
            ["subject"] = subject, ["frame"] = frame, ["tick"] = tick, ["wall_ms"] = ms,
            ["runtime_id"] = runtime, ["facts"] = facts };
        if (attemptId is not null) fields["attempt_id"] = attemptId;
        var node = JsonSerializer.SerializeToNode(fields)!;
        foreach (var p in node["facts"]!.AsObject().ToArray()) {
            if (p.Value is JsonValue v && v.TryGetValue<string>(out var s) && s.Length > 240) {
                node["facts"]![p.Key] = s[..240]; truncated = true;
            }
        }
        if (truncated) node["facts_truncated"] = true;
        while (Encoding.UTF8.GetByteCount(node.ToJsonString()) > RecordBytes) {
            var p = node["facts"]!.AsObject().Where(p => p.Key is "text" or "detail" or "items" or "levelups")
                .OrderByDescending(p => p.Value?.ToJsonString().Length).FirstOrDefault();
            if (p.Value is null) throw new InvalidDataException("Required facts exceed bound");
            if (p.Value is JsonArray a && a.Count > 0) a.RemoveAt(a.Count - 1);
            else node["facts"]!.AsObject().Remove(p.Key);
            node["facts_truncated"] = true;
        }
        if (!NativeSchemas.Valid("occurrence", node)) throw new InvalidDataException("Invalid occurrence");
        var record = JsonSerializer.SerializeToElement(node);
        records.Enqueue(record); ++seq;
        if (records.Count > Capacity) records.Dequeue();
        dirty = true;
    }
    public void Append(JsonElement record) => Append(record.GetProperty("kind").GetString()!,
        record.GetProperty("subject").GetString()!, record.GetProperty("facts"), record.GetProperty("frame").GetInt64(),
        record.GetProperty("tick").GetUInt64(), record.GetProperty("wall_ms").GetInt64(),
        record.TryGetProperty("attempt_id", out var id) ? id.GetString() : null);
    public bool Flush()
    {
        if (!dirty) return true;
        string json = JsonSerializer.Serialize(new { v = "arti.occurrence_ring/1", save_id = save, player_id = player,
            runtime_id = runtime, capacity = Capacity, first_seq = records.Peek().GetProperty("occ_seq").GetInt64(),
            last_seq = seq, records = records.ToArray() });
        if (!AtomicFileWriter.TryWrite(PathName, json).Ok) return false;
        dirty = false; return true;
    }
}
