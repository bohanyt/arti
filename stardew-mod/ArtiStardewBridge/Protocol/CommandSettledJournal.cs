using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Durable dedupe metadata for the existing single-file command inbox.
///
/// This is deliberately NOT a command queue, transport, action owner, or
/// world-state store. <c>request.json</c> remains the sole command ingress and
/// <c>result.json</c> remains the latest-result mailbox. The journal only
/// remembers admission/terminal facts strongly enough to prevent a consumed
/// mutation from becoming admissible again after result loss, compaction, or
/// process restart.
/// </summary>
public sealed partial class CommandSettledJournal
{
    public const int SchemaVersion = 1;
    public const string RequestIdPrefix = "j1-";
    public const string DefaultFileName = "command-settled-journal-v1.json";
    public const int DefaultMaxExactSettledRows = 64;
    public const int DefaultMaxUnresolvedRows = 64;
    public const int DefaultMaxArchives = 32;
    public const int MaxTombstones = 256;
    /// <summary>
    /// PM-1 fix10e (a14 d10:J612): bound on Uncertain rows whose native release receipt proves input idle. Only their
    /// world effect is unknown; past this bound the oldest retires. Rows without that receipt are never retired by
    /// age, count, or day, so genuinely held input still reaches JournalCapacityReached.
    /// </summary>
    public const int DefaultMaxReleasedUncertainRows = 24;
    public const string RetiredSuperseded = "superseded_same_target", RetiredDayBoundary = "retired_day_boundary",
        RetiredBound = "retired_bound";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = false,
        WriteIndented = false,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
    };

    private readonly object sync = new();
    private readonly string path;
    private readonly string backupPath;
    private readonly int maxExactSettledRows;
    private readonly int maxUnresolvedRows;
    private readonly int maxArchives;
    private readonly int maxReleasedUncertainRows;
    private long lastArchiveTicks;
    private JournalSnapshot snapshot = JournalSnapshot.Empty();
    private bool hardFault; // every non-deferred fault; only a durable synchronous persist clears it
    // fix55D: deferred-writer faults are kept apart so a verified heal can clear them without masking any other fault.
    private bool faulted { get => this.hardFault || this.deferredFault; set => this.hardFault = value; }

    public CommandSettledJournal(
        string? path = null,
        int maxExactSettledRows = DefaultMaxExactSettledRows,
        int maxUnresolvedRows = DefaultMaxUnresolvedRows,
        int maxArchives = DefaultMaxArchives,
        int maxReleasedUncertainRows = DefaultMaxReleasedUncertainRows,
        long maxSegmentBytes = DefaultMaxSegmentBytes,
        int maxRetiredSegments = DefaultMaxRetiredSegments,
        bool? deferTerminalWrites = null)
    {
        if (maxExactSettledRows < 1)
            throw new ArgumentOutOfRangeException(nameof(maxExactSettledRows));
        if (maxUnresolvedRows < 1)
            throw new ArgumentOutOfRangeException(nameof(maxUnresolvedRows));
        if (maxArchives < 1)
            throw new ArgumentOutOfRangeException(nameof(maxArchives));

        this.path = string.IsNullOrWhiteSpace(path) ? DefaultPath() : path!;
        this.backupPath = this.path + ".bak";
        bool livePath = string.Equals(System.IO.Path.GetFullPath(this.path), System.IO.Path.GetFullPath(DefaultPath()), StringComparison.OrdinalIgnoreCase);
        if (livePath)
        {
            if (long.TryParse(Environment.GetEnvironmentVariable("ARTI_JOURNAL_SEGMENT_BYTES"), out long bytes) && bytes > 0)
                maxSegmentBytes = bytes;
            if (int.TryParse(Environment.GetEnvironmentVariable("ARTI_JOURNAL_RETAIN_SEGMENTS"), out int count) && count >= 0)
                maxRetiredSegments = count;
        }
        if (maxSegmentBytes < 1) throw new ArgumentOutOfRangeException(nameof(maxSegmentBytes));
        if (maxRetiredSegments < 0) throw new ArgumentOutOfRangeException(nameof(maxRetiredSegments));
        this.activePath = this.path;
        this.maxSegmentBytes = maxSegmentBytes;
        this.maxRetiredSegments = maxRetiredSegments;
        this.maxExactSettledRows = maxExactSettledRows;
        this.maxUnresolvedRows = maxUnresolvedRows;
        this.maxArchives = maxArchives;
        this.deferTerminal = ResolveDeferTerminal(deferTerminalWrites, livePath);
        if (this.deferTerminal)
        {
            this.WriteTimings.Deferred = this.DeferredWriteTimings;
            this.WriteTimings.DeferredPending = () => this.DeferredPending;
            this.WriteTimings.DeferredFault = () => this.DeferredFaultReason;
        }
        if (livePath)
        {
            JournalWriteProbe.Live = this.WriteTimings;
            // fix54I: a clean process exit waits (bounded) for queued terminal records; an abrupt one recovers as Uncertain.
            if (this.deferTerminal) AppDomain.CurrentDomain.ProcessExit += (_, _) => this.WaitForDeferredWrites(5000);
        }
        // Keep released rows strictly under the capacity so they alone can never fill it.
        this.maxReleasedUncertainRows = Math.Max(1, Math.Min(maxReleasedUncertainRows, maxUnresolvedRows - 1));
        try { this.Load(); }
        catch { this.faulted = true; } // Filesystem startup failures never escape into native admission.
    }

    public string PathName => this.path;
    public JournalWriteProbe WriteTimings { get; } = new();
    public string ArchiveDirectory => this.path + ".archive";
    public int MaxUnresolvedRows => this.maxUnresolvedRows;
    public string? BlockedReason => this.Faulted ? CommandRejectReason.JournalPersistenceFault.ToString()
        : this.UnresolvedRowCount >= this.maxUnresolvedRows ? CommandRejectReason.JournalCapacityReached.ToString() : null;

    /// <summary>Bind only to authoritative native identity, never to an incoming request.</summary>
    public bool BindScope(CommandInboxIdentity identity)
    {
        lock (this.sync)
        {
            this.DrainDeferredUnsafe();
            if (this.faulted) return false;
            if (string.IsNullOrWhiteSpace(identity.RuntimeInstanceId) || string.IsNullOrWhiteSpace(identity.SessionId)
                || string.IsNullOrWhiteSpace(identity.SaveId) || identity.PlayerId == 0)
                return false; // Missing native identity is not a session boundary.
            JournalScope scope = new(identity.RuntimeInstanceId, identity.SessionId, identity.SaveId,
                identity.PlayerId, identity.ScreenId);
            if (this.snapshot.Scope == scope) return true;
            if (this.snapshot.Scope is null)
            {
                // V1 migration: runtime + session already bound every legacy namespace.
                // Retain matching rows conservatively; never infer their outcome from age.
                string[] stale = this.snapshot.Namespaces.Values.Where(ns =>
                    ns.RuntimeInstanceId != scope.RuntimeInstanceId || ns.SessionId != scope.SessionId)
                    .Select(ns => ns.NamespaceKey).ToArray();
                if (stale.Length > 0 && !this.TryArchiveUnsafe()) return false;
                foreach (string key in stale)
                {
                    var ns = this.snapshot.Namespaces[key];
                    string watermark = NamespaceKey(new MenuStepCommandRequest("", ns.SessionId, 0, null, "", 0, 0, 0,
                        RuntimeInstanceId: ns.RuntimeInstanceId, OwnerId: ns.OwnerId));
                    Bump(this.snapshot.ArchivedWatermarks, watermark, ns.Generation);
                }
                foreach (string key in stale)
                {
                    foreach (JournalRow row in this.snapshot.Rows.Where(row => row.NamespaceKey == key))
                        this.changes.Rows[RowKey(row)] = new RowChange(row.NamespaceKey, row.RequestSeq, null, false);
                    this.snapshot.Rows.RemoveAll(row => row.NamespaceKey == key);
                    this.snapshot.Namespaces.Remove(key);
                    this.changes.Namespaces[key] = null;
                }
                this.snapshot.Scope = scope;
                return this.TryPersistUnsafe("bind_scope");
            }
            return this.ArchiveAndResetUnsafe(scope);
        }
    }

    private bool TryArchiveUnsafe()
    {
        if (this.snapshot.Namespaces.Count == 0) return true;
        // Archive the complete checksummed snapshot before changing active metadata.
        // A failed/partial archive never permits retirement or native admission.
        for (int attempt = 1; attempt <= PersistAttempts; attempt++)
        {
            string? pending = null;
            try
            {
                Directory.CreateDirectory(this.ArchiveDirectory);
                this.lastArchiveTicks = Math.Max(DateTime.UtcNow.Ticks, this.lastArchiveTicks + 1);
                string archive = System.IO.Path.Combine(this.ArchiveDirectory,
                    this.lastArchiveTicks.ToString("D19") + "-" + Guid.NewGuid().ToString("N") + ".json");
                pending = archive + ".tmp";
                using (FileStream stream = new(pending, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    byte[] bytes = Encoding.UTF8.GetBytes(this.SerializeSnapshotUnsafe());
                    stream.Write(bytes);
                    stream.Flush(flushToDisk: true);
                }
                AtomicFileWriter.Move(pending, archive); // Only a complete, flushed envelope becomes a published archive.
                this.PruneArchivesUnsafe();
                return true;
            }
            catch
            {
                if (attempt < PersistAttempts) Thread.Sleep(PersistRetryDelayMs * attempt);
            }
            finally
            {
                try { if (pending is not null) File.Delete(pending); }
                catch { } // A leftover temp file is never a trusted archive.
            }
        }
        this.faulted = true;
        return false;
    }

    private void PruneArchivesUnsafe()
    {
        // Retention failures do not weaken active replay fences or block a durable reset.
        // Legacy GUID-named archives use their write time; new names have monotonic UTC ticks.
        try
        {
            FileInfo[] archives = new DirectoryInfo(this.ArchiveDirectory).GetFiles("*.json")
                .OrderByDescending(file => file.Name.Length > 20 && file.Name[19] == '-'
                    && long.TryParse(file.Name.AsSpan(0, 19), out long ticks) ? ticks : file.LastWriteTimeUtc.Ticks)
                .ThenByDescending(file => file.Name, StringComparer.Ordinal).ToArray();
            foreach (FileInfo archive in archives.Skip(this.maxArchives))
            {
                try { archive.Delete(); }
                catch { } // Best effort; retry on the next archive.
            }
        }
        catch { }
    }

    private bool ArchiveAndResetUnsafe(JournalScope? scope, bool nativeSessionReset = false)
    {
        this.DrainDeferredUnsafe(); // fix54I: the archive and reset follow every queued terminal record
        if (this.faulted && !nativeSessionReset) return false;
        bool archived = this.TryArchiveUnsafe();
        if (!archived && !nativeSessionReset) return false;
        // Only a native session rotation may retire a faulted/unarchivable scope.
        // Admission still requires the new empty scope to reach durable storage.
        JournalSnapshot previous = this.snapshot;
        JournalDelta previousChanges = this.changes;
        bool previousCheckpoint = this.checkpointRequired;
        this.snapshot = JournalSnapshot.Empty();
        foreach (var entry in previous.ArchivedWatermarks.Concat(previous.Watermarks))
            this.snapshot.ArchivedWatermarks[entry.Key] = Math.Max(entry.Value, this.snapshot.ArchivedWatermarks.GetValueOrDefault(entry.Key));
        this.snapshot.Scope = scope;
        // Replay transfers the already-recorded fences; serialize only any unacknowledged fence additions.
        this.changes = new JournalDelta { Reset = true };
        foreach (var entry in previousChanges.ArchivedWatermarks.Concat(previousChanges.Watermarks))
            this.changes.ArchivedWatermarks[entry.Key] = Math.Max(entry.Value, this.changes.ArchivedWatermarks.GetValueOrDefault(entry.Key));
        if (this.TryPersistUnsafe("reset")) return true;
        this.snapshot = previous;
        this.changes = previousChanges;
        this.checkpointRequired = previousCheckpoint;
        return false;
    }

    public bool Faulted
    {
        get
        {
            lock (this.sync)
                return this.faulted || this.deferredFaultVisible;
        }
    }

    public int ExactRowCount
    {
        get
        {
            lock (this.sync)
                return this.snapshot.Rows.Count;
        }
    }

    public int UnresolvedRowCount
    {
        get
        {
            lock (this.sync)
                return this.snapshot.Rows.Count(static row => row.State != JournalRowState.Settled);
        }
    }

    /// <summary>Uncertain rows this session retired with an explicit outcome, by reason (fix10e).</summary>
    public IReadOnlyDictionary<string, long> RetiredUncertainCounts
    {
        get
        {
            lock (this.sync)
                return new Dictionary<string, long>(this.snapshot.RetiredCounts, StringComparer.Ordinal);
        }
    }

    /// <summary>
    /// Native day boundary (DayStarted after a bed sleep or a pass-out) and save: every Uncertain row whose native
    /// release receipt proves input idle retires with the recorded outcome "unresolved, retired". A row without that
    /// receipt stays and still counts. The retired row moves to the tombstones and its namespace fence advances, so
    /// its request is never admissible again.
    /// </summary>
    public int RetireReleasedUncertain(string reason = RetiredDayBoundary)
    {
        lock (this.sync)
        {
            this.DrainDeferredUnsafe();
            if (this.faulted) return 0;
            JournalRow[] released = this.snapshot.Rows.Where(InputReleasedUncertain).OrderBy(row => row.Ordinal).ToArray();
            if (released.Length == 0) return 0;
            JournalSnapshot before = CloneUnsafe(this.snapshot);
            JournalDelta previousChanges = JsonSerializer.Deserialize<JournalDelta>(JsonSerializer.SerializeToUtf8Bytes(this.changes, JsonOptions), JsonOptions)!;
            foreach (JournalRow row in released) this.RetireUncertainUnsafe(row, reason);
            if (this.TryPersistUnsafe("retire")) return released.Length;
            this.snapshot = before; // the durable fact is still the uncertain row
            this.changes = previousChanges;
            return 0;
        }
    }

    static bool InputReleasedUncertain(JournalRow row) =>
        row.State == JournalRowState.Uncertain && row.Terminal is { InputIdle: true, Fault: null };

    private void RetireUncertainUnsafe(JournalRow row, string reason)
    {
        row.State = JournalRowState.Retired;
        row.RetiredReason = reason;
        if (row.ResultJson is { Length: > 0 } json)
        {
            try
            {
                if (JsonSerializer.Deserialize<MenuStepCommandResult>(json, JsonOptions) is { } stored)
                    row.ResultJson = JsonSerializer.Serialize(stored with { JournalState = "retired" }, JsonOptions);
            }
            catch (JsonException) { } // the stored uncertain result stays as recorded
        }
        row.Ordinal = ++this.snapshot.LastOrdinal;
        this.ChangeRow(row);
        this.snapshot.RetiredCounts[reason] = this.snapshot.RetiredCounts.GetValueOrDefault(reason) + 1;
        this.changes.RetiredCounts[reason] = this.snapshot.RetiredCounts[reason];
        this.AdvanceSettledFenceUnsafe(row.NamespaceKey);
        this.RetireRowUnsafe(row);
    }

    /// <summary>A verified result supersedes older released uncertainty on the same target; the bound caps the rest.</summary>
    private void RetireSupersededUnsafe(JournalRow settled)
    {
        if (settled.TargetKey is { Length: > 0 } target)
            foreach (JournalRow row in this.snapshot.Rows.Where(row => InputReleasedUncertain(row) && row.TargetKey == target
                         && row.Ordinal < settled.Ordinal).ToArray())
                this.RetireUncertainUnsafe(row, RetiredSuperseded);
        this.EnforceReleasedBoundUnsafe();
    }

    private void EnforceReleasedBoundUnsafe()
    {
        JournalRow[] released = this.snapshot.Rows.Where(InputReleasedUncertain).OrderBy(row => row.Ordinal).ToArray();
        foreach (JournalRow row in released.Take(Math.Max(0, released.Length - this.maxReleasedUncertainRows)))
            this.RetireUncertainUnsafe(row, RetiredBound);
    }

    private static JournalSnapshot CloneUnsafe(JournalSnapshot value) =>
        JsonSerializer.Deserialize<JournalSnapshot>(JsonSerializer.SerializeToUtf8Bytes(value, JsonOptions), JsonOptions)!;

    /// <summary>
    /// Same-target identity of a request (fix10e supersession): op, primitive and its pinned target, never the
    /// request id. Two requests with the same key aim at the same native thing from the same place.
    /// </summary>
    internal static string TargetKeyOf(MenuStepCommandRequest request)
    {
        StringBuilder text = new();
        AppendLine(text, "op=" + request.Op);
        AppendLine(text, "family=" + request.Family);
        AppendLine(text, "primitive=" + request.Primitive);
        AppendLine(text, "location=" + request.ExpectedLocationId);
        AppendLine(text, "direction=" + request.Direction);
        AppendLine(text, "tile=" + request.TileX + "," + request.TileY);
        if (request.VerbEnvelope is { } envelope && envelope.ValueKind == JsonValueKind.Object)
        {
            if (envelope.TryGetProperty("verb", out JsonElement verb)) AppendLine(text, "verb=" + verb);
            if (envelope.TryGetProperty("target", out JsonElement target)) AppendLine(text, "target=" + target.GetRawText());
            if (envelope.TryGetProperty("args", out JsonElement args)) AppendLine(text, "args=" + args.GetRawText());
        }
        if (request.ToolTarget is { } tool) AppendLine(text, $"tool={tool.TileX},{tool.TileY},{tool.QualifiedItemId}");
        if (request.InteractionTarget is { } interaction) AppendLine(text, "interact=" + interaction.TargetToken);
        if (request.ThroughTarget is { } through) AppendLine(text, "through=" + through.TargetToken);
        if (request.TransferTarget is { } transfer) AppendLine(text, $"transfer={transfer.ContainerToken},{transfer.TargetToken},{transfer.Mode}");
        if (request.ExitTarget is { } exit) AppendLine(text, "exit=" + exit.ContainerToken);
        if (request.UiPress is { } press) AppendLine(text, $"uipress={press.MenuType},{press.TargetToken},{press.Button}");
        if (request.UiKeyTarget is { } key) AppendLine(text, $"uikey={key.MenuType},{key.Kind},{key.Key},{key.TargetToken}");
        if (request.SelectionTarget is { } selection) AppendLine(text, $"select={selection.TargetToken},{selection.Slot}");
        AppendLine(text, "end", final: true);
        return Sha256Hex(Encoding.UTF8.GetBytes(text.ToString()));
    }

    public long SettledThrough(MenuStepCommandRequest request)
    {
        lock (this.sync)
        {
            string key = NamespaceKey(request);
            return this.snapshot.Namespaces.TryGetValue(key, out JournalNamespace? ns)
                ? ns.SettledThroughSeq
                : 0;
        }
    }

    public long HighestRequestSeq(MenuStepCommandRequest request)
    {
        lock (this.sync)
            return this.snapshot.Namespaces.GetValueOrDefault(NamespaceKey(request))?.HighestRequestSeq ?? 0;
    }

    /// <summary>
    /// PART_C2: a fresh journal file under the temp directory, never the live default path. Used by the
    /// parameterless <see cref="MenuStepCommandInbox"/> (tests and harnesses only).
    /// </summary>
    public static string IsolatedPath() =>
        System.IO.Path.Combine(System.IO.Path.GetTempPath(), "ArtiStardewBridge-isolated-journal",
            Environment.ProcessId.ToString(System.Globalization.CultureInfo.InvariantCulture)
                + "-" + Guid.NewGuid().ToString("N"),
            DefaultFileName);

    public static string DefaultPath()
    {
        string root = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(root))
            root = AppContext.BaseDirectory;
        return System.IO.Path.Combine(root, "ArtiStardewBridge", DefaultFileName);
    }

    private void Bump(Dictionary<string, long> map, string key, long generation)
    {
        long value = Math.Max(generation, map.GetValueOrDefault(key));
        if (map.TryGetValue(key, out long previous) && previous == value) return;
        map[key] = value;
        (ReferenceEquals(map, this.snapshot.Watermarks) ? this.changes.Watermarks : this.changes.ArchivedWatermarks)[key] = value;
    }

    public static bool IsJournalRequest(MenuStepCommandRequest request) =>
        request.RequestSeq > 0
        || request.RequestId.StartsWith(RequestIdPrefix, StringComparison.Ordinal);

    /// <summary>
    /// Canonical payload digest is built from parsed semantic fields, never
    /// raw JSON bytes/order/whitespace. The Python command client mirrors this
    /// exact representation.
    /// </summary>
    public static string CanonicalPayloadDigest(MenuStepCommandRequest request)
    {
        if (request.VerbEnvelope is { } envelope) return VerbRequest.ImmutableDigest(envelope);
        StringBuilder text = new();
        AppendLine(text, "journal-payload-v1");
        AppendLine(text, "protocol=1");
        AppendLine(text, "op=" + B64(request.Op));
        AppendLine(text, "runtime=" + B64(request.RuntimeInstanceId));
        AppendLine(text, "session=" + B64(request.SessionId));
        AppendLine(text, "owner=" + B64(request.OwnerId));
        AppendLine(text, "generation=" + request.Generation);
        AppendLine(text, "control_seq=" + request.ControlSeq);
        AppendLine(text, "context_generation=" + request.ContextGeneration);
        AppendLine(text, "expected_player_id=" + request.ExpectedPlayerId);
        AppendLine(text, "expected_player_name=" + B64(request.ExpectedPlayerName ?? ""));
        AppendLine(text, "expected_screen_id=" + request.ExpectedScreenId);
        AppendLine(text, "expected_location_id=" + B64(request.ExpectedLocationId));
        AppendLine(text, "deadline_unix_ms=" + request.DeadlineUnixMs);
        AppendLine(text, "family=" + B64(request.Family));
        AppendLine(text, "primitive=" + B64(request.Primitive));
        AppendLine(text, "direction=" + B64(request.Direction));
        AppendLine(text, "ticks=" + request.Ticks);
        AppendLine(text, "tile_x=" + request.TileX);
        AppendLine(text, "tile_y=" + request.TileY, final: true);
        if (request.ExecutionDeadlineUnixMs is { } executionDeadline)
        {
            text.Append('\n');
            AppendLine(text, "execution_deadline_unix_ms=" + executionDeadline, final: true);
        }
        if (request.EventToken is { } eventToken)
        {
            text.Append('\n');
            AppendLine(text, "event_token=" + B64(eventToken), final: true);
        }
        if (request.SegmentStop is { } segmentStop)
        {
            // fix89D / ADR 158: the segment's stop tile is part of the request identity.
            text.Append('\n');
            AppendLine(text, "segment_stop_x=" + segmentStop.X);
            AppendLine(text, "segment_stop_y=" + segmentStop.Y, final: true);
        }
        if (request.PathNext is { } pathNext)
        {
            // fix92A / ADR 162: the path's following stops are part of the request identity too.
            text.Append('\n');
            AppendLine(text, "path_next=" + pathNext.Canonical(), final: true);
        }
        // Preserve existing movement/water digests. Only the new primitive
        // extends its semantic identity with selected item and actor pose.
        if (MenuStepCommandInbox.IsUseTool(request) && request.ToolTarget is { } tool)
        {
            text.Append('\n');
            AppendLine(text, "tool_slot=" + tool.Slot);
            AppendLine(text, "tool_item=" + B64(tool.QualifiedItemId));
            AppendLine(text, "tool_x=" + tool.TileX);
            AppendLine(text, "tool_y=" + tool.TileY);
            AppendLine(text, "tool_facing=" + tool.Facing, final: true);
        }
        if (MenuStepCommandInbox.IsSelectItem(request) && request.SelectionTarget is { } selection)
        {
            text.Append('\n');
            AppendLine(text, "select_receiver=" + B64(selection.ReceiverToken));
            AppendLine(text, "select_target=" + B64(selection.TargetToken));
            AppendLine(text, "select_slot=" + selection.Slot);
            AppendLine(text, "select_item=" + B64(selection.QualifiedItemId));
            AppendLine(text, "select_stack=" + selection.Stack);
            AppendLine(text, "select_quality=" + selection.Quality, final: true);
        }
        if (MenuStepCommandInbox.IsWorldInteraction(request) && request.InteractionTarget is { } interaction)
        {
            text.Append('\n');
            AppendLine(text,"interact_target="+B64(interaction.TargetToken));
            AppendLine(text,"interact_source="+B64(interaction.SourceFactId));
            AppendLine(text,"interact_x="+interaction.TileX);
            AppendLine(text,"interact_y="+interaction.TileY);
            AppendLine(text,"interact_actor_x="+interaction.ActorTileX);
            AppendLine(text,"interact_actor_y="+interaction.ActorTileY);
            AppendLine(text,"interact_facing="+interaction.Facing);
            // WS3a: the kind (and a door's expected destination) are part of the semantic identity.
            AppendLine(text,"interact_kind="+B64(interaction.Kind),final:interaction.ExpectedLocationId is null);
            if (interaction.ExpectedLocationId is { } expected)
                AppendLine(text,"interact_expected="+B64(expected),final:true);
        }
        if (MenuStepCommandInbox.IsMoveThrough(request) && request.ThroughTarget is { } through)
        {
            text.Append('\n');
            AppendLine(text,"through_kind="+B64(through.Kind));
            AppendLine(text,"through_source="+B64(through.SourceFactId));
            AppendLine(text,"through_target="+B64(through.TargetToken));
            AppendLine(text,"through_x="+through.TileX);
            AppendLine(text,"through_y="+through.TileY);
            AppendLine(text,"through_actor_x="+through.ActorTileX);
            AppendLine(text,"through_actor_y="+through.ActorTileY,final:through.ExpectedLocationId is null);
            if (through.ExpectedLocationId is { } expectedThrough)
                AppendLine(text,"through_expected="+B64(expectedThrough),final:true);
        }
        if (MenuStepCommandInbox.IsContainerTransfer(request) && request.TransferTarget is { } transfer)
        {
            text.Append('\n');
            AppendLine(text,"transfer_receiver="+B64(transfer.ReceiverToken));
            AppendLine(text,"transfer_container="+B64(transfer.ContainerToken));
            AppendLine(text,"transfer_readback="+B64(transfer.ReadbackToken));
            AppendLine(text,"transfer_source="+B64(transfer.SourceFactId));
            AppendLine(text,"transfer_target="+B64(transfer.TargetToken));
            AppendLine(text,"transfer_slot="+transfer.Slot);
            AppendLine(text,"transfer_item="+B64(transfer.QualifiedItemId));
            AppendLine(text,"transfer_stack="+transfer.Stack);
            AppendLine(text,"transfer_quality="+transfer.Quality);
            AppendLine(text,"transfer_ceiling="+transfer.OwnerCeiling,final:transfer.Quantity is null && transfer.Mode is null);
            if(transfer.Quantity is { } quantity)AppendLine(text,"transfer_quantity="+quantity,final:transfer.Mode is null);
            // WS-G2: a mode request (quick / take_out / ship_one / ship_stack) has its own digest line; v1 unchanged.
            if(transfer.Mode is { } mode)AppendLine(text,"transfer_mode="+B64(mode),final:true);
        }
        if (MenuStepCommandInbox.IsContainerExit(request) && request.ExitTarget is { } exit)
        {
            text.Append('\n');
            AppendLine(text,"exit_receiver="+B64(exit.ReceiverToken));
            AppendLine(text,"exit_container="+B64(exit.ContainerToken));
            AppendLine(text,"exit_source="+B64(exit.SourceFactId));
            AppendLine(text,"exit_readback="+B64(exit.ReadbackToken));
            AppendLine(text,"exit_target="+B64(exit.TargetToken),final:true);
        }
        if (MenuStepCommandInbox.IsPadButton(request) && request.PadButton is { } padButton)
        {
            // Primitives v2 WS-G0: the button is the whole semantic identity of a pad_button request.
            text.Append('\n');
            AppendLine(text, "padbutton_button=" + B64(padButton.Button), final: true);
        }
        if (MenuStepCommandInbox.IsChatSend(request) && request.ChatSend is { } chatSend)
        {
            // Primitives v2 WS-G3: the text is the whole semantic identity of a chat_send request.
            text.Append('\n');
            AppendLine(text, "chatsend_text=" + B64(chatSend.Text), final: true);
        }
        if (MenuStepCommandInbox.IsUiNav(request) && request.UiNav is { } uiNav)
        {
            // Primitives v2 WS-G1: the binding pins + direction are the semantic identity of a ui_nav.
            text.Append('\n');
            AppendLine(text, "uinav_receiver=" + B64(uiNav.ReceiverToken));
            AppendLine(text, "uinav_menu=" + B64(uiNav.MenuType));
            AppendLine(text, "uinav_context=" + B64(uiNav.Context));
            AppendLine(text, "uinav_target=" + B64(uiNav.TargetToken));
            AppendLine(text, "uinav_dir=" + B64(uiNav.Dir), final: true);
        }
        if (MenuStepCommandInbox.IsUiPress(request) && request.UiPress is { } uiPress)
        {
            text.Append('\n');
            AppendLine(text, "uipress_receiver=" + B64(uiPress.ReceiverToken));
            AppendLine(text, "uipress_menu=" + B64(uiPress.MenuType));
            AppendLine(text, "uipress_context=" + B64(uiPress.Context));
            AppendLine(text, "uipress_target=" + B64(uiPress.TargetToken));
            AppendLine(text, "uipress_btn=" + B64(uiPress.Button));
            AppendLine(text, "uipress_snapped=" + uiPress.SnappedId);
            AppendLine(text, "uipress_tab=" + B64(uiPress.Tab ?? ""));
            AppendLine(text, "uipress_reserved=" + B64(uiPress.ReservedCanonical), final: true);
        }
        if (MenuStepCommandInbox.IsReflexFish(request) && request.ReflexFish is { } reflex)
        {
            // Primitives v2 WS-G4: the reflex args are its semantic identity (cast power in per-mille, shared vector).
            text.Append('\n');
            AppendLine(text, "reflex_max_s=" + reflex.MaxSeconds);
            AppendLine(text, "reflex_cast_permille=" + (long)Math.Round(reflex.CastPower * 1000.0, MidpointRounding.AwayFromZero));
            AppendLine(text, "reflex_treasure=" + (reflex.Treasure ? 1 : 0));
            AppendLine(text, "reflex_hook_retries=" + reflex.HookRetries, final: true);
        }
        if (MenuStepCommandInbox.IsReflexCombat(request) && request.ReflexCombat is { } combat)
        {
            // Primitives v2 WS-G5: the reflex args are its semantic identity (shared vector with the Python digest).
            text.Append('\n');
            AppendLine(text, "reflex_max_s=" + combat.MaxSeconds);
            AppendLine(text, "reflex_hp_floor_pct=" + combat.HpFloorPct);
            AppendLine(text, "reflex_max_kills=" + combat.MaxKills);
            AppendLine(text, "reflex_retreat=" + (combat.Retreat ? 1 : 0), final: true);
        }
        if (MenuStepCommandInbox.IsReflexCharge(request) && request.ReflexCharge is { } charge)
        {
            text.Append('\n');
            AppendLine(text, "reflex_target_level=" + charge.Level);
            AppendLine(text, "reflex_max_ticks=" + charge.HoldBudgetTicks, final: true);
        }
        if (MenuStepCommandInbox.IsUiKey(request) && request.UiKeyTarget is { } uiKey)
        {
            text.Append('\n');
            AppendLine(text, "uikey_receiver=" + B64(uiKey.ReceiverToken));
            AppendLine(text, "uikey_menu=" + B64(uiKey.MenuType));
            AppendLine(text, "uikey_kind=" + B64(uiKey.Kind));
            AppendLine(text, "uikey_key=" + B64(uiKey.Key));
            AppendLine(text, "uikey_target=" + B64(uiKey.TargetToken));
            AppendLine(text, "uikey_option=" + (uiKey.OptionIndex ?? -1));
            AppendLine(text, "uikey_profession=" + (uiKey.ProfessionId ?? -1), final: true);
        }
        return Sha256Hex(Encoding.UTF8.GetBytes(text.ToString()));
    }

    public static string ExpectedRequestId(MenuStepCommandRequest request)
    {
        string digest = CanonicalPayloadDigest(request);
        StringBuilder text = new();
        AppendLine(text, CanonicalNamespaceText(request));
        AppendLine(text, "request_seq=" + request.RequestSeq);
        AppendLine(text, "payload_sha256=" + digest, final: true);
        return RequestIdPrefix + Sha256Hex(Encoding.UTF8.GetBytes(text.ToString()));
    }

    /// <summary>
    /// Inspect durable knowledge before any native admission. Any non-Fresh
    /// response is response-only and must never arm input.
    /// </summary>
    public CommandJournalLookup Lookup(MenuStepCommandRequest request)
    {
        lock (this.sync)
            return this.LookupUnsafe(request);
    }

    /// <summary>
    /// Durable pre-admission barrier. Call only after the ordinary current
    /// identity/lease/context/deadline gates have passed and immediately before
    /// the existing sole NativeActionOwner/native lifecycle may be armed.
    /// </summary>
    public bool TryBegin(
        MenuStepCommandRequest request,
        out CommandRejectReason reason)
    {
        lock (this.sync)
        {
            reason = CommandRejectReason.None;
            this.DrainDeferredUnsafe(); // fix55D: a deferred-writer fault may heal here, before lookup and any change
            if (this.snapshot.Scope is { } scope && (request.RuntimeInstanceId != scope.RuntimeInstanceId
                || request.SessionId != scope.SessionId || request.ExpectedPlayerId != scope.PlayerId
                || request.ExpectedScreenId != scope.ScreenId))
            {
                reason = CommandRejectReason.JournalIdentityMismatch;
                return false;
            }
            CommandJournalLookup lookup = this.LookupUnsafe(request);
            if (lookup.Kind != CommandJournalLookupKind.Fresh)
            {
                reason = RejectReasonFor(lookup.Kind);
                return false;
            }

            // ADR 48: only between native transactions, before mutating admission state.
            if (!this.TryRotateUnsafe())
            {
                reason = CommandRejectReason.JournalPersistenceFault;
                return false;
            }

            string key = NamespaceKey(request);

            // Current request already passed authoritative runtime/session/
            // owner/generation lease gates. Settled-only stale namespaces may
            // be retired at that authority transition, but outstanding or
            // uncertain rows are never evicted to make room.
            this.RetireSettledInvalidNamespacesUnsafe(request, key);

            if (this.snapshot.Rows.Count(static row => row.State != JournalRowState.Settled)
                >= this.maxUnresolvedRows)
            {
                reason = CommandRejectReason.JournalCapacityReached;
                return false;
            }

            if (this.snapshot.Rows.Any(row => row.NamespaceKey == key && row.State != JournalRowState.Settled
                && (request.VerbEnvelope is null || row.Terminal is null)))
            {
                reason = CommandRejectReason.JournalUncertain;
                return false;
            }

            if (!this.snapshot.Namespaces.TryGetValue(key, out JournalNamespace? ns))
            {
                ns = NewNamespace(request, key);
                this.snapshot.Namespaces[key] = ns;
            }
            if (request.RequestSeq <= ns.HighestRequestSeq)
            {
                reason = CommandRejectReason.JournalSequenceConflict;
                return false;
            }

            ns.HighestRequestSeq = request.RequestSeq;
            this.changes.Namespaces[key] = ns;
            string watermarkKey = NamespaceKey(request with { Generation = 0 });
            Bump(this.snapshot.Watermarks, watermarkKey, request.Generation);
            var admitted = new JournalRow
            {
                NamespaceKey = key,
                RequestSeq = request.RequestSeq,
                RequestId = request.RequestId,
                PayloadSha256 = CanonicalPayloadDigest(request),
                Op = request.Op,
                State = JournalRowState.Admitted,
                ResultJson = null,
                ControlSeq = request.ControlSeq,
                ContextGeneration = request.ContextGeneration,
                Ordinal = ++this.snapshot.LastOrdinal,
                TargetKey = TargetKeyOf(request),
            };
            this.snapshot.Rows.Add(admitted);
            this.ChangeRow(admitted);

            if (this.TryPersistUnsafe("admit"))
                return true;

            // No native admission follows a failed durable write. Keeping this
            // process faulted is conservative; a restart may recover only from
            // a validated primary/mirror transaction chain.
            reason = CommandRejectReason.JournalPersistenceFault;
            return false;
        }
    }

    /// <summary>
    /// Persist native release separately from the possibly delayed result.
    /// </summary>
    internal bool TryRecordTerminal(MenuStepCommandRequest request, AttemptTerminal terminal, out CommandRejectReason reason)
    {
        lock (this.sync)
        {
            reason = CommandRejectReason.JournalPersistenceFault;
            this.PollDeferredUnsafe(heal: true);
            var row = this.snapshot.Rows.FirstOrDefault(r => r.NamespaceKey == NamespaceKey(request)
                && r.RequestSeq == request.RequestSeq && r.RequestId == request.RequestId
                && r.PayloadSha256 == CanonicalPayloadDigest(request));
            if (row is null && !this.faulted && this.RetiredRowUnsafe(request) is { Terminal: not null })
            {
                reason = CommandRejectReason.None; // fix10e: retired only after this receipt was durable
                return true;
            }
            if (row is null || this.faulted && (row.Terminal is null || terminal.Client != "progression")) return false;
            row.Terminal = terminal;
            this.ChangeRow(row);
            if (terminal.Client == "progression") {
                row.State = JournalRowState.Settled; this.AdvanceSettledFenceUnsafe(row.NamespaceKey); this.CompactUnsafe();
            }
            else if (row.State == JournalRowState.Uncertain) this.EnforceReleasedBoundUnsafe();
            if (!this.TryPersistUnsafe("terminal")) return false;
            reason = CommandRejectReason.None;
            return true;
        }
    }

    /// <summary>Persist terminal/uncertain result state before result.json publication.</summary>
    public bool TryFinalize(
        MenuStepCommandRequest request,
        MenuStepCommandResult result,
        bool uncertain,
        out CommandRejectReason reason)
    {
        lock (this.sync)
        {
            reason = CommandRejectReason.None;
            if (!IsJournalRequest(request))
                return true;
            this.DrainDeferredUnsafe(); // fix54I: result publication never precedes a queued release receipt
            if (this.faulted)
            {
                reason = CommandRejectReason.JournalPersistenceFault;
                return false;
            }

            string key = NamespaceKey(request);
            string digest = CanonicalPayloadDigest(request);
            JournalRow? row = this.snapshot.Rows.FirstOrDefault(
                candidate => string.Equals(candidate.NamespaceKey, key, StringComparison.Ordinal)
                    && candidate.RequestSeq == request.RequestSeq);
            // fix10e: a later finalize of a retired row (e.g. a late readback) keeps the recorded retirement.
            if (row is null && this.RetiredRowUnsafe(request) is { } retired && retired.PayloadSha256 == digest)
                return true;
            if (row is null
                || !string.Equals(row.RequestId, request.RequestId, StringComparison.Ordinal)
                || !string.Equals(row.PayloadSha256, digest, StringComparison.Ordinal))
            {
                this.faulted = true;
                reason = CommandRejectReason.JournalPersistenceFault;
                return false;
            }

            row.State = uncertain ? JournalRowState.Uncertain : JournalRowState.Settled;
            if (row.Terminal is { } terminal) result = result with { ReleaseReceipt = result.ReleaseReceipt ?? terminal.Receipt,
                Closure = result.Closure ?? terminal.Closure };
            row.ResultJson = JsonSerializer.Serialize(result, JsonOptions);
            row.Ordinal = ++this.snapshot.LastOrdinal;
            this.ChangeRow(row);

            if (!uncertain)
            {
                this.AdvanceSettledFenceUnsafe(key);
                this.RetireSupersededUnsafe(row);
                this.CompactUnsafe();
            }
            else this.EnforceReleasedBoundUnsafe();

            if (this.TryPersistUnsafe("finalize"))
                return true;

            // The last trustworthy disk fact is still at least the durable
            // admitted row. Never publish a stronger completed claim ahead of
            // the journal transition.
            row.State = JournalRowState.Uncertain;
            reason = CommandRejectReason.JournalPersistenceFault;
            return false;
        }
    }

    /// <summary>
    /// This boundary is called together with command-session rotation. The old
    /// session is no longer admissible, so its journal can be retired without
    /// reopening a valid native admission path.
    /// </summary>
    public void ResetForNewSession()
    {
        lock (this.sync)
        {
            this.ArchiveAndResetUnsafe(null, nativeSessionReset: true);
        }
    }

    /// <summary>
    /// Native post-release readback can settle a partial cardinal move, never its objective.
    /// Frame receipts, stable stopped captures and exact latch restoration prove termination.
    /// No inference from an idle farmer, elapsed time, or a different command is sufficient.
    /// </summary>
    public MenuStepCommandResult? ResolveMovementReadback(MenuStepCommandRequest request,
        BgMoveCompletionFacts facts, CommandInboxIdentity identity, long completedUnixMs)
    {
        lock (this.sync)
        {
            CommandJournalLookup lookup = this.LookupUnsafe(request);
            MenuStepCommandResult? result = lookup.ResultJson is { Length: > 0 } json
                ? JsonSerializer.Deserialize<MenuStepCommandResult>(json, JsonOptions) : null;
            if (lookup.Kind != CommandJournalLookupKind.OutstandingUncertain || result is null
                || this.snapshot.Scope is { } scope && scope != new JournalScope(identity.RuntimeInstanceId,
                    identity.SessionId, identity.SaveId, identity.PlayerId, identity.ScreenId)
                || request.Op != MenuStepCommandInbox.OpInputOnce
                || request.Family != MenuStepCommandInbox.FamilyWorldKey
                || request.Primitive != MenuStepCommandInbox.PrimitiveMoveCardinal || request.EventToken is not null
                || request.RuntimeInstanceId != identity.RuntimeInstanceId || request.SessionId != identity.SessionId
                || request.ExpectedPlayerId != identity.PlayerId || request.ExpectedScreenId != identity.ScreenId
                || request.ExpectedLocationId != identity.LocationId || request.ContextGeneration != identity.ContextGeneration
                || facts.TargetHFrames != request.Ticks || facts.ConsumedHFrames < 0 || facts.ConsumedHFrames > request.Ticks
                || !double.IsFinite(facts.PixelDisplacement) || facts.PixelDisplacement < 0
                || facts.ReleaseReason is not ("DEADLINE" or "LEASE_INVALID")
                || result.RejectReason != "NOT_COMPLETED:RELEASED_EARLY:" + facts.ReleaseReason
                || BgMoveCompletion.Evaluate(facts) != result.RejectReason)
                return null;

            MenuStepCommandResult resolved = result with
            {
                Status = MenuStepCommandResult.StatusInterrupted, Success = false,
                CleanupConfirmed = true, InputSettled = true, PressCount = facts.ConsumedHFrames,
                WorldEffectStatus = "unverified", CompletedUnixMs = completedUnixMs, JournalState = "settled",
            };
            return this.TryFinalize(request, resolved, uncertain: false, out _) ? resolved : null;
        }
    }

    public MenuStepCommandResult? BuildResponse(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity,
        long completedUnixMs)
    {
        lock (this.sync)
        {
            CommandJournalLookup lookup = this.LookupUnsafe(request);
            if ((lookup.Kind == CommandJournalLookupKind.CachedSettled
                    || lookup.Kind == CommandJournalLookupKind.OutstandingUncertain)
                && lookup.ResultJson is { Length: > 0 } cachedJson)
            {
                try
                {
                    MenuStepCommandResult? cached = JsonSerializer.Deserialize<MenuStepCommandResult>(
                        cachedJson, JsonOptions);
                    if (cached is not null)
                    {
                        return cached with
                        {
                            JournalRecovered = true,
                            JournalState = lookup.Kind == CommandJournalLookupKind.CachedSettled
                                ? "settled"
                                : cached.JournalState == "retired" ? "retired" : "uncertain",
                            RequestSeq = request.RequestSeq,
                        };
                    }
                }
                catch (JsonException)
                {
                    this.faulted = true;
                    return FaultResult(request, identity, completedUnixMs);
                }
            }

            // Durable admitted with no terminal record: after restart the only
            // honest answer is uncertainty and zero replay.
            if (lookup.Kind == CommandJournalLookupKind.OutstandingUncertain)
            {
                return new MenuStepCommandResult
                {
                    RequestId = request.RequestId,
                    SessionId = request.SessionId,
                    Op = request.Op,
                    Accepted = true,
                    Status = MenuStepCommandResult.StatusUncertain,
                    RejectReason = "JournalOutstandingUncertain",
                    Success = false,
                    CompletedUnixMs = completedUnixMs,
                    RequestSeq = request.RequestSeq,
                    JournalRecovered = true,
                    JournalState = "uncertain",
                };
            }

            if (lookup.Kind == CommandJournalLookupKind.SettledCompacted)
            {
                return new MenuStepCommandResult
                {
                    RequestId = request.RequestId,
                    SessionId = request.SessionId,
                    Op = request.Op,
                    Accepted = false,
                    Status = MenuStepCommandResult.StatusRejected,
                    RejectReason = nameof(CommandRejectReason.JournalSettledCompacted),
                    Success = false,
                    CompletedUnixMs = completedUnixMs,
                    RequestSeq = request.RequestSeq,
                    JournalRecovered = true,
                    JournalState = "compacted",
                };
            }

            if (lookup.Kind == CommandJournalLookupKind.Faulted)
                return FaultResult(request, identity, completedUnixMs);
            return null;
        }
    }

    private static MenuStepCommandResult FaultResult(
        MenuStepCommandRequest request,
        CommandInboxIdentity identity,
        long completedUnixMs) => new()
    {
        RequestId = request.RequestId,
        SessionId = identity.SessionId,
        Op = request.Op,
        Accepted = false,
        Status = MenuStepCommandResult.StatusRejected,
        RejectReason = CommandRejectReason.JournalPersistenceFault.ToString(),
        Success = false,
        CompletedUnixMs = completedUnixMs,
        RequestSeq = request.RequestSeq,
        JournalRecovered = true,
        JournalState = "faulted",
    };

    private CommandJournalLookup LookupUnsafe(MenuStepCommandRequest request)
    {
        if (!IsJournalRequest(request))
            return CommandJournalLookup.Fresh();
        if (this.faulted)
            return CommandJournalLookup.Fault();
        if (request.RequestSeq < 1
            || request.VerbEnvelope is null && (!request.RequestId.StartsWith(RequestIdPrefix, StringComparison.Ordinal)
            || !string.Equals(
                request.RequestId,
                ExpectedRequestId(request),
                StringComparison.Ordinal)))
        {
            return CommandJournalLookup.IdentityMismatch();
        }

        return this.LookupIdentityUnsafe(request, CanonicalPayloadDigest(request));
    }

    private CommandJournalLookup LookupIdentityUnsafe(MenuStepCommandRequest request, string digest, bool statusQuery = false)
    {
        if (this.faulted) return CommandJournalLookup.Fault();
        string key = NamespaceKey(request);
        JournalRow? row = this.snapshot.Rows.Concat(this.snapshot.Tombstones).FirstOrDefault(
            candidate => string.Equals(candidate.NamespaceKey, key, StringComparison.Ordinal)
                && candidate.RequestSeq == request.RequestSeq);
        if (row is not null)
        {
            if (!string.Equals(row.RequestId, request.RequestId, StringComparison.Ordinal)
                || !string.Equals(row.PayloadSha256, digest, StringComparison.Ordinal))
            {
                return CommandJournalLookup.IdentityMismatch();
            }
            return row.State switch
            {
                JournalRowState.Settled => CommandJournalLookup.Cached(row.ResultJson),
                JournalRowState.Admitted or JournalRowState.Uncertain =>
                    CommandJournalLookup.Uncertain(row.ResultJson),
                // fix10e: a retired row answers its recorded uncertain result and is never admissible again.
                JournalRowState.Retired => CommandJournalLookup.Uncertain(row.ResultJson),
                _ => CommandJournalLookup.Fault(),
            };
        }

        if (statusQuery && request.Generation <= this.snapshot.ArchivedWatermarks.GetValueOrDefault(NamespaceKey(request with { Generation = 0 })))
            return new(CommandJournalLookupKind.Archived);
        if (!this.snapshot.Namespaces.TryGetValue(key, out JournalNamespace? ns))
            return request.Generation <= this.snapshot.Watermarks.GetValueOrDefault(NamespaceKey(request with { Generation = 0 }))
                ? CommandJournalLookup.Compacted() : CommandJournalLookup.Fresh();
        if (request.RequestSeq <= ns.SettledThroughSeq)
            return CommandJournalLookup.Compacted();
        if (request.RequestSeq <= ns.HighestRequestSeq)
            return CommandJournalLookup.SequenceConflict();
        return CommandJournalLookup.Fresh();
    }

    public JsonElement AttemptStatus(JsonElement query, long nowMs)
    {
        lock (this.sync)
        {
            var request = new MenuStepCommandRequest(query.GetProperty("request_id").GetString()!, query.GetProperty("session_id").GetString()!,
                0, null, "", 0, 0, 0, RuntimeInstanceId: query.GetProperty("runtime_instance_id").GetString()!,
                OwnerId: query.GetProperty("owner_id").GetString()!, Generation: query.GetProperty("generation").GetInt64(),
                RequestSeq: query.GetProperty("request_seq").GetInt64());
            var lookup = this.LookupIdentityUnsafe(request, query.GetProperty("payload_sha256").GetString()!, statusQuery: true);
            string key = NamespaceKey(request);
            var row = this.snapshot.Rows.FirstOrDefault(r => r.NamespaceKey == key && r.RequestSeq == request.RequestSeq);
            string source = row is not null ? "live_row" : this.snapshot.Tombstones.Any(r => r.NamespaceKey == key && r.RequestSeq == request.RequestSeq) ? "tombstone"
                : lookup.Kind == CommandJournalLookupKind.SettledCompacted ? "watermark" : "none";
            string status = lookup.Kind switch {
                CommandJournalLookupKind.CachedSettled => "Settled", CommandJournalLookupKind.OutstandingUncertain => row?.State == JournalRowState.Admitted ? "Admitted"
                    : row is null && this.RetiredRowUnsafe(request) is not null ? "Retired" : "Uncertain",
                CommandJournalLookupKind.SettledCompacted => "Compacted", CommandJournalLookupKind.Faulted => "Fault", _ => lookup.Kind.ToString() };
            if (status == "Fresh" && request.Generation <= this.snapshot.Watermarks.GetValueOrDefault(NamespaceKey(request with { Generation = 0 })))
            { status = "Compacted"; source = "watermark"; }
            var answer = new Dictionary<string, object?> { ["query_id"] = query.GetProperty("query_id").GetString(), ["request_id"] = request.RequestId,
                ["answered_ms"] = nowMs, ["status"] = status, ["source"] = source };
            if (status == "Settled" && lookup.ResultJson is { } json)
            {
                var result = JsonSerializer.Deserialize<MenuStepCommandResult>(json, JsonOptions)!;
                answer["result"] = JsonSerializer.Deserialize<JsonElement>(AdmissionRefusals.Serialize(result));
            }
            return JsonSerializer.SerializeToElement(answer);
        }
    }

    private static CommandRejectReason RejectReasonFor(CommandJournalLookupKind kind) => kind switch
    {
        CommandJournalLookupKind.CachedSettled => CommandRejectReason.JournalDuplicateSettled,
        CommandJournalLookupKind.OutstandingUncertain => CommandRejectReason.JournalUncertain,
        CommandJournalLookupKind.SettledCompacted => CommandRejectReason.JournalSettledCompacted,
        CommandJournalLookupKind.IdentityMismatch => CommandRejectReason.JournalIdentityMismatch,
        CommandJournalLookupKind.SequenceConflict => CommandRejectReason.JournalSequenceConflict,
        CommandJournalLookupKind.Faulted => CommandRejectReason.JournalPersistenceFault,
        CommandJournalLookupKind.Archived => CommandRejectReason.JournalSettledCompacted,
        _ => CommandRejectReason.None,
    };

    private void AdvanceSettledFenceUnsafe(string key)
    {
        if (!this.snapshot.Namespaces.TryGetValue(key, out JournalNamespace? ns))
            return;

        long candidate = ns.SettledThroughSeq;
        foreach (JournalRow row in this.snapshot.Rows
                     .Where(row => string.Equals(row.NamespaceKey, key, StringComparison.Ordinal))
                     .OrderBy(row => row.RequestSeq))
        {
            if (row.RequestSeq <= candidate)
                continue;
            if (row.State is not (JournalRowState.Settled or JournalRowState.Retired))
                break;
            candidate = row.RequestSeq;
        }
        ns.SettledThroughSeq = candidate;
        this.changes.Namespaces[key] = ns;
    }

    private void CompactUnsafe()
    {
        int settledCount = this.snapshot.Rows.Count(static row => row.State == JournalRowState.Settled);
        if (settledCount <= this.maxExactSettledRows)
            return;

        int removeCount = settledCount - this.maxExactSettledRows;
        List<JournalRow> removable = this.snapshot.Rows
            .Where(row => row.State == JournalRowState.Settled
                && this.snapshot.Namespaces.TryGetValue(row.NamespaceKey, out JournalNamespace? ns)
                && row.RequestSeq <= ns.SettledThroughSeq)
            .OrderBy(row => row.Ordinal)
            .Take(removeCount)
            .ToList();
        foreach (JournalRow row in removable)
            this.RetireRowUnsafe(row);
    }

    private JournalRow? RetiredRowUnsafe(MenuStepCommandRequest request)
    {
        string key = NamespaceKey(request);
        return this.snapshot.Tombstones.LastOrDefault(row => row.State == JournalRowState.Retired
            && row.NamespaceKey == key && row.RequestSeq == request.RequestSeq && row.RequestId == request.RequestId);
    }

    private void RetireRowUnsafe(JournalRow row)
    {
        this.snapshot.Tombstones.Add(row);
        this.snapshot.Tombstones.Sort((a, b) => a.Ordinal.CompareTo(b.Ordinal));
        this.snapshot.Rows.Remove(row);
        this.ChangeRow(row, tombstone: true);
        if (this.snapshot.Tombstones.Count > MaxTombstones)
        {
            JournalRow removed = this.snapshot.Tombstones[0];
            this.snapshot.Tombstones.RemoveAt(0);
            this.changes.Rows[RowKey(removed)] = new RowChange(removed.NamespaceKey, removed.RequestSeq, null, false);
        }
    }

    private void RetireSettledInvalidNamespacesUnsafe(
        MenuStepCommandRequest current,
        string currentKey)
    {
        List<string> stale = this.snapshot.Namespaces.Values
            .Where(ns => !string.Equals(ns.NamespaceKey, currentKey, StringComparison.Ordinal)
                && !this.snapshot.Rows.Any(
                    row => string.Equals(row.NamespaceKey, ns.NamespaceKey, StringComparison.Ordinal)
                        && row.State != JournalRowState.Settled)
                && (!string.Equals(ns.RuntimeInstanceId, current.RuntimeInstanceId, StringComparison.Ordinal)
                    || !string.Equals(ns.SessionId, current.SessionId, StringComparison.Ordinal)
                    // Owners are independent namespaces (core and native progression coexist).
                    // Only a newer generation of the SAME owner invalidates its old authority.
                    || string.Equals(ns.OwnerId, current.OwnerId, StringComparison.Ordinal)
                        && ns.Generation < current.Generation))
            .Select(ns => ns.NamespaceKey)
            .ToList();
        if (stale.Count == 0)
            return;

        HashSet<string> set = new(stale, StringComparer.Ordinal);
        foreach (var row in this.snapshot.Rows.Where(row => set.Contains(row.NamespaceKey)).ToArray()) this.RetireRowUnsafe(row);
        foreach (string key in stale)
        {
            this.snapshot.Namespaces.Remove(key);
            this.changes.Namespaces[key] = null;
        }
    }

    private static JournalNamespace NewNamespace(
        MenuStepCommandRequest request,
        string key) => new()
    {
        NamespaceKey = key,
        RuntimeInstanceId = request.RuntimeInstanceId,
        SessionId = request.SessionId,
        OwnerId = request.OwnerId,
        Generation = request.Generation,
        HighestRequestSeq = 0,
        SettledThroughSeq = 0,
    };

    private string SerializeSnapshotUnsafe()
    {
        byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(this.snapshot, JsonOptions);
        return JsonSerializer.Serialize(new JournalEnvelope
        {
            SchemaVersion = SchemaVersion, PayloadBase64 = Convert.ToBase64String(bytes), Sha256 = Sha256Hex(bytes),
        }, JsonOptions);
    }

    private static string NamespaceKey(MenuStepCommandRequest request) =>
        Sha256Hex(Encoding.UTF8.GetBytes(CanonicalNamespaceText(request)));

    private static string CanonicalNamespaceText(MenuStepCommandRequest request)
    {
        StringBuilder text = new();
        AppendLine(text, "journal-namespace-v1");
        AppendLine(text, "runtime=" + B64(request.RuntimeInstanceId));
        AppendLine(text, "session=" + B64(request.SessionId));
        AppendLine(text, "owner=" + B64(request.OwnerId));
        AppendLine(text, "generation=" + request.Generation, final: true);
        return text.ToString();
    }

    private static string B64(string value) =>
        Convert.ToBase64String(Encoding.UTF8.GetBytes(value ?? ""));

    private static void AppendLine(StringBuilder target, string value, bool final = false)
    {
        target.Append(value);
        if (!final)
            target.Append('\n');
    }

    private static string Sha256Hex(byte[] payload) =>
        Convert.ToHexString(SHA256.HashData(payload)).ToLowerInvariant();

    private sealed record JournalEnvelope
    {
        public int SchemaVersion { get; init; }
        public string PayloadBase64 { get; init; } = "";
        public string Sha256 { get; init; } = "";
    }

    private sealed record JournalSnapshot
    {
        public int SchemaVersion { get; init; } = CommandSettledJournal.SchemaVersion;
        public Dictionary<string, JournalNamespace> Namespaces { get; set; } =
            new(StringComparer.Ordinal);
        public List<JournalRow> Rows { get; set; } = new();
        public List<JournalRow> Tombstones { get; set; } = new();
        public Dictionary<string, long> Watermarks { get; set; } = new(StringComparer.Ordinal);
        public Dictionary<string, long> ArchivedWatermarks { get; set; } = new(StringComparer.Ordinal);
        public long LastOrdinal { get; set; }
        public JournalScope? Scope { get; set; }
        public Dictionary<string, long> RetiredCounts { get; set; } = new(StringComparer.Ordinal);

        public static JournalSnapshot Empty() => new();
    }

    private sealed record JournalScope(string RuntimeInstanceId, string SessionId, string SaveId, long PlayerId, int ScreenId);

    private sealed record JournalNamespace
    {
        public string NamespaceKey { get; init; } = "";
        public string RuntimeInstanceId { get; init; } = "";
        public string SessionId { get; init; } = "";
        public string OwnerId { get; init; } = "";
        public long Generation { get; init; }
        public long HighestRequestSeq { get; set; }
        public long SettledThroughSeq { get; set; }
    }

    private sealed record JournalRow
    {
        public string NamespaceKey { get; init; } = "";
        public long RequestSeq { get; init; }
        public string RequestId { get; init; } = "";
        public string PayloadSha256 { get; init; } = "";
        public string Op { get; init; } = "";
        public JournalRowState State { get; set; }
        public string? ResultJson { get; set; }
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public AttemptTerminal? Terminal { get; set; }
        public long ControlSeq { get; init; }
        public long ContextGeneration { get; init; }
        public long Ordinal { get; set; }
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? TargetKey { get; init; }
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? RetiredReason { get; set; }
    }
}

public enum JournalRowState
{
    Admitted,
    Settled,
    Uncertain,
    /// <summary>fix10e: was Uncertain with an input-idle release receipt; retired with a recorded reason.</summary>
    Retired,
}

public enum CommandJournalLookupKind
{
    Archived,
    Fresh,
    CachedSettled,
    OutstandingUncertain,
    SettledCompacted,
    IdentityMismatch,
    SequenceConflict,
    Faulted,
}

public readonly record struct CommandJournalLookup(
    CommandJournalLookupKind Kind,
    string? ResultJson = null)
{
    public static CommandJournalLookup Fresh() => new(CommandJournalLookupKind.Fresh);
    public static CommandJournalLookup Cached(string? resultJson) =>
        new(CommandJournalLookupKind.CachedSettled, resultJson);
    public static CommandJournalLookup Uncertain(string? resultJson) =>
        new(CommandJournalLookupKind.OutstandingUncertain, resultJson);
    public static CommandJournalLookup Compacted() =>
        new(CommandJournalLookupKind.SettledCompacted);
    public static CommandJournalLookup IdentityMismatch() =>
        new(CommandJournalLookupKind.IdentityMismatch);
    public static CommandJournalLookup SequenceConflict() =>
        new(CommandJournalLookupKind.SequenceConflict);
    public static CommandJournalLookup Fault() => new(CommandJournalLookupKind.Faulted);
}
