using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ArtiStardewBridge.Protocol;

// R0-03: the same game-thread owner persists and clears episodes. No second owner/thread.
public sealed partial class NativeActionOwner
{
    string? haltFolder;
    JsonObject? haltRecord;
    bool haltPersisted;
    long clearSeq = -1;
    public JsonObject? Rearmed { get; private set; }
    public List<string> HaltNotes { get; } = new();
    public bool ProgressionAllowed => !this.recordFaulted && (!this.stopLatched || this.haltRecord?["scope"]?.ToString() == "core");
    public JsonObject? HaltFact => !this.stopLatched || this.haltRecord is null ? null : new JsonObject {
        ["episode_id"] = Copy(this.haltRecord["episode_id"]!), ["halt_seq"] = Copy(this.haltRecord["halt_seq"]!),
        ["entries_digest"] = Copy(this.haltRecord["entries_digest"]!), ["scope"] = Copy(this.haltRecord["scope"]!),
        ["since_ms"] = Copy(this.haltRecord["since_ms"]!), ["persisted"] = this.haltPersisted,
        ["entries"] = Copy(this.haltRecord["entries"]!) };

    internal void NoteHalt(string note)
    { if (!this.HaltNotes.Contains(note)) { if (this.HaltNotes.Count == 4) this.HaltNotes.RemoveAt(0); this.HaltNotes.Add(note); } }
    internal static string Hash(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
    static JsonNode Copy(JsonNode node) => JsonNode.Parse(node.ToJsonString())!;
    static string EntriesDigest(JsonArray entries)
    {
        var copy = Copy(entries).AsArray(); foreach (var entry in copy) entry!.AsObject().Remove("file_present");
        var text = new StringBuilder(); VerbRequest.WriteCanonical(text, JsonSerializer.SerializeToElement(copy));
        return Hash(Encoding.UTF8.GetBytes(text.ToString()));
    }
    static JsonObject ReArmHistory(JsonObject record) => new() { ["episode_id"] = Copy(record["episode_id"]!),
        ["halt_seq"] = Copy(record["halt_seq"]!), ["ms"] = Copy(record["rearmed"]!["ms"]!),
        ["stop_ids"] = new JsonArray(record["entries"]!.AsArray().Select(e => Copy(e!["stop_id"]!)).ToArray()) };

    // Called once per runtime, before any session publication or command polling. The previous
    // session is the pre-B1 runtime witness; a persisted B1 ring header takes precedence when present.
    public void StartHalt(string folder, CommandInboxIdentity identity, long nowMs, ActionOwnerRecordHandoff? initial = null)
    {
        if (this.haltFolder is not null) return;
        this.haltFolder = folder;
        string? previous = null;
        try { previous = JsonNode.Parse(AtomicFileWriter.ReadAllText(Path.Combine(folder, "session.json")))?["runtime_instance_id"]?.GetValue<string>(); }
        catch (Exception e) when (RecordError(e)) { }
        foreach (string ring in Directory.EnumerateFiles(folder, "occurrences.*.json").OrderByDescending(File.GetLastWriteTimeUtc).Take(1))
        { try { var r = JsonNode.Parse(AtomicFileWriter.ReadAllText(ring)); if (r is JsonObject && (previous is null || r["save_id"]?.ToString() == identity.SaveId && r["player_id"]?.ToString() == identity.PlayerId.ToString()))
                previous = r["runtime_id"]?.GetValue<string>() ?? previous; } catch (Exception e) when (RecordError(e)) { } }
        string path = Path.Combine(folder, "halt.json");
        if (File.Exists(path))
        {
            try {
                if (new FileInfo(path).Length > 32768) throw new JsonException("halt oversize");
                this.haltRecord = JsonNode.Parse(AtomicFileWriter.ReadAllText(path)) as JsonObject ?? throw new JsonException("halt object");
                if (!ValidHalt(this.haltRecord)) throw new JsonException("halt schema");
                this.stopLatched = this.haltRecord["state"]!.ToString() == "latched"; this.haltPersisted = true;
                this.Rearmed = this.stopLatched ? this.haltRecord["last_rearmed"] is { } history ? Copy(history).AsObject() : null : ReArmHistory(this.haltRecord);
            } catch (Exception e) when (RecordError(e)) {
                this.haltRecord = null;
                this.AppendHalt("native", "halt_record_corrupt", "all", "native", null, new string('0', 64), nowMs);
                AtomicFileWriter.Move(path, Path.Combine(folder, "halt.corrupt." + this.boundRuntimeId + ".json"), true);
            }
        }
        string? exited = null;
        try { exited = JsonNode.Parse(AtomicFileWriter.ReadAllText(Path.Combine(folder, "mod_exit.json")))?["runtime_id"]?.GetValue<string>(); }
        catch (Exception e) when (RecordError(e)) { }
        // X6 wins over state:rearmed and old tokens. Historical discharges are retained.
        if (previous is not null && previous != this.boundRuntimeId && exited != previous)
            this.AppendHalt("native", "native_unclean_exit", "all", "native", null, new string('0', 64), nowMs);
        using var poller = new ActionOwnerRecordPoller(folder);
        this.ConsumeHalt(initial ?? poller.PollOnce(), identity, nowMs, false, evaluateToken: false);
    }

    public void WriteModExit(long nowMs)
    { if (this.haltFolder is { } folder) WriteHaltFile(Path.Combine(folder, "mod_exit.json"), new JsonObject { ["runtime_id"] = this.boundRuntimeId, ["ms"] = nowMs }); }

    public void Contain(long nowMs)
    {
        this.AppendHalt("native", "native_containment", "all", "native", null, new string('0', 64), nowMs);
        if (this.haltFolder is { } folder) this.haltPersisted = WriteHaltFile(Path.Combine(folder, "halt.json"), this.haltRecord!);
    }

    void AppendHalt(string source, string cause, string scope, string writer, string? stopId, string sha, long nowMs)
    {
        if (source != "native" && this.stopLatched && this.haltRecord?["entries"]!.AsArray().Any(e => e!["source"]!.ToString() == source
            && e["stop_sha256"]!.ToString() == sha) == true) return;
        long seq = (this.haltRecord?["halt_seq"]?.GetValue<long>() ?? 0) + 1;
        stopId ??= $"native:{this.boundRuntimeId}:{seq}";
        if (!this.stopLatched || this.haltRecord is null)
        {
            this.haltRecord = new JsonObject { ["v"] = "arti.halt/1", ["state"] = "latched", ["episode_id"] = stopId,
                ["halt_seq"] = seq, ["scope"] = scope, ["since_ms"] = nowMs, ["entries"] = new JsonArray() };
            if (this.Rearmed is not null) this.haltRecord["last_rearmed"] = Copy(this.Rearmed);
        }
        var entries = this.haltRecord["entries"]!.AsArray();
        if (entries.Count == 16) { entries.RemoveAt(15); cause = "entries_overflow"; scope = "all"; }
        entries.Add(new JsonObject { ["n"] = entries.Count + 1, ["source"] = source, ["stop_id"] = stopId,
            ["stop_sha256"] = sha, ["cause"] = cause, ["scope"] = scope, ["writer"] = writer, ["latched_ms"] = nowMs, ["file_present"] = source != "native" });
        this.haltRecord["halt_seq"] = seq;
        if (scope == "all") this.haltRecord["scope"] = "all";
        this.haltRecord["entries_digest"] = EntriesDigest(entries);
        this.stopLatched = true; this.haltPersisted = false;
        if (this.Slot?.ActiveAttempt?.Client != "progression" || !this.ProgressionAllowed) this.Slot?.Cancel("stop");
        this.releaseRequested = this.Slot?.InputBusy == true; this.releaseAcked = !this.releaseRequested;
    }

    internal void ConsumeHalt(ActionOwnerRecordHandoff snap, CommandInboxIdentity identity, long nowMs, bool inflight, bool evaluateToken = true)
    {
        if (snap.Stops is null) return; // an empty initial handoff cannot clear startup RF
        bool fault = false;
        foreach (var stop in snap.Seq <= this.clearSeq ? Array.Empty<HaltStopRead>() : snap.Stops)
        {
            if (!stop.Present) continue;
            if (stop.Record is not { } value)
            {
                fault = true; this.ApplyRecordFault(CommandRejectReason.MalformedJson, inflight);
                // ADR-21 (accepted, CT 2026-10-27 option A): unreadable bytes retain RF; no invented hash/HA.
                if (stop.FaultPolls >= 3 && stop.Sha256 is { } hash) this.AppendHalt(stop.Source, "malformed", "all", "native", null, hash, nowMs);
                continue;
            }
            bool legacy = !value.TryGetProperty("v", out _);
            string S(string key) => value.GetProperty(key).GetString()!;
            if (legacy ? S("runtime_instance_id") != this.boundRuntimeId || S("session_id") != identity.SessionId
                : S("save_id") != identity.SaveId || value.GetProperty("player_id").GetInt64() != identity.PlayerId) this.NoteHalt("stop_identity_mismatch");
            this.AppendHalt(stop.Source, legacy ? "legacy" : S("cause"), legacy ? "core" : S("scope"), legacy ? "legacy" : S("writer"),
                legacy ? "legacy:" + stop.Sha256 : S("stop_id"), stop.Sha256!, nowMs);
        }
        if (!fault) this.ClearRecordFault();
        if ((fault || this.stopLatched) && inflight && this.Slot?.ActiveAttempt?.Client != "progression")
        { this.releaseRequested = true; this.releaseAcked = false; }
        if (this.haltFolder is null) return;
        if (this.stopLatched && this.haltRecord is { } record)
        {
            foreach (var entry in record["entries"]!.AsArray()) entry!["file_present"] = entry["source"]!.ToString() != "native"
                && (snap.Stops?.Any(s => s.Present && s.Source == entry["source"]!.ToString() && s.Sha256 == entry["stop_sha256"]!.ToString()) == true);
            if (!this.haltPersisted || !File.Exists(Path.Combine(this.haltFolder, "halt.json")))
                this.haltPersisted = WriteHaltFile(Path.Combine(this.haltFolder, "halt.json"), record);
            if (snap.Stops?.All(s => !s.Present) == true && snap.RearmRaw is null) this.NoteHalt("halt_record_missing");
        }
        if (!evaluateToken || this.recordFaulted || snap.RearmRaw is not { } raw) return;
        JsonObject? token;
        try { token = JsonNode.Parse(raw) as JsonObject ?? throw new JsonException("token object"); if (!ValidToken(token)) throw new JsonException("token schema"); }
        catch (Exception e) when (RecordError(e)) { this.NoteHalt("rearm_token_mismatch"); return; }
        if (!this.stopLatched) { this.NoteHalt("rearm_token_unlatched"); return; }
        if (new[] { "episode_id", "halt_seq", "entries_digest" }.Any(k => token[k]!.ToJsonString() != this.haltRecord![k]!.ToJsonString()))
        { this.NoteHalt("rearm_token_stale"); return; }
        // HB-H8: stat on the game thread, including files created after the poll snapshot.
        if (new[] { "stop.json", "stop_all.json" }.Any(n => File.Exists(Path.Combine(this.haltFolder, n))))
        { this.NoteHalt("rearm_waiting_stop_files"); return; }
        // A watchdog token names every stop its Python gate read; each is stat'ed here, in the same tick.
        if (GuardsBlock(token) is { } held) { this.NoteHalt(held); return; }
        var rearmed = Copy(this.haltRecord!).AsObject(); rearmed["state"] = "rearmed";
        rearmed["rearmed"] = new JsonObject { ["halt_seq"] = Copy(token["halt_seq"]!), ["token_sha256"] = Hash(Encoding.UTF8.GetBytes(raw)), ["ms"] = nowMs };
        if (!WriteHaltFile(Path.Combine(this.haltFolder, "halt.json"), rearmed)) return;
        this.haltRecord = rearmed; this.Rearmed = ReArmHistory(rearmed); this.haltPersisted = true;
        try { File.Delete(Path.Combine(this.haltFolder, "rearm.json")); } catch (Exception e) when (RecordError(e)) { }
        this.clearSeq = snap.Seq;
        this.DisarmFenced(true); this.ClearStopLatch(); // persistence precedes memory clear
    }

    /// <summary>
    /// Why a token's <c>stop_guards</c> keep the latch, or null. A token without the list is the owner's
    /// manual re-arm and keeps today's check (the inbox stop files only). Every guard must be a fully qualified path
    /// (else <c>rearm_token_mismatch</c>); any guard that exists now keeps the latch (<c>rearm_waiting_stop_files</c>).
    /// Plain loops and named static methods only: no closure in the halt consumption path (IL rows).
    /// </summary>
    internal static string? GuardsBlock(JsonObject token)
    {
        if (token["stop_guards"] is not JsonArray guards) return null;
        foreach (JsonNode? guard in guards)
            if (GuardPath(guard) is not { } path || !Path.IsPathFullyQualified(path)) return "rearm_token_mismatch";
        foreach (JsonNode? guard in guards)
            if (GuardPresent(GuardPath(guard)!)) return "rearm_waiting_stop_files";
        return null;
    }

    static string? GuardPath(JsonNode? guard)
    {
        if (guard is JsonValue value && value.TryGetValue(out string? path)) return path;
        return null;
    }

    /// <summary>Existence counts, never content: a file of zero bytes or a directory is present. Only a missing file or a
    /// missing directory reads as absent; any other failure to stat (a sharing or permission fault, a bad path) is present.</summary>
    internal static bool GuardPresent(string path)
    {
        try { File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
        catch (Exception) { return true; }
    }

    static bool WriteHaltFile(string path, JsonObject value)
    {
        return AtomicFileWriter.TryWrite(path, value.ToJsonString(), flushToDisk: true).Ok;
    }

    static bool RecordError(Exception e) => e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException or FormatException or OverflowException or KeyNotFoundException or ArgumentException;
    internal static bool ValidToken(JsonObject t) => ArtiCore.Pure.NativeSchemas.Valid("rearm", t);
    internal static bool ValidStop(JsonObject t) => ArtiCore.Pure.NativeSchemas.Valid("stop", t);
    static bool ValidHalt(JsonObject h) => ArtiCore.Pure.NativeSchemas.Valid("halt", h)
        && h["entries_digest"]?.ToString() == EntriesDigest(h["entries"]!.AsArray());
}
