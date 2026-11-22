using System.Text;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// fix54I, ADR 73 (a39 FH:L126431, 7.86 s TICK_STALL): a terminal (native release) record's two FlushFileBuffers queued behind
/// clip-ops ffmpeg writes on the same volume and took 6.67 s on the game thread. Terminal records are now appended by one
/// bounded background writer. The game thread still serializes, hashes and sequences the record under the journal lock,
/// so the chain and its offsets are fixed before the write. Every other persist (admit, finalize, reset, retire,
/// bind_scope, recover, rotation) first waits until all queued terminal records are durable on mirror and primary, so
/// ADR 37/48 ordering holds: no admission, result publication or later fact is ever durable ahead of an earlier terminal.
/// Admit stays synchronous (fix14e barrier).
/// fix55D (REVIEW2 #2/#3): that wait is bounded (<see cref="DeferredDrainTimeoutMs"/>) and a timeout faults the journal
/// with a named reason; the writer can never die with records still counted as pending; and a deferred-writer fault heals
/// at the next barrier, rate-limited, only after both copies are verified to end exactly at the lost record (or after it).
/// </summary>
public sealed partial class CommandSettledJournal
{
    public const int MaxDeferredRecords = 8;
    /// <summary>fix55D: the longest one barrier (game thread, journal lock) waits for queued terminal records.</summary>
    public const int DeferredDrainTimeoutMs = 10_000;
    /// <summary>fix55D: off the game thread a write may ride out a seconds-long AV/ffmpeg lock (50..1000 ms, 3.55 s in all).</summary>
    internal const int DeferredPersistAttempts = 8;
    /// <summary>fix55D: first spacing between heal attempts; it doubles per failed attempt up to 30 s.</summary>
    public const int DeferredHealSpacingMs = 2_000;
    internal const int DeferredHealMaxSpacingMs = 30_000;
    internal static int DeferredRetryDelayMs(int attempt) => Math.Min(50 << Math.Min(attempt - 1, 5), 1_000);

    public const string FaultDeferredWriteFailed = "deferred_write_failed";
    public const string FaultDeferredWriterCrashed = "deferred_writer_crashed";
    public const string FaultDeferredDrainTimeout = "deferred_drain_timeout";
    public const string FaultDeferredHealUnverified = "deferred_heal_unverified";

    private bool deferTerminal;
    private readonly object writerSync = new();
    // Fixed-size FIFO ring (writerSync): bounded by construction; full means the caller persists synchronously.
    private readonly DeferredRecord?[] deferredRing = new DeferredRecord?[MaxDeferredRecords];
    private int deferredHead, deferredCount;
    private bool deferredWriterActive; // writerSync: one pooled work item drains the queue, then returns
    private bool deferredFailed; // writerSync
    private DeferredRecord? deferredFailure; // writerSync: the first record that may not be durable (heal anchor)
    private bool deferredLost; // sync: a deferred record may be absent on disk; the next persist verifies and checkpoints
    private bool deferredFault; // sync: lost record or a timed-out barrier; only a verified heal or checkpoint clears it
    private int healAttempts; // sync
    private long lastHealMs; // sync
    private volatile bool deferredFaultVisible;
    private volatile string? deferredFaultReason;
    internal Action<long>? BeforeDeferredWriteForTests { get; set; }
    internal Action<long>? AfterDeferredWriteForTests { get; set; }
    internal int DrainTimeoutMs { get; set; } = DeferredDrainTimeoutMs;
    internal int HealSpacingMs { get; set; } = DeferredHealSpacingMs;
    public JournalWriteProbe DeferredWriteTimings { get; } = new();
    public bool DefersTerminalWrites => this.deferTerminal;
    /// <summary>fix55D: why the deferred writer faulted the journal (null when it did not). Never takes a lock.</summary>
    public string? DeferredFaultReason => this.deferredFaultReason;

    private sealed record DeferredRecord(string BackupPath, long BackupLength, bool BackupNewline,
        string PrimaryPath, long PrimaryLength, bool PrimaryNewline, byte[] Wire, long Sequence, string Hash, string? Previous);

    /// <summary>Queued plus in-flight terminal records. Never takes the journal (game-thread) lock.</summary>
    public int DeferredPending { get { lock (this.writerSync) return this.deferredCount; } }

    /// <summary>Explicit constructor choice, else the ARTI_JOURNAL_DEFER_TERMINAL operator switch (1/0), else on for
    /// the live journal only.</summary>
    private static bool ResolveDeferTerminal(bool? requested, bool livePath) =>
        requested ?? Environment.GetEnvironmentVariable("ARTI_JOURNAL_DEFER_TERMINAL") switch
        {
            "1" => true,
            "0" => false,
            _ => livePath,
        };

    /// <summary>Process exit / tests: wait for queued terminal records; true when all are durable.</summary>
    public bool WaitForDeferredWrites(int timeoutMs = Timeout.Infinite)
    {
        lock (this.writerSync)
        {
            DateTime until = timeoutMs < 0 ? DateTime.MaxValue : DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (this.deferredCount > 0)
            {
                TimeSpan left = until == DateTime.MaxValue ? Timeout.InfiniteTimeSpan : until - DateTime.UtcNow;
                if (left != Timeout.InfiniteTimeSpan && left <= TimeSpan.Zero) return false;
                Monitor.Wait(this.writerSync, left);
            }
            return !this.deferredFailed && this.deferredFailure is null;
        }
    }

    /// <summary>Non-blocking: surface a writer failure as the ordinary fault before any decision reads it. With
    /// <paramref name="heal"/> (only before the caller changes state) a healable fault may heal here.</summary>
    private void PollDeferredUnsafe(bool heal = false)
    {
        if (!this.deferTerminal || !this.deferredFaultVisible && !this.deferredFault) return;
        if (this.ConsumeDeferredFailureUnsafe() && heal) this.TryHealDeferredUnsafe();
    }

    /// <summary>Ordering barrier: every queued terminal record is durable (or known lost) before the caller proceeds.
    /// fix55D: bounded. False when the writer still owns the copies; the journal is then faulted
    /// (<see cref="FaultDeferredDrainTimeout"/>) and nothing may be appended until the writer is idle again.</summary>
    private bool DrainDeferredUnsafe(bool heal = true)
    {
        if (!this.deferTerminal) return true;
        lock (this.writerSync)
        {
            // Once faulted, a still-busy writer fails the barrier at once: one slow disk costs one bounded stall, not one per tick.
            long until = Environment.TickCount64 + (this.deferredFault ? 0 : Math.Max(0, this.DrainTimeoutMs));
            while (this.deferredCount > 0)
            {
                long left = until - Environment.TickCount64;
                if (left <= 0) break;
                Monitor.Wait(this.writerSync, TimeSpan.FromMilliseconds(left));
            }
        }
        if (!this.ConsumeDeferredFailureUnsafe())
        {
            this.deferredFault = true;
            this.deferredFaultReason ??= FaultDeferredDrainTimeout;
            return false;
        }
        if (heal) this.TryHealDeferredUnsafe();
        return true;
    }

    /// <summary>False while the writer still has records; otherwise turns a reported failure into the lost state.</summary>
    private bool ConsumeDeferredFailureUnsafe()
    {
        bool failed;
        lock (this.writerSync)
        {
            if (this.deferredCount > 0) return false;
            failed = this.deferredFailed;
            this.deferredFailed = false;
        }
        if (failed) this.MarkDeferredLostUnsafe();
        return true;
    }

    private void MarkDeferredLostUnsafe()
    {
        // Same disposition as a failed synchronous persist: faulted, and nothing is appended at a guessed offset.
        this.deferredFault = true;
        this.deferredLost = true;
        this.offsetsKnown = false;
        this.checkpointRequired = true;
        this.healAttempts = 0;
    }

    /// <summary>
    /// fix55D (REVIEW2 #3): bounded heal of a deferred-writer fault, run at a barrier before the caller changes state.
    /// A timed-out barrier whose writer later confirmed every record durable simply clears. A lost record heals by a
    /// verified recovery checkpoint (see <see cref="VerifyDeferredLossUnsafe"/>); attempts are spaced 2 s, 4 s, ... 30 s
    /// so a locked disk costs one short check per spacing, never one per tick. Any other fault is never cleared here.
    /// </summary>
    private void TryHealDeferredUnsafe()
    {
        if (!this.deferredFault || this.hardFault || this.rotationFaulted || !this.ConsumeDeferredFailureUnsafe()) return;
        if (!this.deferredLost) { this.ClearDeferredFaultUnsafe(); return; }
        long now = Environment.TickCount64;
        if (this.healAttempts > 0 && now - this.lastHealMs
            < Math.Min((long)Math.Max(0, this.HealSpacingMs) << Math.Min(this.healAttempts - 1, 4), DeferredHealMaxSpacingMs)) return;
        this.healAttempts++;
        this.lastHealMs = now;
        this.TryPersistUnsafe("heal"); // checkpointRequired: the whole verified snapshot, including every lost fact
    }

    private void ClearDeferredFaultUnsafe()
    {
        this.deferredLost = this.deferredFault = this.deferredFaultVisible = false;
        this.deferredFaultReason = null;
        this.healAttempts = 0;
        lock (this.writerSync) this.deferredFailure = null;
    }

    /// <summary>
    /// fix55D: the heal precondition. Each copy must still hold every byte before the first lost record F, then only a
    /// prefix of F's own bytes (absent or torn) or F whole, and the record ending at F's offset must be F's chain
    /// predecessor (sequence and hash). Reads at most one record before F plus F; never the whole segment. On success the
    /// offsets point past the verified end of each copy, so the recovery checkpoint replaces only F's torn tail.
    /// </summary>
    private bool VerifyDeferredLossUnsafe()
    {
        DeferredRecord? lost;
        lock (this.writerSync) lost = this.deferredFailure;
        if (lost is null || lost.Previous is null || lost.PrimaryPath != this.activePath || lost.BackupPath != this.ActiveBackupPath
            || lost.Sequence > this.storageSequence) return false;
        try
        {
            if (!VerifyLossCopy(lost.BackupPath, lost.BackupLength, lost.BackupNewline, lost, out long backupEnd, out bool backupWhole)
                || !VerifyLossCopy(lost.PrimaryPath, lost.PrimaryLength, lost.PrimaryNewline, lost, out long primaryEnd, out bool primaryWhole))
                return false;
            this.backupLength = backupEnd; this.primaryLength = primaryEnd;
            this.backupNeedsNewline = !backupWhole && lost.BackupNewline;
            this.primaryNeedsNewline = !primaryWhole && lost.PrimaryNewline;
            this.offsetsKnown = true;
            return true;
        }
        catch { return false; }
    }

    private static bool VerifyLossCopy(string path, long offset, bool newline, DeferredRecord lost, out long end, out bool whole)
    {
        end = offset; whole = false;
        using var stream = AtomicFileWriter.OpenRead(path);
        if (offset < 1 || stream.Length < offset) return false; // shortened outside its writer
        byte[] expected = newline ? new[] { (byte)'\n' }.Concat(lost.Wire).ToArray() : lost.Wire;
        long tail = stream.Length - offset;
        if (tail > expected.Length) return false; // bytes this process never wrote
        byte[] actual = new byte[tail];
        stream.Position = offset;
        ReadFully(stream, actual, actual.Length);
        if (!actual.AsSpan().SequenceEqual(expected.AsSpan(0, actual.Length))) return false;
        whole = tail == expected.Length;
        if (whole) end = stream.Length;
        return PredecessorMatches(stream, offset, lost);
    }

    private static bool PredecessorMatches(FileStream stream, long offset, DeferredRecord lost)
    {
        long contentEnd = offset;
        byte[] buffer = new byte[64 * 1024];
        stream.Position = contentEnd - 1;
        if (stream.ReadByte() == '\n') contentEnd--;
        long start = contentEnd;
        while (start > 0)
        {
            int count = (int)Math.Min(buffer.Length, start);
            stream.Position = start - count;
            ReadFully(stream, buffer, count);
            int newline = Array.LastIndexOf(buffer, (byte)'\n', count - 1, count);
            if (newline >= 0) { start = start - count + newline + 1; break; }
            start -= count;
        }
        byte[] line = new byte[contentEnd - start];
        stream.Position = start;
        ReadFully(stream, line, line.Length);
        return TryFrame(Encoding.UTF8.GetString(line).TrimStart('﻿'), out StorageRecord? record, out _)
            && record!.Sequence == lost.Sequence - 1 && record.Sha256 == lost.Previous;
    }

    private static void ReadFully(Stream stream, byte[] buffer, int count)
    {
        for (int read = 0, n; read < count; read += n)
            if ((n = stream.Read(buffer, read, count - read)) == 0) throw new EndOfStreamException();
    }

    private bool CanDeferUnsafe(string phase)
    {
        if (!this.deferTerminal || phase != "terminal" || this.checkpointRequired || !this.offsetsKnown
            || this.rotationFaulted || this.deferredLost || this.faulted) return false;
        lock (this.writerSync)
            return !this.deferredFailed && this.deferredFailure is null && this.deferredCount < MaxDeferredRecords;
    }

    private void EnqueueDeferredUnsafe(byte[] wire, long sequence, string hash, string? previous)
    {
        var record = new DeferredRecord(this.ActiveBackupPath, this.backupLength, this.backupNeedsNewline,
            this.activePath, this.primaryLength, this.primaryNeedsNewline, wire, sequence, hash, previous);
        bool inline = false;
        lock (this.writerSync)
        {
            this.deferredRing[(this.deferredHead + this.deferredCount++) % MaxDeferredRecords] = record;
            if (!this.deferredWriterActive)
            {
                // Event-driven, never a polling loop: it exists only while records are queued.
                this.deferredWriterActive = true;
                try { ThreadPool.UnsafeQueueUserWorkItem(_ => this.DeferredWriterLoop(), null); }
                catch { inline = true; } // fix55D: no pool item -> write here, as a full ring does
            }
        }
        if (inline) this.DeferredWriterLoop();
    }

    private void DeferredWriterLoop()
    {
        bool idle = false;
        try
        {
            while (true)
            {
                DeferredRecord next;
                lock (this.writerSync)
                {
                    if (this.deferredCount == 0) { this.deferredWriterActive = false; idle = true; return; }
                    next = this.deferredRing[this.deferredHead]!; // stays counted as pending until durable
                }
                bool ok = this.WriteDeferred(next);
                this.AfterDeferredWriteForTests?.Invoke(next.Sequence);
                lock (this.writerSync)
                {
                    if (ok)
                    {
                        this.deferredRing[this.deferredHead] = null;
                        this.deferredHead = (this.deferredHead + 1) % MaxDeferredRecords;
                        this.deferredCount--;
                    }
                    else this.LoseDeferredFromHeadUnsafe(FaultDeferredWriteFailed);
                    Monitor.PulseAll(this.writerSync);
                }
            }
        }
        catch (Exception)
        {
            // fix55D (REVIEW2 #2): never escape a pool thread (that ends the process); handled below.
        }
        finally
        {
            if (!idle)
                lock (this.writerSync)
                {
                    // A writer that is gone must never leave a barrier waiting on records it still counted as pending.
                    if (this.deferredCount > 0) this.LoseDeferredFromHeadUnsafe(FaultDeferredWriterCrashed);
                    this.deferredWriterActive = false;
                    Monitor.PulseAll(this.writerSync);
                }
        }
    }

    /// <summary>writerSync: the head record (and every later one, which chains onto it) is not known durable.</summary>
    private void LoseDeferredFromHeadUnsafe(string reason)
    {
        // Later records chain onto this one: never append them past a missing predecessor.
        this.deferredFailure = this.deferredRing[this.deferredHead];
        Array.Clear(this.deferredRing);
        this.deferredHead = this.deferredCount = 0;
        this.deferredFailed = true;
        this.deferredFaultReason = reason;
        this.deferredFaultVisible = true;
    }

    private bool WriteDeferred(DeferredRecord record)
    {
        long start = this.DeferredWriteTimings.Begin("terminal");
        this.DeferredWriteTimings.Record(record.Sequence, record.Wire.LongLength);
        bool succeeded = false;
        try
        {
            for (int attempt = 1; ; attempt++)
            {
                try
                {
                    this.BeforeDeferredWriteForTests?.Invoke(record.Sequence);
                    string? directory = System.IO.Path.GetDirectoryName(record.PrimaryPath);
                    if (!string.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);
                    // Identical bytes at the acknowledged boundaries; a retry discards only its own torn tail.
                    AtomicFileWriter.AppendDurable(record.BackupPath, record.BackupLength, record.Wire, record.BackupNewline);
                    AtomicFileWriter.AppendDurable(record.PrimaryPath, record.PrimaryLength, record.Wire, record.PrimaryNewline);
                    return succeeded = true;
                }
                catch
                {
                    if (attempt >= DeferredPersistAttempts) return false;
                    Thread.Sleep(DeferredRetryDelayMs(attempt));
                }
            }
        }
        finally { this.DeferredWriteTimings.End(start, succeeded); }
    }
}
