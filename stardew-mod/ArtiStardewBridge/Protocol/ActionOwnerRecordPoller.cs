using System.Text;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Bounded control/stop/stop_all/rearm reads in the existing inbox folder.
/// Bukan transport, bukan receiver, bukan owner: hanya baca/parse
/// berkas tetap di folder inbox yang sama, lalu menyerahkan snapshot
/// imutable terbaru. Tidak membaca dunia game, tidak Press, tidak
/// menyentuh berkas permintaan mutasi.
///
/// Irama default 50 ms (#133 §5.3). Handoff latest-wins, tanpa antrean.
/// Game thread yang menerapkan STOP lalu control. Shutdown deterministik
/// lewat <see cref="Stop"/> / <see cref="Dispose"/>.
/// </summary>
public sealed class ActionOwnerRecordPoller : IDisposable
{
    public const int DefaultIntervalMs = 50;

    readonly string folder;
    readonly int intervalMs;
    readonly object gate = new();
    readonly ManualResetEventSlim pulse = new(false);
    readonly ManualResetEventSlim stopped = new(true);

    ActionOwnerRecordHandoff latest = ActionOwnerRecordHandoff.Empty;
    string? publishedStopIdentity;
    string? publishedControlIdentity;
    bool publishedStopPresent;
    bool publishedControlPresent;
    long seq;

    string? deleteControlIdentity;
    string? publishedHaltIdentity;
    readonly int[] faultPolls = new int[2];

    volatile bool running;
    bool disposed;

    public ActionOwnerRecordPoller(string folder, int intervalMs = DefaultIntervalMs)
    {
        if (string.IsNullOrWhiteSpace(folder))
            throw new ArgumentException("folder inbox wajib", nameof(folder));
        if (intervalMs <= 0)
            throw new ArgumentOutOfRangeException(nameof(intervalMs), "interval poller harus > 0");
        this.folder = folder;
        this.intervalMs = intervalMs;
    }

    // Hermetic I/O-failure tests only. Null in production; faults retain the
    // exact pending acknowledgement and never grant execution authority.
    internal Action<string>? BeforeAcknowledgementDeleteForTests { get; set; }

    public int IntervalMs => this.intervalMs;
    public bool IsRunning => this.running;
    public string Folder => this.folder;

    /// <summary>Snapshot imutable terbaru. Game thread hanya membaca ini.</summary>
    public ActionOwnerRecordHandoff Latest
    {
        get
        {
            lock (this.gate)
                return this.latest;
        }
    }

    public void Start()
    {
        lock (this.gate)
        {
            if (this.disposed)
                throw new ObjectDisposedException(nameof(ActionOwnerRecordPoller));
            if (this.running)
                return;
            this.running = true;
            this.pulse.Reset();
            this.stopped.Reset();
            if (!ThreadPool.QueueUserWorkItem(_ => this.Loop()))
                throw new InvalidOperationException("poller control/stop gagal masuk ThreadPool");
        }
    }

    public void Stop()
    {
        lock (this.gate)
        {
            if (!this.running)
                return;
            this.running = false;
        }

        this.pulse.Set();
        this.stopped.Wait(TimeSpan.FromSeconds(2));
        this.pulse.Reset();
    }

    public void Dispose()
    {
        if (this.disposed)
            return;
        this.Stop();
        this.pulse.Dispose();
        this.stopped.Dispose();
        this.disposed = true;
    }

    /// <summary>
    /// Satu putaran baca/parse. Tes menyuntik ini tanpa thread. Bukan
    /// transport perintah: hanya control/stop.
    /// </summary>
    public ActionOwnerRecordHandoff PollOnce()
    {
        this.ProcessPendingDeletes();

        FileRead stopRead = ReadFixedRecord(
            Path.Combine(this.folder, MenuStepCommandInbox.StopFileName));
        FileRead controlRead = ReadFixedRecord(
            Path.Combine(this.folder, MenuStepCommandInbox.ControlFileName));
        var stops = new[] { this.ReadHaltStop("stop", 0), this.ReadHaltStop("stop_all", 1) };
        var rearm = ReadFixedRecord(Path.Combine(this.folder, "rearm.json"));
        string haltIdentity = string.Join("|", stops.Select(s => $"{s.Present}:{s.Sha256}:{s.FaultPolls}")) + rearm.Identity;

        bool stopChanged = stopRead.Present != this.publishedStopPresent
            || !string.Equals(stopRead.Identity, this.publishedStopIdentity, StringComparison.Ordinal);
        bool controlChanged = controlRead.Present != this.publishedControlPresent
            || !string.Equals(controlRead.Identity, this.publishedControlIdentity, StringComparison.Ordinal);
        if (!stopChanged && !controlChanged && haltIdentity == this.publishedHaltIdentity)
        {
            lock (this.gate)
                return this.latest;
        }

        ParsedStop parsedStop = ParseStop(stopRead);
        ParsedControl parsedControl = ParseControl(controlRead);

        ActionOwnerRecordHandoff next;
        lock (this.gate)
        {
            this.seq++;
            this.publishedHaltIdentity = haltIdentity;
            this.publishedStopIdentity = stopRead.Identity;
            this.publishedControlIdentity = controlRead.Identity;
            this.publishedStopPresent = stopRead.Present;
            this.publishedControlPresent = controlRead.Present;
            next = new ActionOwnerRecordHandoff(
                this.seq,
                stopRead.Present,
                parsedStop.Value,
                parsedStop.Reason,
                stopRead.Identity,
                controlRead.Present,
                parsedControl.Value,
                parsedControl.Reason,
                controlRead.Identity) { Stops = stops, RearmRaw = rearm.Present ? rearm.Raw ?? "#unreadable" : null };
            this.latest = next;
        }

        return next;
    }

    /// <summary>
    /// Game thread sudah menerapkan snapshot. Hapus berkas di thread poller
    /// hanya jika isinya masih identity yang sama — jangan menelan tulis baru.
    /// </summary>
    public void AckConsumed(
        bool deleteStop,
        bool deleteControl,
        string? stopIdentity,
        string? controlIdentity
    )
    {
        lock (this.gate)
        {
            // R0-03: only tools/arti_rearm may archive a stop file. Ignore legacy deleteStop acks.
            if (deleteControl)
            {
                this.deleteControlIdentity = controlIdentity;
            }
        }
    }

    void Loop()
    {
        try
        {
            while (this.running)
            {
                try
                {
                    this.PollOnce();
                }
                catch
                {
                    // Fail closed: jangan biarkan exception mematikan poller,
                    // dan jangan terapkan record yang tidak terbaca.
                }

                this.pulse.Wait(this.intervalMs);
            }
        }
        finally
        {
            this.stopped.Set();
        }
    }

    void ProcessPendingDeletes()
    {
        string? controlId;
        lock (this.gate)
        {
            controlId = this.deleteControlIdentity;
        }
        bool controlDone = controlId is null || TryDeleteIfIdentity(
            Path.Combine(this.folder, MenuStepCommandInbox.ControlFileName), controlId);
        lock (this.gate)
        {
            // A transient sharing violation must not permanently discard the
            // only acknowledgement of an already-applied record. A newer ack
            // wins, and a different on-disk identity is never deleted.
            // One control slot, one attempt per poll; never wait for a reader
            // during shutdown. Retention is not renewal or execution authority.
            if (this.deleteControlIdentity == controlId && controlDone)
                this.deleteControlIdentity = null;
        }
    }

    HaltStopRead ReadHaltStop(string source, int index)
    {
        string? hash = null; JsonElement? record = null; bool present = true;
        try {
            using var stream = AtomicFileWriter.OpenRead(Path.Combine(this.folder, source + ".json"));
            using var sha = SHA256.Create(); hash = Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
            if (stream.Length <= MenuStepCommandInbox.MaxRequestBytes) {
                stream.Position = 0; using var doc = JsonDocument.Parse(stream); var root = doc.RootElement;
                if (root.ValueKind == JsonValueKind.Object && (root.TryGetProperty("v", out _)
                    ? NativeActionOwner.ValidStop(JsonNode.Parse(root.GetRawText())!.AsObject())
                    : ActionOwnerStop.TryParse(root.GetRawText(), out _, out _))) record = root.Clone();
            }
        } catch (FileNotFoundException) { present = false; }
        catch (DirectoryNotFoundException) { present = false; }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException) { }
        this.faultPolls[index] = present && record is null ? Math.Min(3, this.faultPolls[index] + 1) : 0;
        return new(source, present, record, hash, this.faultPolls[index]);
    }

    static FileRead ReadFixedRecord(string path)
    {
        try
        {
            var info = new FileInfo(path);
            if (!info.Exists)
                return FileRead.Missing;

            if (info.Length > MenuStepCommandInbox.MaxRequestBytes)
                return FileRead.Fail("oversize");

            // PM-1 a7 #7 (N:415 ff., 18x MalformedJson): File.ReadAllText shares Read only, so a poll that overlapped the
            // client's atomic replace (MoveFileEx opens the target for DELETE) failed as "io" -> MalformedJson. Share
            // write/delete like ReadHaltStop and retry a transient sharing violation; a record still unreadable fails closed.
            for (int attempt = 0; ; attempt++)
            {
                try
                {
                    using var stream = AtomicFileWriter.OpenRead(path);
                    using var reader = new StreamReader(stream, Encoding.UTF8);
                    string raw = reader.ReadToEnd();
                    if (raw.Length > MenuStepCommandInbox.MaxRequestBytes)
                        return FileRead.Fail("oversize");
                    return FileRead.Ok(raw);
                }
                catch (IOException e) when (attempt < 3 && e is not FileNotFoundException) { Thread.Sleep(2); }
            }
        }
        catch (Exception)
        {
            return FileRead.Fail("io");
        }
    }

    static ParsedStop ParseStop(FileRead read)
    {
        if (!read.Present)
            return new ParsedStop(null, CommandRejectReason.None);
        if (read.FailClosed)
            return new ParsedStop(null, CommandRejectReason.MalformedJson);
        return ActionOwnerStop.TryParse(read.Raw, out ActionOwnerStop stop, out CommandRejectReason reason)
            ? new ParsedStop(stop, CommandRejectReason.None)
            : new ParsedStop(null, reason);
    }

    static ParsedControl ParseControl(FileRead read)
    {
        if (!read.Present)
            return new ParsedControl(null, CommandRejectReason.None);
        if (read.FailClosed)
            return new ParsedControl(null, CommandRejectReason.MalformedJson);
        return ActionOwnerControl.TryParse(read.Raw, out ActionOwnerControl control, out CommandRejectReason reason)
            ? new ParsedControl(control, CommandRejectReason.None)
            : new ParsedControl(null, reason);
    }

    bool TryDeleteIfIdentity(string path, string identity)
    {
        try
        {
            if (!File.Exists(path))
                return true;
            string raw = AtomicFileWriter.ReadAllText(path, Encoding.UTF8);
            if (!string.Equals(raw, identity, StringComparison.Ordinal))
                return true;
            this.BeforeAcknowledgementDeleteForTests?.Invoke(path);
            File.Delete(path);
            return true;
        }
        catch (Exception)
        {
            return false; // retain this exact ack; bounded work on every later poll
        }
    }

    readonly record struct FileRead(bool Present, bool FailClosed, string? Raw)
    {
        public static FileRead Missing => new(false, false, null);
        public static FileRead Ok(string raw) => new(true, false, raw);
        public static FileRead Fail(string _) => new(true, true, null);
        public string? Identity => this.FailClosed ? "#fail" : this.Raw;
    }

    readonly record struct ParsedStop(ActionOwnerStop? Value, CommandRejectReason Reason);
    readonly record struct ParsedControl(ActionOwnerControl? Value, CommandRejectReason Reason);
}

/// <summary>
/// Snapshot imutable hasil poller. Game thread tidak membaca berkas.
/// </summary>
public sealed class ActionOwnerRecordHandoff
{
    public HaltStopRead[]? Stops { get; init; }
    public string? RearmRaw { get; init; }
    public static readonly ActionOwnerRecordHandoff Empty = new(
        0, false, null, CommandRejectReason.None, null,
        false, null, CommandRejectReason.None, null);

    public ActionOwnerRecordHandoff(
        long seq,
        bool stopPresent,
        ActionOwnerStop? stop,
        CommandRejectReason stopReason,
        string? stopIdentity,
        bool controlPresent,
        ActionOwnerControl? control,
        CommandRejectReason controlReason,
        string? controlIdentity
    )
    {
        this.Seq = seq;
        this.StopPresent = stopPresent;
        this.Stop = stop;
        this.StopReason = stopReason;
        this.StopIdentity = stopIdentity;
        this.ControlPresent = controlPresent;
        this.Control = control;
        this.ControlReason = controlReason;
        this.ControlIdentity = controlIdentity;
    }

    public long Seq { get; }
    public bool StopPresent { get; }
    public ActionOwnerStop? Stop { get; }
    public CommandRejectReason StopReason { get; }
    public string? StopIdentity { get; }
    public bool ControlPresent { get; }
    public ActionOwnerControl? Control { get; }
    public CommandRejectReason ControlReason { get; }
    public string? ControlIdentity { get; }

    public bool StopValid => this.Stop is not null && this.StopReason == CommandRejectReason.None;
    public bool ControlValid => this.Control is not null && this.ControlReason == CommandRejectReason.None;
}

public sealed record HaltStopRead(string Source, bool Present, JsonElement? Record, string? Sha256, int FaultPolls);
