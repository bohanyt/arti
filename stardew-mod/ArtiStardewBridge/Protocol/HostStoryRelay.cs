using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>Values copied on the game thread; Instance is only compared by reference, never inspected here.</summary>
public sealed record HostStorySample(object? Instance, string SaveId, string? EventId, string EventType,
    string Source, string Date, int Time, string Location, string? Text, string? Speaker,
    bool Narration, long Frame, long ObservedAtMs, object? TextInstance = null, int TextPage = 0,
    string HostPlayerId = "UNKNOWN");

/// <summary>ADR 63 HOST opt-in. Only observed native text; no state changes or inferred story facts.</summary>
public sealed class HostStoryRelay
{
    readonly string directory, runtime;
    HostStorySample? previous;
    string? episode;
    int episodes, lines;
    long sequence;
    bool initialized;
    public string PathName => Path.Combine(directory, "host_story.jsonl");

    public HostStoryRelay(string directory, string runtimeId)
    {
        this.directory = directory;
        runtime = runtimeId;
    }

    public static bool CanCapture(bool enabled, bool isMainPlayer, bool isSplitScreen) =>
        enabled && isMainPlayer && !isSplitScreen;

    // Returns without any filesystem access when disabled or on a farmhand/split-screen.
    // State advances only after a successful append, so an unavailable file can be retried next tick.
    public void Observe(bool enabled, bool isMainPlayer, bool isSplitScreen, HostStorySample sample)
    {
        if (!CanCapture(enabled, isMainPlayer, isSplitScreen)) return;
        if (!initialized) {
            // Preserve the bridge's file-wide dedupe sequence across HOST mod restarts.
            if (File.Exists(PathName)) {
                using var file = new FileStream(PathName, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                using var reader = new StreamReader(file, Encoding.UTF8);
                while (reader.ReadLine() is { } line) {
                    try {
                        using var row = JsonDocument.Parse(line);
                        if (row.RootElement.ValueKind == JsonValueKind.Object
                            && row.RootElement.TryGetProperty("seq", out var seq) && seq.ValueKind == JsonValueKind.Number
                            && seq.TryGetInt64(out long value))
                            sequence = Math.Max(sequence, value);
                    } catch (JsonException) { /* Incomplete crash tails are not witnessed facts. */ }
                }
            }
            initialized = true;
        }
        bool edge = !ReferenceEquals(previous?.Instance, sample.Instance)
            || previous?.SaveId != sample.SaveId || previous?.Source != sample.Source;
        bool textEdge = previous?.Text != sample.Text || !ReferenceEquals(previous?.TextInstance, sample.TextInstance)
            || previous?.TextPage != sample.TextPage || previous?.Speaker != sample.Speaker;
        if (!edge && !textEdge) return;
        int nextEpisodes = episodes, nextLines = lines;
        string? nextEpisode = episode;
        long nextSeq = sequence;
        var rows = new StringBuilder();
        void Row(string phase, HostStorySample facts, string id, int order)
        {
            rows.Append(JsonSerializer.Serialize(new {
                v = 1, schema = "arti.host_story/1", runtime_id = runtime, seq = ++nextSeq, episode = id, episode_id = id,
                phase, save_id = facts.SaveId, witness = "host",
                event_id = string.IsNullOrEmpty(facts.EventId) ? "UNKNOWN" : facts.EventId,
                host_player_id = facts.HostPlayerId,
                event_type = facts.EventType, source = facts.Source,
                date = facts.SaveId == sample.SaveId ? sample.Date : facts.Date,
                time = facts.SaveId == sample.SaveId ? sample.Time : facts.Time,
                location = facts.Location, speaker = phase == "line" ? facts.Speaker ?? "UNKNOWN" : "UNKNOWN",
                text = phase == "line" ? facts.Text ?? "" : "", narration = facts.Narration,
                change = "", line_order = order, frame = sample.Frame,
                utc_ms = sample.ObservedAtMs, observed_at_ms = sample.ObservedAtMs
            })).Append('\n');
        }
        if (edge && nextEpisode is not null && previous is not null)
        {
            Row("ended", previous, nextEpisode, nextLines);
            nextEpisode = null;
        }
        if (edge && sample.Instance is not null)
        {
            nextEpisode = runtime + ":story:" + ++nextEpisodes;
            nextLines = 0;
            Row("started", sample, nextEpisode, 0);
        }
        if (nextEpisode is not null && !string.IsNullOrWhiteSpace(sample.Text)
            && (edge || textEdge))
            Row("line", sample, nextEpisode, ++nextLines);
        if (rows.Length != 0) Append(rows.ToString());
        previous = sample; episode = nextEpisode; episodes = nextEpisodes; lines = nextLines; sequence = nextSeq;
    }

    void Append(string rows)
    {
        Directory.CreateDirectory(directory);
        byte[] bytes = new UTF8Encoding(false).GetBytes(rows);
        // One HOST writer, concurrent tail readers. Roll back a partial failed write before retrying.
        using var stream = new FileStream(PathName, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite);
        long start = stream.Seek(0, SeekOrigin.End);
        try {
            // Isolate any incomplete crash tail; never join a valid new row onto malformed old JSON.
            if (start != 0) {
                stream.Seek(-1, SeekOrigin.End);
                bool incomplete = stream.ReadByte() != '\n';
                stream.Seek(0, SeekOrigin.End);
                if (incomplete) stream.WriteByte((byte)'\n');
            }
            stream.Write(bytes, 0, bytes.Length); stream.Flush();
        }
        catch { stream.SetLength(start); throw; }
    }
}
