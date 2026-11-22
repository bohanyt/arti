using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// ADR 37: a durability barrier writes one changed transaction, never session history.
public sealed partial class CommandSettledJournal
{
    public const int StorageSchemaVersion = 2;
    internal const int PersistAttempts = 3;
    internal const int PersistRetryDelayMs = 15;
    internal Action<int>? BeforePersistAttemptForTests { get; set; }

    private JournalDelta changes = new();
    private bool checkpointRequired = true;
    private bool primaryNeedsNewline, backupNeedsNewline;
    private bool offsetsKnown;
    private long storageSequence, primaryLength, backupLength;
    private string? storageHash;

    private sealed record RowChange(string NamespaceKey, long RequestSeq, JournalRow? Row, bool Tombstone);
    private sealed record JournalDelta
    {
        public JournalSnapshot? Checkpoint { get; init; }
        public bool Reset { get; init; }
        public JournalScope? Scope { get; set; }
        public long LastOrdinal { get; set; }
        public Dictionary<string, RowChange> Rows { get; init; } = new(StringComparer.Ordinal);
        public Dictionary<string, JournalNamespace?> Namespaces { get; init; } = new(StringComparer.Ordinal);
        public Dictionary<string, long> Watermarks { get; init; } = new(StringComparer.Ordinal);
        public Dictionary<string, long> ArchivedWatermarks { get; init; } = new(StringComparer.Ordinal);
        public Dictionary<string, long> RetiredCounts { get; set; } = new(StringComparer.Ordinal);
    }
    private sealed record StorageRecord
    {
        public int SchemaVersion { get; init; } = StorageSchemaVersion;
        public long Sequence { get; init; }
        public string? PreviousSha256 { get; init; }
        public string PayloadBase64 { get; init; } = "";
        public int PayloadLength { get; init; }
        public string Sha256 { get; init; } = "";
    }
    private sealed record LoadedJournal(JournalSnapshot Snapshot, long Sequence, string? Hash, long Length, bool Legacy);
    private static string RowKey(JournalRow row) => row.NamespaceKey + ":" + row.RequestSeq;
    private void ChangeRow(JournalRow row, bool tombstone = false) =>
        this.changes.Rows[RowKey(row)] = new RowChange(row.NamespaceKey, row.RequestSeq, row, tombstone);

    private void Load()
    {
        lock (this.sync)
        {
            this.SelectSegmentUnsafe();
            bool primaryExists = File.Exists(this.activePath), backupExists = File.Exists(this.ActiveBackupPath);
            if (!primaryExists && !backupExists) { this.offsetsKnown = true; return; }
            LoadedJournal? primary = ReadJournal(this.activePath, out long primaryObserved, out bool primaryComplete),
                backup = ReadJournal(this.ActiveBackupPath, out long backupObserved, out bool backupComplete);
            this.InitializeOffsets(primary, backup);
            this.storageSequence = Math.Max(primaryObserved, backupObserved);
            if (!primaryComplete || !backupComplete) { this.offsetsKnown = false; this.faulted = true; return; }
            if (primary is null && backup is null) { this.faulted = true; return; }
            if (primary is not null && backup is not null && primary.Sequence > 0 && primary.Sequence == backup.Sequence
                && primary.Hash != backup.Hash) { this.faulted = true; return; } // Never guess between two forks.
            LoadedJournal loaded = primary is null || backup is not null && backup.Sequence > primary.Sequence ? backup! : primary!;
            if (this.segmentHead is { } selected && loaded.Sequence < selected.Generation)
            { this.faulted = true; return; } // A published checkpoint cannot be treated as a never-admitted torn first record.
            this.snapshot = loaded.Snapshot;
            this.storageSequence = Math.Max(this.storageSequence, loaded.Sequence);
            this.storageHash = loaded.Hash;
            this.checkpointRequired = loaded.Legacy || loaded.Sequence == 0 || this.storageSequence != loaded.Sequence
                || primary?.Hash != loaded.Hash || backup?.Hash != loaded.Hash;
            // A validated prefix may end before a torn tail. An interior-corrupt copy is preserved;
            // an appended recovery checkpoint repairs it without replacing any historical bytes.
            foreach (var ns in this.snapshot.Namespaces.Values)
            {
                var request = new MenuStepCommandRequest("", ns.SessionId, 0, null, "", 0, 0, 0,
                    RuntimeInstanceId: ns.RuntimeInstanceId, OwnerId: ns.OwnerId);
                this.Bump(this.snapshot.Watermarks, NamespaceKey(request), ns.Generation);
            }
            bool recovered = false;
            foreach (JournalRow row in this.snapshot.Rows)
            {
                if (row.State != JournalRowState.Admitted) continue;
                row.State = JournalRowState.Uncertain; row.ResultJson = null;
                row.Ordinal = ++this.snapshot.LastOrdinal;
                this.ChangeRow(row); recovered = true;
            }
            if ((this.checkpointRequired || recovered) && !this.TryPersistUnsafe("recover")) this.faulted = true;
            if (!this.faulted && this.segmentHead is { } head)
            {
                this.PublishHead(head); // Repair a crash between head publications before allowing appends/pruning.
                this.PruneSegmentsUnsafe();
            }
            if (!this.faulted) this.TryRotateUnsafe(startup: true);
        }
    }

    private bool TryPersistUnsafe(string phase = "persist")
    {
        // fix54I: only a terminal record may leave the game thread; every other persist is an ordering barrier.
        // fix55D: never heal here (the caller already changed state) and never append while the writer owns the copies.
        this.PollDeferredUnsafe();
        bool defer = this.CanDeferUnsafe(phase);
        if (!defer && !this.DrainDeferredUnsafe(heal: false)) return false;
        long start = this.WriteTimings.Begin(defer ? "terminal_enqueue" : phase);
        bool succeeded = false;
        try { return succeeded = this.TryPersistCoreUnsafe(defer); }
        finally { this.WriteTimings.End(start, succeeded); }
    }

    private bool TryPersistCoreUnsafe(bool defer = false)
    {
        if (this.rotationFaulted) return false; // Never extend a retired chain after a partially published head.
        if (!this.offsetsKnown && this.deferredLost)
        {
            // fix55D: after a lost deferred record only the verified loss point is a valid place to append.
            if (!this.VerifyDeferredLossUnsafe()) { this.deferredFaultReason = FaultDeferredHealUnverified; return false; }
            this.checkpointRequired = true;
        }
        if (!this.offsetsKnown)
        {
            try
            {
                var primary = ReadJournal(this.activePath, out long primaryObserved, out bool primaryComplete);
                var backup = ReadJournal(this.ActiveBackupPath, out long backupObserved, out bool backupComplete);
                if (!primaryComplete || !backupComplete) { this.faulted = true; return false; }
                this.storageSequence = Math.Max(this.storageSequence, Math.Max(primaryObserved, backupObserved));
                this.InitializeOffsets(primary, backup);
                this.checkpointRequired = true;
            }
            catch { this.faulted = true; return false; }
        }
        // Serialize exactly once, including on a bounded retry. Old watermark dictionaries and
        // unchanged full readbacks never participate in per-action serialization or disk writes.
        JournalDelta delta = this.checkpointRequired ? new JournalDelta { Checkpoint = this.snapshot } : this.changes;
        delta.Scope = this.snapshot.Scope;
        delta.LastOrdinal = this.snapshot.LastOrdinal;
        byte[] payload, wire;
        string hash;
        string? previous;
        long sequence;
        try
        {
            sequence = checked(this.storageSequence + 1);
            payload = JsonSerializer.SerializeToUtf8Bytes(delta, JsonOptions);
            previous = this.checkpointRequired ? null : this.storageHash;
            hash = RecordHash(sequence, previous, payload);
            wire = JsonSerializer.SerializeToUtf8Bytes(new StorageRecord {
                Sequence = sequence, PreviousSha256 = previous, PayloadBase64 = Convert.ToBase64String(payload), Sha256 = hash,
                PayloadLength = payload.Length,
            }, JsonOptions).Concat(new byte[] { (byte)'\n' }).ToArray();
            this.WriteTimings.Record(sequence, wire.LongLength);
        }
        catch { this.faulted = true; return false; }
        if (defer)
        {
            // The chain position and offsets are fixed here; the writer appends exactly these bytes there.
            this.EnqueueDeferredUnsafe(wire, sequence, hash, previous);
            this.primaryLength += wire.Length + (this.primaryNeedsNewline ? 1 : 0);
            this.backupLength += wire.Length + (this.backupNeedsNewline ? 1 : 0);
            this.primaryNeedsNewline = this.backupNeedsNewline = false;
            this.storageSequence = sequence; this.storageHash = hash;
            this.changes = new JournalDelta();
            return true;
        }
        for (int attempt = 1; ; attempt++)
        {
            try
            {
                this.BeforePersistAttemptForTests?.Invoke(attempt);
                string? directory = System.IO.Path.GetDirectoryName(this.activePath);
                if (!string.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);
                // Checkpoints, legacy migration and native resets append too. No active file is replaced.
                AtomicFileWriter.AppendDurable(this.ActiveBackupPath, this.backupLength, wire, this.backupNeedsNewline);
                AtomicFileWriter.AppendDurable(this.activePath, this.primaryLength, wire, this.primaryNeedsNewline);
                this.primaryLength += wire.Length + (this.primaryNeedsNewline ? 1 : 0);
                this.backupLength += wire.Length + (this.backupNeedsNewline ? 1 : 0);
                this.primaryNeedsNewline = this.backupNeedsNewline = false;
                this.storageSequence = sequence; this.storageHash = hash;
                this.changes = new JournalDelta(); this.checkpointRequired = false; this.faulted = false;
                this.ClearDeferredFaultUnsafe(); // the checkpoint superseded any lost record
                return true;
            }
            catch
            {
                // fix55D: a failed heal keeps the deferred fault (still healable); any other write failure is a hard fault.
                if (attempt >= PersistAttempts)
                {
                    if (this.deferredLost) this.offsetsKnown = false; // the next heal verifies the copies again
                    else this.faulted = true;
                    return false;
                }
                Thread.Sleep(PersistRetryDelayMs * attempt);
            }
        }
    }

    private static string RecordHash(long sequence, string? previous, byte[] payload)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        hash.AppendData(Encoding.UTF8.GetBytes("command-journal-storage-v2\n" + sequence.ToString(
            System.Globalization.CultureInfo.InvariantCulture) + "\n" + previous + "\n"));
        hash.AppendData(payload);
        return Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
    }

    private static LoadedJournal? ReadJournal(string path) => ReadJournal(path, out _, out _);
    private static LoadedJournal? ReadJournal(string path, out long observedSequence, out bool complete)
    {
        observedSequence = 0;
        complete = false;
        if (!File.Exists(path)) { complete = true; return null; }
        try
        {
            using var stream = AtomicFileWriter.OpenRead(path);
            JournalSnapshot? snapshot = null;
            long sequence = 0, highestSeen = 0, validLength = 0;
            string? hash = null;
            bool legacy = false;
            int invalidTailLines = 0;
            StringBuilder? legacyText = null;
            foreach (JournalLine line in Lines(stream))
            {
                if (legacy && invalidTailLines == 0 && string.IsNullOrWhiteSpace(line.Text))
                { validLength = line.End; continue; } // V1 permits trailing whitespace; preserve it during migration.
                if (line.Terminated && TryFrame(line.Text, out StorageRecord? record, out JournalDelta? delta))
                {
                    long previousHighest = highestSeen;
                    highestSeen = Math.Max(highestSeen, record!.Sequence);
                    observedSequence = highestSeen;
                    if (delta!.Checkpoint is not null && !delta.Reset && record.PreviousSha256 is null
                        && record.Sequence > previousHighest && Normalize(delta.Checkpoint) is { } checkpoint)
                    {
                        // A self-contained migration/recovery record can repair a corrupt older copy
                        // by appending. Earlier bytes remain available for forensics, never replaced.
                        snapshot = checkpoint; legacyText = null;
                    }
                    else if (snapshot is not null && invalidTailLines == 0 && delta.Checkpoint is null
                        && record.Sequence == sequence + 1 && record.PreviousSha256 == hash)
                        snapshot = Apply(snapshot, delta);
                    else { invalidTailLines++; continue; }
                    sequence = record.Sequence; hash = record.Sha256; legacy = false;
                    validLength = line.End; invalidTailLines = 0;
                    continue;
                }
                if (snapshot is null)
                {
                    // V1 snapshots may be minified or pretty-printed, with or without a final newline.
                    legacyText ??= new StringBuilder();
                    legacyText.Append(line.Text).Append('\n');
                    if (ReadLegacy(legacyText.ToString(), line.End) is { } old)
                    {
                        snapshot = old.Snapshot; hash = old.Hash; legacy = true; validLength = line.End;
                        invalidTailLines = 0; legacyText = null; continue;
                    }
                    if (!line.Terminated && line.Text.StartsWith("{\"schemaVersion\":2", StringComparison.Ordinal))
                        snapshot = JournalSnapshot.Empty(); // A torn first v2 record never admitted input.
                }
                invalidTailLines++;
            }
            // Only a last bad record is a skippable torn tail. Interior gaps need a later verified
            // checkpoint; a reordered or corrupted chain without one fails closed.
            complete = true;
            return snapshot is null || invalidTailLines > 1 ? null : new LoadedJournal(snapshot, sequence, hash, validLength, legacy);
        }
        catch { return null; }
    }

    private static LoadedJournal? ReadLegacy(string raw, long length)
    {
        try
        {
            var envelope = JsonSerializer.Deserialize<JournalEnvelope>(raw, JsonOptions);
            if (envelope?.SchemaVersion != SchemaVersion) return null;
            byte[] payload = Convert.FromBase64String(envelope.PayloadBase64);
            if (Sha256Hex(payload) != envelope.Sha256) return null;
            var legacy = Normalize(JsonSerializer.Deserialize<JournalSnapshot>(payload, JsonOptions));
            return legacy is null ? null : new LoadedJournal(legacy, 0, envelope.Sha256, length, true);
        }
        catch { return null; }
    }

    private static bool TryFrame(string text, out StorageRecord? record, out JournalDelta? delta)
    {
        record = null; delta = null;
        try
        {
            record = JsonSerializer.Deserialize<StorageRecord>(text, JsonOptions);
            if (record is null || record.SchemaVersion != StorageSchemaVersion || record.Sequence < 1) return false;
            byte[] payload = Convert.FromBase64String(record.PayloadBase64);
            if (payload.Length != record.PayloadLength || RecordHash(record.Sequence, record.PreviousSha256, payload) != record.Sha256) return false;
            delta = JsonSerializer.Deserialize<JournalDelta>(payload, JsonOptions);
            return delta is not null;
        }
        catch { return false; }
    }

    private sealed record JournalLine(string Text, long End, bool Terminated);
    private static IEnumerable<JournalLine> Lines(Stream stream)
    {
        byte[] buffer = new byte[64 * 1024];
        using var pending = new MemoryStream();
        long end = 0;
        bool first = true;
        int count;
        while ((count = stream.Read(buffer, 0, buffer.Length)) > 0)
        {
            int start = 0;
            while (start < count)
            {
                int newline = Array.IndexOf(buffer, (byte)'\n', start, count - start);
                int length = (newline < 0 ? count : newline) - start;
                pending.Write(buffer, start, length); end += length;
                if (newline < 0) break;
                string text = Encoding.UTF8.GetString(pending.GetBuffer(), 0, checked((int)pending.Length));
                if (first) { text = text.TrimStart('\uFEFF'); first = false; }
                end++;
                yield return new JournalLine(text, end, true);
                pending.SetLength(0); pending.Position = 0; start = newline + 1;
            }
        }
        if (pending.Length > 0)
        {
            string text = Encoding.UTF8.GetString(pending.GetBuffer(), 0, checked((int)pending.Length));
            if (first) text = text.TrimStart('\uFEFF');
            yield return new JournalLine(text, end, false);
        }
    }

    private static bool NeedsNewline(string path, long length)
    {
        if (length == 0) return false;
        using var stream = AtomicFileWriter.OpenRead(path);
        stream.Position = length - 1;
        return stream.ReadByte() != '\n';
    }

    private void InitializeOffsets(LoadedJournal? primary, LoadedJournal? backup)
    {
        this.primaryLength = primary?.Length ?? (File.Exists(this.activePath) ? new FileInfo(this.activePath).Length : 0);
        this.backupLength = backup?.Length ?? (File.Exists(this.ActiveBackupPath) ? new FileInfo(this.ActiveBackupPath).Length : 0);
        this.primaryNeedsNewline = NeedsNewline(this.activePath, this.primaryLength);
        this.backupNeedsNewline = NeedsNewline(this.ActiveBackupPath, this.backupLength);
        this.offsetsKnown = true;
    }

    private static JournalSnapshot? Normalize(JournalSnapshot? value)
    {
        if (value is null || value.SchemaVersion != SchemaVersion) return null;
        value.Namespaces ??= new(StringComparer.Ordinal); value.Rows ??= new(); value.Tombstones ??= new();
        value.Watermarks ??= new(StringComparer.Ordinal); value.ArchivedWatermarks ??= new(StringComparer.Ordinal);
        value.RetiredCounts ??= new(StringComparer.Ordinal);
        return value;
    }
    private static JournalSnapshot Apply(JournalSnapshot snapshot, JournalDelta delta)
    {
        if (delta.Reset)
        {
            JournalSnapshot previous = snapshot;
            snapshot = JournalSnapshot.Empty();
            foreach (var entry in previous.ArchivedWatermarks.Concat(previous.Watermarks))
                snapshot.ArchivedWatermarks[entry.Key] = Math.Max(entry.Value, snapshot.ArchivedWatermarks.GetValueOrDefault(entry.Key));
        }
        snapshot.Scope = delta.Scope; snapshot.LastOrdinal = delta.LastOrdinal;
        foreach (var (key, value) in delta.RetiredCounts) snapshot.RetiredCounts[key] = value;
        foreach (var (key, ns) in delta.Namespaces)
            if (ns is null) snapshot.Namespaces.Remove(key); else snapshot.Namespaces[key] = ns;
        foreach (var (key, value) in delta.Watermarks) snapshot.Watermarks[key] = value;
        foreach (var (key, value) in delta.ArchivedWatermarks) snapshot.ArchivedWatermarks[key] = value;
        foreach (RowChange change in delta.Rows.Values)
        {
            bool Same(JournalRow row) => row.NamespaceKey == change.NamespaceKey && row.RequestSeq == change.RequestSeq;
            if (change.Row is null)
            {
                snapshot.Rows.RemoveAll(Same); snapshot.Tombstones.RemoveAll(Same); continue;
            }
            var target = change.Tombstone ? snapshot.Tombstones : snapshot.Rows;
            var other = change.Tombstone ? snapshot.Rows : snapshot.Tombstones;
            other.RemoveAll(Same);
            int index = target.FindIndex(Same);
            if (index < 0) target.Add(change.Row); else target[index] = change.Row;
        }
        snapshot.Tombstones.Sort((a, b) => a.Ordinal.CompareTo(b.Ordinal));
        return snapshot;
    }

    /// <summary>Verified materialized v1 view for offline readers/replays; never opens the live path implicitly.</summary>
    internal static string ReadSnapshotForReplay(string path)
    {
        var head = SelectHead(path);
        if (head is not null) path = SegmentPath(path, head.Active);
        LoadedJournal? primary = ReadJournal(path), backup = ReadJournal(path + ".bak");
        if (primary is not null && backup is not null && primary.Sequence > 0
            && primary.Sequence == backup.Sequence && primary.Hash != backup.Hash)
            throw new InvalidDataException("Conflicting command journal chains");
        var loaded = primary is null || backup is not null && backup.Sequence > primary.Sequence ? backup : primary;
        if (head is not null && (loaded is null || loaded.Sequence < head.Generation))
            throw new InvalidDataException("Missing published journal checkpoint");
        return JsonSerializer.Serialize(loaded?.Snapshot ?? throw new InvalidDataException("Invalid command journal"), JsonOptions);
    }
}
