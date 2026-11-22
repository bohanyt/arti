using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

// ADR 48: journals append within a segment; only the small redundant head is replaced.
public sealed partial class CommandSettledJournal
{
    public const long DefaultMaxSegmentBytes = 64L * 1024 * 1024;
    public const int DefaultMaxRetiredSegments = 2;
    private readonly long maxSegmentBytes;
    private readonly int maxRetiredSegments;
    private string activePath;
    private string ActiveBackupPath => this.activePath + ".bak";
    private long checkpointBytes;
    private SegmentHead? segmentHead;
    private bool rotationFaulted;
    public string ActivePathName => this.activePath;
    public string SegmentDirectory => this.path + ".segments";
    internal Action<string>? RotationStepForTests { get; set; }

    private sealed record SegmentHead(long Generation, string Active, long CheckpointBytes, string[] Retired);
    private sealed record ClosedCopy(long Bytes, string Sha256);
    private sealed record ClosedSegment(long Sequence, string? RecordSha256, ClosedCopy Primary, ClosedCopy Mirror);

    private static string SegmentPath(string logical, string name) =>
        System.IO.Path.Combine(logical + ".segments", name, "journal.jsonl");
    private static bool ValidSegmentName(string name) => name.Length == 52 && name[19] == '-'
        && name.Take(19).All(char.IsDigit) && Guid.TryParseExact(name.Substring(20), "N", out _);
    private static string NewSegmentName(long sequence) => sequence.ToString("D19") + "-" + Guid.NewGuid().ToString("N");

    private static string HeadWire(SegmentHead head)
    {
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(head, JsonOptions);
        return JsonSerializer.Serialize(new JournalEnvelope {
            SchemaVersion = 1, PayloadBase64 = Convert.ToBase64String(payload), Sha256 = Sha256Hex(payload),
        }, JsonOptions);
    }

    private static SegmentHead? ReadHead(string file)
    {
        if (!File.Exists(file)) return null;
        try
        {
            var envelope = JsonSerializer.Deserialize<JournalEnvelope>(AtomicFileWriter.ReadAllText(file), JsonOptions);
            if (envelope?.SchemaVersion != 1) return null;
            byte[] payload = Convert.FromBase64String(envelope.PayloadBase64);
            if (Sha256Hex(payload) != envelope.Sha256) return null;
            var head = JsonSerializer.Deserialize<SegmentHead>(payload, JsonOptions);
            return head is { Generation: > 0, CheckpointBytes: > 0, Retired: not null }
                && ValidSegmentName(head.Active) && head.Retired.All(ValidSegmentName)
                && !head.Retired.Contains(head.Active) && head.Retired.Distinct().Count() == head.Retired.Length ? head : null;
        }
        catch { return null; }
    }

    private static SegmentHead? SelectHead(string logical)
    {
        bool exists = File.Exists(logical + ".head") || File.Exists(logical + ".head.bak");
        var primary = ReadHead(logical + ".head");
        var mirror = ReadHead(logical + ".head.bak");
        if (exists && primary is null && mirror is null) throw new InvalidDataException("Invalid journal heads");
        if (primary is not null && mirror is not null && primary.Generation == mirror.Generation
            && HeadWire(primary) != HeadWire(mirror)) throw new InvalidDataException("Conflicting journal heads");
        var head = primary is null || mirror is not null && mirror.Generation > primary.Generation ? mirror : primary;
        if (head is not null && !File.Exists(SegmentPath(logical, head.Active))
            && !File.Exists(SegmentPath(logical, head.Active) + ".bak"))
            throw new InvalidDataException("Missing active journal segment"); // Never fall back to retired commands.
        return head;
    }

    private void PublishHead(SegmentHead head)
    {
        string wire = HeadWire(head);
        AtomicFileWriter.Write(this.path + ".head.bak", wire, flushToDisk: true);
        this.RotationStepForTests?.Invoke("mirror_head");
        AtomicFileWriter.Write(this.path + ".head", wire, flushToDisk: true);
        this.RotationStepForTests?.Invoke("primary_head");
    }

    private void SelectSegmentUnsafe()
    {
        this.segmentHead = SelectHead(this.path);
        if (this.segmentHead is not { } head) return;
        this.activePath = SegmentPath(this.path, head.Active);
        this.checkpointBytes = head.CheckpointBytes;
    }

    private static ClosedCopy DescribeCopy(string path)
    {
        using var stream = AtomicFileWriter.OpenRead(path);
        using var hash = SHA256.Create();
        return new ClosedCopy(stream.Length, Convert.ToHexString(hash.ComputeHash(stream)).ToLowerInvariant());
    }

    private static void CopyDurable(string source, string target)
    {
        using var input = AtomicFileWriter.OpenRead(source);
        using var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
        input.CopyTo(output); output.Flush(flushToDisk: true);
    }

    private bool TryRotateUnsafe(bool startup = false)
    {
        this.DrainDeferredUnsafe(); // fix54I: closed-segment hashes and copies need every queued record durable
        if (this.faulted) return false;
        if (Math.Max(this.primaryLength, this.backupLength) - this.checkpointBytes < this.maxSegmentBytes
            || !startup && this.snapshot.Rows.Any(row => row.State == JournalRowState.Admitted)) return true;
        long started = this.WriteTimings.Begin("rotate");
        bool succeeded = false;
        try
        {
            // Called at startup or before any changes for a new admission. Include EVERY semantic fact.
            if (this.checkpointRequired || this.changes.Rows.Count != 0 || this.changes.Namespaces.Count != 0
                || this.changes.Watermarks.Count != 0 || this.changes.ArchivedWatermarks.Count != 0)
                throw new InvalidOperationException("Rotation requires a committed journal boundary");
            long sequence = checked(this.storageSequence + 1);
            byte[] payload = JsonSerializer.SerializeToUtf8Bytes(new JournalDelta { Checkpoint = this.snapshot,
                Scope = this.snapshot.Scope, LastOrdinal = this.snapshot.LastOrdinal }, JsonOptions);
            string hash = RecordHash(sequence, null, payload);
            byte[] wire = JsonSerializer.SerializeToUtf8Bytes(new StorageRecord { Sequence = sequence,
                PayloadBase64 = Convert.ToBase64String(payload), PayloadLength = payload.Length, Sha256 = hash,
            }, JsonOptions).Concat(new byte[] { (byte)'\n' }).ToArray();
            this.WriteTimings.Record(sequence, wire.LongLength);
            string next = NewSegmentName(sequence), nextPath = SegmentPath(this.path, next);
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(nextPath)!);
            AtomicFileWriter.AppendDurable(nextPath + ".bak", 0, wire);
            this.RotationStepForTests?.Invoke("mirror_checkpoint");
            AtomicFileWriter.AppendDurable(nextPath, 0, wire);
            this.RotationStepForTests?.Invoke("primary_checkpoint");

            string closed = this.segmentHead?.Active ?? NewSegmentName(this.storageSequence);
            string closedPath = SegmentPath(this.path, closed);
            if (this.segmentHead is null)
            {
                Directory.CreateDirectory(System.IO.Path.GetDirectoryName(closedPath)!);
                CopyDurable(this.activePath, closedPath);
                CopyDurable(this.ActiveBackupPath, closedPath + ".bak");
            }
            var description = new ClosedSegment(this.storageSequence, this.storageHash,
                DescribeCopy(closedPath), DescribeCopy(closedPath + ".bak"));
            AtomicFileWriter.Write(closedPath + ".closed.json", JsonSerializer.Serialize(description, JsonOptions), flushToDisk: true);
            this.RotationStepForTests?.Invoke("closed");
            string[] retained = (this.segmentHead?.Retired ?? Array.Empty<string>()).Append(closed)
                .TakeLast(this.maxRetiredSegments).ToArray();
            var head = new SegmentHead(sequence, next, wire.LongLength, retained);
            this.PublishHead(head); // Both checkpoint copies precede BOTH durable head copies.
            this.segmentHead = head; this.activePath = nextPath; this.checkpointBytes = wire.LongLength;
            this.primaryLength = this.backupLength = wire.LongLength;
            this.primaryNeedsNewline = this.backupNeedsNewline = false;
            this.storageSequence = sequence; this.storageHash = hash;
            this.PruneSegmentsUnsafe();
            return succeeded = true;
        }
        catch { this.rotationFaulted = this.faulted = true; return false; } // A partial handover must restart before any further write.
        finally { this.WriteTimings.End(started, succeeded); }
    }

    private void PruneSegmentsUnsafe()
    {
        if (this.segmentHead is not { } head) return;
        try
        {
            // Only after identical durable heads: neither copy can still need a pruned generation.
            var keep = head.Retired.Append(head.Active).ToHashSet(StringComparer.Ordinal);
            foreach (string directory in Directory.EnumerateDirectories(this.SegmentDirectory))
                if (ValidSegmentName(System.IO.Path.GetFileName(directory)) && !keep.Contains(System.IO.Path.GetFileName(directory)))
                    Directory.Delete(directory, recursive: true);
            File.Delete(this.path); File.Delete(this.backupPath); // Legacy history is now a retained closed segment.
        }
        catch { /* Read-only forensic files may delay pruning; recovery never depends on deletion. */ }
    }
}
