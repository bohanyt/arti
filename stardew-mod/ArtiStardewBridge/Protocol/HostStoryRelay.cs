using System.Text;
using System.Text.Json;
using System.Threading.Channels;

namespace ArtiStardewBridge.Protocol;

/// <summary>Values copied on the game thread; Instance is only compared by reference, never inspected here.</summary>
public sealed record HostStorySample(object? Instance, string SaveId, string? EventId, string EventType,
    string Source, string Date, int Time, string Location, string? Text, string? Speaker,
    bool Narration, long Frame, long ObservedAtMs, object? TextInstance = null, int TextPage = 0,
    string HostPlayerId = "UNKNOWN");

/// <summary>ADR 63 HOST opt-in. Only observed native text; no state changes or inferred story facts.</summary>
public sealed class HostStoryRelay
{
    readonly record struct Presence(long Window, bool Solo, long Now, string? Gate);
    readonly record struct Observation(long Window, HostStorySample Sample);
    const int MaxPendingObservations = 64;
    readonly string directory, runtime;
    // TryWrite refuses excess observations. The game tick never waits for space.
    readonly Channel<Observation> pending = Channel.CreateBounded<Observation>(
        new BoundedChannelOptions(MaxPendingObservations) { SingleWriter = true });
    HostStorySample? lastCaptured;
    HostStorySample? previous;
    string? episode;
    int episodes, lines;
    long sequence;
    bool initialized;
    bool watching;
    string? windowSave;
    long window, submittedWindow, submittedTick, writtenWindow;
    EventWaitHandle? gate;
    string? gateName;
    Task? writer;
    volatile bool presenceDirty;
    public string PathName => Path.Combine(directory, "host_story.jsonl");
    public bool HasVisibleStory => lastCaptured?.Instance is not null;

    public HostStoryRelay(string directory, string runtimeId)
    {
        this.directory = directory;
        runtime = runtimeId;
    }

    public static bool CanCapture(bool enabled, bool isMainPlayer, bool isSplitScreen) =>
        enabled && isMainPlayer && !isSplitScreen;

    // The local event is revoked on the game thread. A voice turn can check
    // it even while the background writer is unable to replace the heartbeat.
    public void PublishPresence(bool solo, string saveId, long now, long tick)
    {
        bool changed = watching != solo || (windowSave != saveId && windowSave != "0");
        watching = solo;
        windowSave = saveId;
        if (changed)
        {
            gate?.Reset();
            gate?.Dispose();
            gate = null;
            gateName = null;
            Interlocked.Increment(ref window);
            if (solo && OperatingSystem.IsWindows())
            {
                string name = "Local\\ArtiHostStory-" + Guid.NewGuid().ToString("N");
                gate = new EventWaitHandle(true, EventResetMode.ManualReset, name);
                gateName = name;
            }
        }

        if (writer is { IsCompleted: false }) return;
        bool refreshDue = submittedWindow != window ||
            ((solo || presenceDirty) && (tick < submittedTick || tick - submittedTick >= 60));
        if (refreshDue)
        {
            submittedWindow = window;
            submittedTick = tick;
            ScheduleWriter(new Presence(window, solo, now, gateName));
        }
        else ScheduleWriter(null);
    }

    // The game loop only copies observations. Sequence recovery and appends
    // use the same serial background worker as presence publication.
    public void Capture(HostStorySample sample)
    {
        if (!watching)
        {
            // Do not relay a private page held open through disconnect.
            lastCaptured = sample;
            return;
        }
        if (sample.Instance is null && !HasVisibleStory) return;
        if (!Changed(lastCaptured, sample) || !pending.Writer.TryWrite(new Observation(window, sample))) return;
        lastCaptured = sample;
        ScheduleWriter(null);
    }

    void ScheduleWriter(Presence? update)
    {
        if (writer is { IsCompleted: false } || (update is null && !pending.Reader.TryPeek(out _))) return;
        writer = Task.Run(() => Drain(update));
    }

    void Drain(Presence? update)
    {
        if (update is { } stamp) WritePresence(stamp);
        for (int i = 0; i < MaxPendingObservations && pending.Reader.TryPeek(out var observation); i++)
        {
            try
            {
                if (observation.Window == Volatile.Read(ref window))
                    WriteObservation(observation.Sample, observation.Window);
            }
            catch (Exception)
            {
                // Retain the first unwritten observation for a later tick.
                return;
            }
            pending.Reader.TryRead(out _);
        }
    }

    void WritePresence(Presence stamp)
    {
        try
        {
            if (Volatile.Read(ref window) != stamp.Window) return;
            Directory.CreateDirectory(directory);
            presenceDirty = !AtomicFileWriter.TryWrite(Path.Combine(directory, "host_story.session.json"),
                JsonSerializer.Serialize(new {
                    runtime_id = runtime, window = stamp.Window, relay_window = stamp.Solo ? (long?)stamp.Window : null,
                    utc_ms = stamp.Now, pid = Environment.ProcessId, gate = stamp.Gate
                }), retry: false).Ok;
        }
        catch (Exception)
        {
            presenceDirty = true;
        }
    }

    // Synchronous protocol entry point. Runtime capture uses Capture instead.
    public void Observe(bool enabled, bool isMainPlayer, bool isSplitScreen, HostStorySample sample)
    {
        if (!CanCapture(enabled, isMainPlayer, isSplitScreen)) return;
        WriteObservation(sample, 0);
    }

    static bool SceneChanged(HostStorySample? prior, HostStorySample sample) =>
        !ReferenceEquals(prior?.Instance, sample.Instance) || prior?.SaveId != sample.SaveId || prior?.Source != sample.Source;

    static bool TextChanged(HostStorySample? prior, HostStorySample sample) =>
        prior?.Text != sample.Text || !ReferenceEquals(prior?.TextInstance, sample.TextInstance)
        || prior?.TextPage != sample.TextPage || prior?.Speaker != sample.Speaker;

    static bool Changed(HostStorySample? prior, HostStorySample sample) =>
        SceneChanged(prior, sample) || TextChanged(prior, sample);

    void WriteObservation(HostStorySample sample, long sampleWindow)
    {
        if (sampleWindow != writtenWindow)
        {
            previous = null;
            episode = null;
            writtenWindow = sampleWindow;
        }
        if (sample.Instance is null && previous?.Instance is null) return;
        if (!initialized) {
            // Preserve the bridge's file-wide dedupe sequence across HOST mod restarts.
            try {
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
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            initialized = true;
        }
        bool edge = SceneChanged(previous, sample);
        bool textEdge = TextChanged(previous, sample);
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
                utc_ms = sample.ObservedAtMs, observed_at_ms = sample.ObservedAtMs,
                relay_window = sampleWindow != 0 ? (long?)sampleWindow : null
            })).Append('\n');
        }
        if (edge && nextEpisode is not null && previous is not null)
        {
            Row("ended", previous, nextEpisode, nextLines);
            nextEpisode = null;
        }
        if ((edge || (nextEpisode is null && textEdge)) && sample.Instance is not null)
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
