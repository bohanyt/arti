using ArtiCore.Pure;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ArtiStardewBridge.Protocol;

public sealed class NightLedger
{
    public sealed record SleepIntent(long frame, string? attempt_id);
    public sealed record DayEvent(string kind, long frame, bool recovered = true);
    public sealed record Night(string night_id, long seq, JsonNode date, string state, string opened_by,
        SleepIntent? intent, string? intent_cleared_by, string? collapse, DayEvent[] day_events,
        JsonNode? plan, string save = "unknown", long? day_started_frame = null, string? cause_hint = null);
    readonly string save;
    readonly long player;
    readonly List<Night> nights;
    string durable = "";
    bool planRecorded;
    public string PathName { get; }
    public Night Pending => nights[^1];
    public IReadOnlyList<Night> Nights => nights;
    public bool HasIntent => Pending.intent is not null && Pending.intent_cleared_by is null;
    static JsonNode Copy(object value) => JsonSerializer.SerializeToNode(value)!;
    public NightLedger(string directory, string saveId, long playerId, object date, bool midNight = false)
    {
        save = saveId; player = playerId;
        PathName = Path.Combine(directory, $"nights.{save}.{player}.json");
        var raw = File.Exists(PathName) ? JsonNode.Parse(AtomicFileWriter.ReadAllText(PathName))!.AsArray() : new();
        if (raw.Count > 17 || raw.Any(n => n is null || !NativeSchemas.Valid("night", n)
            || !n["night_id"]!.GetValue<string>().StartsWith(save + "|" + player + "|", StringComparison.Ordinal)))
            throw new InvalidDataException("Invalid night history; preserved");
        nights = JsonSerializer.Deserialize<List<Night>>(raw.ToJsonString())!;
        if (nights.Where((n, i) => n.state != (i == nights.Count - 1 ? "pending" : "committed")
            || i > 0 && n.seq <= nights[i - 1].seq).Any()) throw new InvalidDataException("Invalid night sequence; preserved");
        if (nights.Count == 0) Open(date, "restart");
        else if (Pending.date.ToJsonString() != Copy(date).ToJsonString()) {
            Set(Pending with { intent = null, collapse = null, cause_hint = "restart_across_date" });
            Commit(date, null);
        }
        if (midNight && Pending.plan is null) Set(Pending with { plan = JsonValue.Create("unknown"),
            cause_hint = raw.Count == 0 ? "lost_pending" : Pending.cause_hint });
        Flush();
    }
    void Set(Night n) { nights[^1] = n; Flush(); }
    void Open(object date, string opened)
    {
        long seq = nights.Count == 0 ? 0 : Pending.seq + 1;
        var d = Copy(date);
        nights.Add(new($"{save}|{player}|{d["year"]}-{d["season"]}-{d["day"]}", seq, d, "pending", opened,
            null, null, null, Array.Empty<DayEvent>(), null));
        while (nights.Count > 17) nights.RemoveAt(0);
    }
    public void Intent(long frame, string? attempt)
        => Set(Pending with { intent = new(frame, attempt), intent_cleared_by = null });
    public void Clear(string cause) => Set(Pending with { intent_cleared_by = cause,
        cause_hint = cause is "reconnect" or "save_failed" ? cause : Pending.cause_hint });
    public void Collapse(string kind)
        => Set(Pending with { intent_cleared_by = "collapse", collapse = kind });
    public void Recovered(long frame)
    {
        if (Pending.collapse is not ("ko" or "exhaustion")) return;
        Set(Pending with { day_events = Pending.day_events.Append(new(Pending.collapse, frame)).Take(8).ToArray(), collapse = null });
    }
    public void Plan(object[] levelups, bool shipping, string path, OccurrenceRing? history = null,
        long frame = 0, ulong tick = 0, long ms = 0) {
        if (Pending.plan is not null) return;
        var next = Pending with { plan = Copy(new { night_id = Pending.night_id, levelups, shipping, save = path }) };
        bool overflow = !NativeSchemas.Valid("night", Copy(next));
        if (overflow) next = Pending with { plan = JsonValue.Create("unknown"), cause_hint = "other" };
        if (history is not null) {
            if (!planRecorded) {
                history.Append("barrier", "night_plan", new { edge = "stage", barrier_kind = "night",
                    instance_id = Pending.night_id, stage = "plan", levelup_count = levelups.Length, shipping, save = path,
                    overflow, levelups = levelups.Take(12).Select(l => JsonSerializer.Serialize(l)).ToArray() }, frame, tick, ms,
                    truncated: overflow);
                planRecorded = true;
            }
            if (!history.Flush()) return;
        }
        Set(next);
    }
    public void Commit(object date, long? frame, bool dayStarted = false)
    {
        if (dayStarted) {
            string[] seasons = { "spring", "summer", "fall", "winter" };
            var d = Copy(date);
            int days = (d["year"]!.GetValue<int>() - 1) * 112
                + Array.IndexOf(seasons, d["season"]!.GetValue<string>()) * 28 + d["day"]!.GetValue<int>() - 2;
            int day = days % 28 + 1;
            var prev = Copy(new { year = days / 112 + 1, season = seasons[days / 28 % 4], day });
            if (days >= 0 && !nights.Any(n => n.date.ToJsonString() == prev.ToJsonString())) {
                if (Pending.date.ToJsonString() != Copy(date).ToJsonString()) Commit(date, frame);
                var current = Pending;
                nights.Insert(nights.Count - 1, current with { night_id = $"{save}|{player}|{prev["year"]}-{prev["season"]}-{day}",
                    date = prev, state = "committed", plan = JsonValue.Create("unknown"), intent = null, collapse = null,
                    seq = current.seq, cause_hint = "lost_pending", day_started_frame = frame });
                nights[^1] = current with { seq = current.seq + 1 };
                while (nights.Count > 17) nights.RemoveAt(0);
                Flush();
            }
        }
        if (Pending.date.ToJsonString() == Copy(date).ToJsonString()) return;
        nights[^1] = Pending with { state = "committed", day_started_frame = frame };
        Open(date, frame is null ? "restart" : "day_started"); planRecorded = false; Flush();
    }
    public bool Flush()
    {
        string json = JsonSerializer.Serialize(nights);
        if (json == durable) return true;
        if (nights.Any(n => !NativeSchemas.Valid("night", Copy(n))))
            throw new InvalidDataException("Invalid night producer; history preserved");
        if (!AtomicFileWriter.TryWrite(PathName, json).Ok) return false;
        durable = json; return true;
    }
}

public sealed class BarrierFacts
{
    public sealed record Fact(string kind, string instance_id, string stage, int stage_index,
        long opened_frame, long opened_ms, long stage_since_ms, int decisions_served = 0);
    static readonly JsonElement rows = JsonSerializer.Deserialize<JsonElement>(new StreamReader(typeof(ArtiCore.Pure.FrameFacts)
        .Assembly.GetManifestResourceStream("barrier_stages.json")!).ReadToEnd()).GetProperty("barriers");
    readonly Dictionary<string, Fact> active = new();
    readonly Action<JsonElement> emit;
    public BarrierFacts(Action<JsonElement> emitEdge) => emit = emitEdge;
    public Fact? Current => active.GetValueOrDefault("host_pause") ?? active.GetValueOrDefault("night") ?? active.Values.LastOrDefault();
    public bool Contains(string kind) => active.ContainsKey(kind);
    void Edge(Fact b, string edge) => emit(JsonSerializer.SerializeToElement(new {
        edge, barrier_kind = b.kind, b.instance_id, b.stage, b.stage_index }));
    public void Stage(string kind, string stage, long frame, long ms, int index = 0)
    {
        var stages = rows.EnumerateArray().Single(r => r.GetProperty("kind").GetString() == kind)
            .GetProperty("stages").EnumerateArray().Select(s => s.GetString()).ToArray();
        if (stages.Length > 0 && !stages.Contains(stage)) throw new InvalidDataException("Unlisted barrier stage");
        if (!active.TryGetValue(kind, out var b)) {
            b = new(kind, kind + ":" + ms + ":" + frame, stage, index, frame, ms, ms);
            active.Add(kind, b); Edge(b, "opened"); return;
        }
        if (kind != "event" && Array.IndexOf(stages, stage) < Array.IndexOf(stages, b.stage)) return;
        if (b.stage == stage && b.stage_index == index) return;
        active[kind] = b = b with { stage = stage, stage_index = index, stage_since_ms = ms }; Edge(b, "stage");
    }
    public void Close(string kind) { if (active.Remove(kind, out var b)) Edge(b, "closed"); }
}
