using System.Text.Json;
using ArtiCore.Pure;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// G2-0 — satu native action-owner, lease, dan stop latch. Bukan transport
/// kedua: record <c>control.json</c> / <c>stop.json</c> tetap di folder
/// <see cref="MenuStepCommandInbox"/>. Jam disuntik lewat argumen
/// <c>nowUnixMs</c> supaya kedaluwarsa lease deterministik tanpa game.
///
/// Default desain (bukan hasil ukur gameplay): lease 1500 ms, petunjuk
/// renew klien 500 ms.
/// </summary>
public sealed partial class NativeActionOwner
{
    public const int LeaseDurationMs = 1500;
    public const int RenewHintMs = 500;
    /// <summary>fix54I (a39: 15/24 approach_uncertain were lease/deadline expiries at ~90% CPU): a client may size an
    /// ACQUIRE/RENEW lease from its measured hand-off latency, never below the 1500 ms default nor above this bound. The
    /// lease stays the dead-man bound; STOP, release, faults and the stuck-input hard stop keep their precedence.</summary>
    public const int MaxLeaseDurationMs = 4000;

    string boundRuntimeId = "";
    bool armed;
    string ownerId = "";
    string sessionId = "";
    long generation;
    long controlSeq;
    long leaseExpiresUnixMs;
    public long AcquiredUnixMs { get; private set; }

    bool stopLatched;
    long stopGeneration;
    bool releaseRequested;
    bool releaseAcked = true;
    bool releasing;
    long fencedGeneration;
    bool recordFaulted;
    CommandRejectReason recordFaultReason;

    public bool IsArmed => this.armed;
    public bool StopLatched => this.stopLatched;
    public bool RecordFaulted => this.recordFaulted;
    public CommandRejectReason RecordFaultReason => this.recordFaultReason;
    public bool ReleaseRequested => this.releaseRequested;
    public bool ReleaseAcked => this.releaseAcked;
    public bool IsReleasing => this.releasing;
    public string OwnerId => this.ownerId;
    public string SessionId => this.sessionId;
    public long Generation => this.generation;
    public long ControlSeq => this.controlSeq;
    public long LeaseExpiresUnixMs => this.leaseExpiresUnixMs;
    public long StopGeneration => this.stopGeneration;
    public long FencedGeneration => this.fencedGeneration;
    public string BoundRuntimeId => this.boundRuntimeId;
    public ExecutionSlot? Slot { get; set; }
    public Func<(ReceiverObserver Observer, ReceiverSurface Surface)>? ObserveReceiver { get; set; }
    public Queue<JsonElement> ClaimAnswers { get; } = new();
    public JsonElement? CancelAnswer { get; private set; }
    public Queue<JsonElement> StatusAnswers { get; } = new();
    readonly Dictionary<(string, long, long), JsonElement> claims = new();
    public string? CancelOutcome { get; private set; }

    public void BindRuntime(string runtimeInstanceId) =>
        this.boundRuntimeId = runtimeInstanceId ?? "";

    /// <summary>
    /// New command session disarms ownership; HALT and RF survive.
    /// <c>runtime_instance_id</c> tetap (mod belum unload).
    /// </summary>
    public void ResetForNewSession()
    {
        this.DisarmFenced(fenceCurrent: false);
        this.fencedGeneration = 0;
    }

    void ClearStopLatch()
    {
        this.stopLatched = false;
        this.stopGeneration = 0;
        this.releaseRequested = false;
        this.releaseAcked = true;
        this.releasing = false;
    }

    public ControlApplyResult ApplyControl(
        ActionOwnerControl control,
        CommandInboxIdentity identity,
        long nowUnixMs,
        bool mutationBusy = false,
        bool ownedInflight = false
    )
    {
        var binding = this.BindingReject(control, identity);
        if (binding != CommandRejectReason.None) return ControlApplyResult.Reject(binding);
        if (control.Envelope is { } envelope)
        {
            if (envelope.TryGetProperty("fence", out var fence))
            {
                long generation = fence.GetProperty("generation").GetInt64();
                this.fencedGeneration = Math.Max(this.fencedGeneration, generation);
                if (this.armed && this.generation <= generation)
                { this.Slot?.Cancel("lease"); this.DisarmFenced(fenceCurrent: true); }
            }
            if (envelope.TryGetProperty("status_query", out var query) && this.Slot is { } slot)
            {
                this.StatusAnswers.Enqueue(slot.AttemptStatus(query, nowUnixMs));
                while (this.StatusAnswers.Count > 8) this.StatusAnswers.Dequeue();
            }
            if (envelope.TryGetProperty("claim", out var claim))
            {
                var key = (claim.GetProperty("owner_id").GetString()!, claim.GetProperty("generation").GetInt64(), claim.GetProperty("claim_seq").GetInt64());
                if (!this.claims.TryGetValue(key, out var answer))
                {
                    string result = this.recordFaulted ? "control_record_fault" : this.stopLatched ? "stop_latched"
                        : this.ObserveReceiver is null || this.Slot is null ? "claim_stale_epoch"
                        : this.Slot.Claim(this.ObserveReceiver(), claim.GetProperty("receiver_epoch").GetInt64());
                    answer = JsonSerializer.SerializeToElement(new { owner_id = key.Item1, claim_seq = key.Item3,
                        receiver_epoch = claim.GetProperty("receiver_epoch").GetInt64(), result });
                    this.claims[key] = answer;
                }
                this.ClaimAnswers.Enqueue(answer);
                while (this.ClaimAnswers.Count > 8) this.ClaimAnswers.Dequeue();
            }
        }
        if (this.recordFaulted && control.Kind is not (ActionOwnerControlKind.Release or ActionOwnerControlKind.Cancel or ActionOwnerControlKind.None))
            return ControlApplyResult.Reject(CommandRejectReason.ControlRecordFault);
        if (this.stopLatched && control.Kind is ActionOwnerControlKind.Acquire or ActionOwnerControlKind.Renew)
            return ControlApplyResult.Reject(CommandRejectReason.StopLatched);
        if (control.Kind == ActionOwnerControlKind.None) return this.recordFaulted && control.Envelope?.TryGetProperty("claim", out _) == true
            ? ControlApplyResult.Reject(CommandRejectReason.ControlRecordFault) : ControlApplyResult.Accept(control.Kind);
        if (control.Kind == ActionOwnerControlKind.Cancel) return this.ApplyCancel(control);
        if (control.Kind == ActionOwnerControlKind.Acquire)
            return this.ApplyAcquire(control, identity, nowUnixMs, mutationBusy);
        if (control.Kind == ActionOwnerControlKind.Renew)
            return this.ApplyRenew(control, identity, nowUnixMs);
        if (control.Kind == ActionOwnerControlKind.Release)
            return this.ApplyRelease(control, identity, nowUnixMs, ownedInflight);
        return ControlApplyResult.Reject(CommandRejectReason.UnsupportedOp);
    }

    ControlApplyResult ApplyCancel(ActionOwnerControl control)
    {
        if (control.Envelope is not { } envelope || !envelope.TryGetProperty("cancel", out var cancel)) return ControlApplyResult.Reject(CommandRejectReason.MalformedJson);
        if (!this.armed || control.OwnerId != this.ownerId) return ControlApplyResult.Reject(CommandRejectReason.OwnerMismatch);
        if (control.Generation != this.generation) return ControlApplyResult.Reject(CommandRejectReason.GenerationMismatch);
        if (control.ControlSeq <= this.controlSeq) return ControlApplyResult.Reject(CommandRejectReason.StaleControlSeq);
        this.controlSeq = control.ControlSeq; // HB6: stale an unconsumed publication; never renew the lease.
        this.CancelOutcome = this.Slot?.Cancel(cancel.GetProperty("attempt_id").GetString()!,
            cancel.GetProperty("request_id").GetString()!, control.Generation, cancel.GetProperty("reason").GetString()!) == true
            ? "cancelled" : "cancel_no_active";
        this.CancelAnswer = JsonSerializer.SerializeToElement(new { control_seq = control.ControlSeq,
            request_id = cancel.GetProperty("request_id").GetString(), result = this.CancelOutcome == "cancelled" ? "applied" : "cancel_no_active" });
        return ControlApplyResult.Accept(ActionOwnerControlKind.Cancel);
    }

    public ControlApplyResult ApplyStop(
        ActionOwnerStop stop,
        CommandInboxIdentity identity,
        long nowUnixMs,
        bool hasInflight
    )
    {
        this.ClearRecordFault();
        if (stop.RuntimeInstanceId != this.boundRuntimeId || stop.SessionId != identity.SessionId)
            this.NoteHalt("stop_identity_mismatch");

        this.stopLatched = true;
        this.stopGeneration = stop.Generation;
        this.Slot?.Cancel("stop");
        if (hasInflight)
        {
            this.releaseRequested = true;
            this.releaseAcked = false;
        }
        else
        {
            this.releaseRequested = false;
            this.releaseAcked = true;
        }

        return ControlApplyResult.Accept(ActionOwnerControlKind.Stop);
    }

    /// <summary>
    /// G2-0 F5 — stop.json ada tapi tidak parseable (malformed, oversize,
    /// I/O). Bukan latch generasi: tidak ada angka yang bisa di-fence.
    /// Jangan hapus berkasnya. Mutasi/acquire/renew nol sampai handoff
    /// berikutnya membuktikan stop hilang atau diganti yang parseable.
    /// </summary>
    public void ApplyRecordFault(CommandRejectReason reason, bool hasInflight)
    {
        this.recordFaulted = true;
        this.Slot?.Cancel("stop");
        this.recordFaultReason = reason == CommandRejectReason.None
            ? CommandRejectReason.MalformedJson
            : reason;
        if (hasInflight)
        {
            this.releaseRequested = true;
            this.releaseAcked = false;
        }
        else if (!this.releaseRequested)
        {
            this.releaseAcked = true;
        }
    }

    void ClearRecordFault()
    {
        this.recordFaulted = false;
        this.recordFaultReason = CommandRejectReason.None;
    }

    /// <summary>
    /// Game-thread stop admission precedes control/request/Press. R0-03 HALT
    /// persistence and the clear-time stat are the explicit game-thread I/O boundary.
    /// </summary>
    public RecordHandoffConsumption ConsumeRecordHandoff(
        ActionOwnerRecordHandoff snap,
        CommandInboxIdentity identity,
        long nowUnixMs,
        bool mutationInflight
    )
    {
        if (snap.Stops is not null)
            this.ConsumeHalt(snap, identity, nowUnixMs, mutationInflight);
        if (snap.Stops is null && snap.StopPresent && !snap.StopValid)
        {
            this.ApplyRecordFault(snap.StopReason, mutationInflight);
            return new RecordHandoffConsumption(
                DeleteStop: false,
                DeleteControl: snap.ControlValid,
                StopApplied: ControlApplyResult.Reject(CommandRejectReason.ControlRecordFault),
                ControlApplied: snap.ControlValid
                    ? this.ApplyControl(snap.Control!.Value, identity, nowUnixMs, ownedInflight: mutationInflight)
                    : snap.ControlPresent ? ControlApplyResult.Reject(CommandRejectReason.ControlRecordFault)
                    : default);
        }

        ControlApplyResult stopApplied = default;
        if (snap.Stops is null && snap.StopValid)
        {
            stopApplied = this.ApplyStop(
                snap.Stop!.Value, identity, nowUnixMs, mutationInflight);
        }
        else if (snap.Stops is null)
        {
            this.ClearRecordFault();
        }

        ControlApplyResult controlApplied = default;
        bool deleteControl = false;
        if (snap.ControlValid)
        {
            controlApplied = this.ApplyControl(
                snap.Control!.Value,
                identity,
                nowUnixMs,
                mutationBusy: mutationInflight && !this.armed,
                ownedInflight: mutationInflight);
            deleteControl = true;
        }
        else if (snap.ControlPresent)
        {
            deleteControl = false; // malformed control is retained; renewal loss expires the lease
        }

        return new RecordHandoffConsumption(
            false, deleteControl, stopApplied, controlApplied);
    }

    ControlApplyResult ApplyAcquire(
        ActionOwnerControl control,
        CommandInboxIdentity identity,
        long nowUnixMs,
        bool mutationBusy
    )
    {
        CommandRejectReason binding = BindingReject(control, identity);
        if (binding != CommandRejectReason.None)
            return ControlApplyResult.Reject(binding);

        if (identity.IsMainPlayer || identity.IsSplitScreen)
            return ControlApplyResult.Reject(CommandRejectReason.PlayerMismatch);

        if (this.releasing)
            return ControlApplyResult.Reject(CommandRejectReason.CommandBusy);

        if (this.stopLatched)
            return ControlApplyResult.Reject(CommandRejectReason.StopLatched);

        bool sameAcquire = this.armed
            && string.Equals(control.OwnerId, this.ownerId, StringComparison.Ordinal)
            && control.Generation == this.generation
            && control.ControlSeq == this.controlSeq;
        if (sameAcquire)
            return ControlApplyResult.Accept(ActionOwnerControlKind.Acquire);

        if (control.Generation <= this.fencedGeneration)
            return ControlApplyResult.Reject(CommandRejectReason.GenerationMismatch);

        if (this.armed)
            return ControlApplyResult.Reject(CommandRejectReason.CommandBusy);

        if (mutationBusy)
            return ControlApplyResult.Reject(CommandRejectReason.CommandBusy);

        this.armed = true;
        this.AcquiredUnixMs = nowUnixMs;
        this.ownerId = control.OwnerId;
        this.sessionId = control.SessionId;
        this.generation = control.Generation;
        this.controlSeq = control.ControlSeq;
        this.leaseExpiresUnixMs = nowUnixMs + (control.LeaseMs ?? LeaseDurationMs);
        return ControlApplyResult.Accept(ActionOwnerControlKind.Acquire);
    }

    ControlApplyResult ApplyRenew(
        ActionOwnerControl control,
        CommandInboxIdentity identity,
        long nowUnixMs
    )
    {
        CommandRejectReason binding = BindingReject(control, identity);
        if (binding != CommandRejectReason.None)
            return ControlApplyResult.Reject(binding);

        if (!this.armed
            || !string.Equals(control.OwnerId, this.ownerId, StringComparison.Ordinal))
        {
            return ControlApplyResult.Reject(CommandRejectReason.OwnerMismatch);
        }

        if (control.Generation != this.generation)
            return ControlApplyResult.Reject(CommandRejectReason.GenerationMismatch);

        if (control.ControlSeq <= this.controlSeq)
            return ControlApplyResult.Reject(CommandRejectReason.StaleControlSeq);

        if (this.stopLatched)
            return ControlApplyResult.Reject(CommandRejectReason.StopLatched);

        if (this.releasing)
            return ControlApplyResult.Reject(CommandRejectReason.CommandBusy);

        if (nowUnixMs > this.leaseExpiresUnixMs)
            return ControlApplyResult.Reject(CommandRejectReason.LeaseExpired);

        this.controlSeq = control.ControlSeq;
        this.leaseExpiresUnixMs = nowUnixMs + (control.LeaseMs ?? LeaseDurationMs);
        return ControlApplyResult.Accept(ActionOwnerControlKind.Renew);
    }

    ControlApplyResult ApplyRelease(
        ActionOwnerControl control,
        CommandInboxIdentity identity,
        long nowUnixMs,
        bool ownedInflight
    )
    {
        _ = nowUnixMs;
        CommandRejectReason binding = BindingReject(control, identity);
        if (binding != CommandRejectReason.None)
            return ControlApplyResult.Reject(binding);

        if (!this.armed
            || !string.Equals(control.OwnerId, this.ownerId, StringComparison.Ordinal)
            || control.Generation != this.generation)
        {
            return ControlApplyResult.Reject(CommandRejectReason.OwnerMismatch);
        }

        if (control.ControlSeq <= this.controlSeq)
            return ControlApplyResult.Reject(CommandRejectReason.StaleControlSeq);

        this.controlSeq = control.ControlSeq;
        this.fencedGeneration = Math.Max(this.fencedGeneration, this.generation);
        if (this.SlotHeld(ownedInflight))
        {
            // F3: jangan drop ownership sebelum netral. Mutasi kedua ditolak
            // sampai AckOwnedRelease.
            this.releasing = true;
            this.releaseRequested = true;
            this.releaseAcked = false;
            return ControlApplyResult.Accept(ActionOwnerControlKind.Release);
        }

        this.DisarmFenced(fenceCurrent: true);
        return ControlApplyResult.Accept(ActionOwnerControlKind.Release);
    }

    CommandRejectReason BindingReject(ActionOwnerControl control, CommandInboxIdentity identity)
    {
        if (this.boundRuntimeId.Length > 0
            && !string.Equals(control.RuntimeInstanceId, this.boundRuntimeId, StringComparison.Ordinal))
        {
            return CommandRejectReason.RuntimeMismatch;
        }

        if (identity.SessionId.Length > 0
            && !string.Equals(control.SessionId, identity.SessionId, StringComparison.Ordinal))
        {
            return CommandRejectReason.SessionMismatch;
        }

        if (control.ExpectedPlayerId is long wantedId && wantedId != identity.PlayerId)
            return CommandRejectReason.PlayerMismatch;
        if (control.ExpectedPlayerName is { Length: > 0 } wantedName
            && !string.Equals(wantedName, identity.PlayerName, StringComparison.Ordinal))
        {
            return CommandRejectReason.PlayerMismatch;
        }

        if (control.ExpectedScreenId is int wantedScreen && wantedScreen != identity.ScreenId)
            return CommandRejectReason.ScreenMismatch;

        return CommandRejectReason.None;
    }

    public bool CanAdmitInboxMutation(
        CommandInboxIdentity identity,
        long nowUnixMs,
        out CommandRejectReason reason
    )
    {
        if (identity.IsMainPlayer || identity.IsSplitScreen)
        {
            reason = CommandRejectReason.PlayerMismatch;
            return false;
        }

        if (this.recordFaulted)
        {
            reason = CommandRejectReason.ControlRecordFault;
            return false;
        }

        if (this.stopLatched)
        {
            reason = CommandRejectReason.StopLatched;
            return false;
        }

        if (this.releasing)
        {
            reason = CommandRejectReason.CommandBusy;
            return false;
        }

        // F4: lease generic tidak memberkati request.json lama tanpa bukti
        // owner/generation/control. inspect_* tidak memanggil ini.
        if (this.armed)
        {
            if (!string.Equals(identity.SessionId, this.sessionId, StringComparison.Ordinal))
            {
                reason = CommandRejectReason.SessionMismatch;
                return false;
            }

            reason = nowUnixMs > this.leaseExpiresUnixMs
                ? CommandRejectReason.LeaseExpired
                : CommandRejectReason.CommandBusy;
            return false;
        }

        reason = CommandRejectReason.None;
        return true;
    }

    /// <summary>
    /// G2 typed leased-mutation proof. Ini sengaja action-agnostic: owner,
    /// runtime, generation, control sequence, stop/fault/release dan expiry
    /// adalah kontrak ownership yang sama untuk setiap primitive mutating yang
    /// sudah punya envelope typed. Binding player/screen/location/context tetap
    /// diverifikasi oleh command inbox sebelum slot dipegang.
    /// </summary>
    public bool CanAdmitLeasedMutation(
        CommandInboxIdentity identity,
        long nowUnixMs,
        string runtimeInstanceId,
        string ownerId,
        long generation,
        long controlSeq,
        out CommandRejectReason reason
    )
    {
        if (identity.IsMainPlayer || identity.IsSplitScreen)
        {
            reason = CommandRejectReason.PlayerMismatch;
            return false;
        }

        if (this.recordFaulted)
        {
            reason = CommandRejectReason.ControlRecordFault;
            return false;
        }

        if (this.stopLatched)
        {
            // T03-27: an explicitly fenced publication stays generation_fenced under HALT.
            reason = generation <= this.fencedGeneration ? CommandRejectReason.GenerationMismatch : CommandRejectReason.StopLatched;
            return false;
        }

        if (this.releasing)
        {
            reason = CommandRejectReason.CommandBusy;
            return false;
        }

        if (generation <= this.fencedGeneration)
        {
            reason = CommandRejectReason.GenerationMismatch;
            return false;
        }

        if (!this.armed)
        {
            reason = CommandRejectReason.CommandBusy;
            return false;
        }

        if (nowUnixMs > this.leaseExpiresUnixMs)
        {
            reason = CommandRejectReason.LeaseExpired;
            return false;
        }

        if (this.boundRuntimeId.Length > 0
            && !string.Equals(runtimeInstanceId, this.boundRuntimeId, StringComparison.Ordinal))
        {
            reason = CommandRejectReason.RuntimeMismatch;
            return false;
        }

        if (!string.Equals(identity.SessionId, this.sessionId, StringComparison.Ordinal))
        {
            reason = CommandRejectReason.SessionMismatch;
            return false;
        }

        if (!string.Equals(ownerId, this.ownerId, StringComparison.Ordinal))
        {
            reason = CommandRejectReason.OwnerMismatch;
            return false;
        }

        if (generation != this.generation)
        {
            reason = CommandRejectReason.GenerationMismatch;
            return false;
        }

        if (controlSeq != this.controlSeq)
        {
            reason = CommandRejectReason.StaleControlSeq;
            return false;
        }

        reason = CommandRejectReason.None;
        return true;
    }

    /// <summary>
    /// Nama G2-1 lama dipertahankan sebagai wrapper kompatibilitas. Tidak ada
    /// owner atau aturan lease kedua khusus input_once.
    /// </summary>
    public bool CanAdmitLeasedInputOnce(
        CommandInboxIdentity identity,
        long nowUnixMs,
        string runtimeInstanceId,
        string ownerId,
        long generation,
        long controlSeq,
        out CommandRejectReason reason
    ) => this.CanAdmitLeasedMutation(
        identity,
        nowUnixMs,
        runtimeInstanceId,
        ownerId,
        generation,
        controlSeq,
        out reason);

    public MutationAdvice Advise(long nowUnixMs, bool ownedInflight)
    {
        this.AdvanceExpiry(nowUnixMs, ownedInflight);

        if (this.recordFaulted)
        {
            if (ownedInflight && !this.releaseAcked)
                this.releaseRequested = true;
            bool expiredWhileFault = this.armed && nowUnixMs > this.leaseExpiresUnixMs;
            return new MutationAdvice(
                AllowNewMutation: false,
                RequestOwnedRelease: !this.releaseAcked || ownedInflight,
                StopLatched: this.stopLatched,
                LeaseHeld: this.armed && !expiredWhileFault,
                LeaseExpired: expiredWhileFault,
                RejectReason: CommandRejectReason.ControlRecordFault.ToString(),
                ReleaseReason: CommandRejectReason.ControlRecordFault.ToString());
        }

        if (this.stopLatched)
        {
            if (ownedInflight && !this.releaseAcked)
                this.releaseRequested = true;
            bool expiredWhileStop = this.armed && nowUnixMs > this.leaseExpiresUnixMs;
            return new MutationAdvice(
                AllowNewMutation: false,
                RequestOwnedRelease: !this.releaseAcked || ownedInflight,
                StopLatched: true,
                LeaseHeld: this.armed && !expiredWhileStop,
                LeaseExpired: expiredWhileStop,
                RejectReason: CommandRejectReason.StopLatched.ToString(),
                ReleaseReason: CommandRejectReason.StopLatched.ToString());
        }

        if (this.releasing)
        {
            bool expired = this.armed && nowUnixMs > this.leaseExpiresUnixMs;
            string why = expired
                ? CommandRejectReason.LeaseExpired.ToString()
                : CommandRejectReason.CommandBusy.ToString();
            if (ownedInflight)
                this.releaseRequested = true;
            return new MutationAdvice(
                AllowNewMutation: false,
                RequestOwnedRelease: ownedInflight || !this.releaseAcked,
                StopLatched: false,
                LeaseHeld: false,
                LeaseExpired: expired,
                RejectReason: why,
                ReleaseReason: why);
        }

        if (this.armed && !ownedInflight)
        {
            return new MutationAdvice(
                AllowNewMutation: false,
                RequestOwnedRelease: false,
                StopLatched: false,
                LeaseHeld: true,
                LeaseExpired: false,
                RejectReason: CommandRejectReason.CommandBusy.ToString(),
                ReleaseReason: null);
        }

        return new MutationAdvice(
            AllowNewMutation: !this.armed || ownedInflight,
            RequestOwnedRelease: false,
            StopLatched: false,
            LeaseHeld: this.armed,
            LeaseExpired: false,
            RejectReason: null,
            ReleaseReason: null);
    }

    public void AckOwnedRelease()
    {
        this.releaseRequested = false;
        this.releaseAcked = true;
        if (this.releasing && !this.stopLatched)
            this.DisarmFenced(fenceCurrent: true);
    }

    /// <summary>
    /// F2: kedaluwarsa adalah deadman. Tanpa inflight, owner langsung
    /// DISARMED + fenced. Dengan inflight, minta netral dulu.
    /// </summary>
    void AdvanceExpiry(long nowUnixMs, bool ownedInflight)
    {
        if (!this.armed || nowUnixMs <= this.leaseExpiresUnixMs)
            return;

        this.fencedGeneration = Math.Max(this.fencedGeneration, this.generation);
        if (this.SlotHeld(ownedInflight) || this.releasing)
        {
            this.releasing = true;
            this.releaseRequested = true;
            this.releaseAcked = false;
            return;
        }

        this.DisarmFenced(fenceCurrent: true);
    }

    // PM-1 fix10c (a12 N:2834): the slot is the one source of "input held". Advise callers pass their own
    // notion (world-press probe: inbox inflight; use-tool: its cleanup), so whichever saw the lease deadline
    // first decided. A slot-blind caller disarmed the owner past a finished but unreleased move: the attempt
    // had no receipt and no cancel, and nothing could ever retire it. The lease now retires only through the slot.
    bool SlotHeld(bool callerOwned) => callerOwned
        || this.Slot?.InputBusy == true && this.Slot.ActiveAttempt?.Client != ExecutionSlot.BodyGuardClient;

    void DisarmFenced(bool fenceCurrent)
    {
        if (fenceCurrent && this.generation > 0)
            this.fencedGeneration = Math.Max(this.fencedGeneration, this.generation);
        this.armed = false;
        this.ownerId = "";
        this.sessionId = "";
        this.generation = 0;
        this.controlSeq = 0;
        this.leaseExpiresUnixMs = 0;
        this.releasing = false;
        this.releaseRequested = false;
        this.releaseAcked = true;
    }
}

public readonly record struct RecordHandoffConsumption(
    bool DeleteStop,
    bool DeleteControl,
    ControlApplyResult StopApplied,
    ControlApplyResult ControlApplied
);

public readonly record struct MutationAdvice(
    bool AllowNewMutation,
    bool RequestOwnedRelease,
    bool StopLatched,
    bool LeaseHeld,
    bool LeaseExpired,
    string? RejectReason,
    string? ReleaseReason
);

public readonly record struct ControlApplyResult(
    bool Accepted,
    CommandRejectReason Reason,
    ActionOwnerControlKind Kind,
    bool IgnoredOldGeneration
)
{
    public static ControlApplyResult Accept(ActionOwnerControlKind kind) =>
        new(true, CommandRejectReason.None, kind, false);

    public static ControlApplyResult Reject(CommandRejectReason reason) =>
        new(false, reason, ActionOwnerControlKind.None, false);

    public static ControlApplyResult IgnoreOldGeneration() =>
        new(false, CommandRejectReason.GenerationMismatch, ActionOwnerControlKind.Stop, true);
}

public enum ActionOwnerControlKind
{
    None,
    Acquire,
    Renew,
    Release,
    Stop,
    Cancel,
}

public readonly record struct ActionOwnerControl(
    ActionOwnerControlKind Kind,
    string RuntimeInstanceId,
    string SessionId,
    string OwnerId,
    long Generation,
    long ControlSeq,
    long? ExpectedPlayerId = null,
    string? ExpectedPlayerName = null,
    int? ExpectedScreenId = null,
    JsonElement? Envelope = null,
    long? LeaseMs = null
)
{
    public static bool TryParse(
        string? raw,
        out ActionOwnerControl control,
        out CommandRejectReason reason
    )
    {
        control = default;
        reason = CommandRejectReason.MalformedJson;
        if (string.IsNullOrWhiteSpace(raw) || raw.Length > MenuStepCommandInbox.MaxRequestBytes)
            return false;

        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(raw);
        }
        catch (JsonException)
        {
            return false;
        }

        using (doc)
        {
            JsonElement root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return false;

            if (!TryInt(root, "protocol", out int protocol)
                || protocol != MenuStepCommandInbox.ProtocolVersion)
            {
                reason = CommandRejectReason.UnsupportedProtocol;
                return false;
            }

            string kindRaw = TryString(root, "kind") ?? "";
            ActionOwnerControlKind kind = kindRaw switch
            {
                "acquire" => ActionOwnerControlKind.Acquire,
                "renew" => ActionOwnerControlKind.Renew,
                "release" => ActionOwnerControlKind.Release,
                "cancel" => ActionOwnerControlKind.Cancel,
                _ => ActionOwnerControlKind.None,
            };
            if (kind == ActionOwnerControlKind.None && kindRaw != "none")
            {
                reason = CommandRejectReason.UnsupportedOp;
                return false;
            }

            string runtime = TryString(root, "runtime_instance_id") ?? "";
            string session = TryString(root, "session_id") ?? "";
            string owner = TryString(root, "owner_id") ?? "";
            if (runtime.Length == 0 || runtime.Length > MenuStepCommandInbox.MaxRequestIdLength
                || session.Length == 0 || session.Length > MenuStepCommandInbox.MaxRequestIdLength
                || owner.Length == 0 || owner.Length > MenuStepCommandInbox.MaxRequestIdLength)
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }

            if (!TryInt64(root, "generation", out long generation) || generation <= 0)
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }

            if (!TryInt64(root, "control_seq", out long seq) || seq < (kind == ActionOwnerControlKind.None ? 0 : 1))
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }

            long? playerId = TryInt64(root, "expected_player_id", out long pid) ? pid : null;
            string? playerName = TryString(root, "expected_player_name");
            int? screenId = TryInt(root, "expected_screen_id", out int sid) ? sid : null;

            if (!ValidAuxiliary(root, kind, owner, generation)) return false;
            long? leaseMs = null;
            if (root.TryGetProperty("lease_ms", out _))
            {
                // Out-of-range or non-integer is malformed, never silently clamped into a different promise.
                if (kind is not (ActionOwnerControlKind.Acquire or ActionOwnerControlKind.Renew)
                    || !TryInt64(root, "lease_ms", out long requested)
                    || requested < NativeActionOwner.LeaseDurationMs || requested > NativeActionOwner.MaxLeaseDurationMs)
                    return false;
                leaseMs = requested;
            }
            control = new ActionOwnerControl(
                kind, runtime, session, owner, generation, seq, playerId, playerName, screenId, root.Clone(), leaseMs);
            reason = CommandRejectReason.None;
            return true;
        }
    }

    static bool ValidAuxiliary(JsonElement root, ActionOwnerControlKind kind, string owner, long generation)
    {
        bool Id(JsonElement o, string name) => TryString(o, name) is { Length: > 0 and <= 128 };
        bool Positive(JsonElement o, string name) => TryInt64(o, name, out long n) && n > 0;
        foreach (string name in new[] { "cancel", "claim", "fence", "status_query" })
        {
            if (!root.TryGetProperty(name, out var value)) { if (name == "cancel" && kind == ActionOwnerControlKind.Cancel) return false; continue; }
            if (value.ValueKind != JsonValueKind.Object || !Positive(value, "generation")) return false;
            if (name == "status_query")
            {
                if (!Id(value, "query_id") || !Id(value, "runtime_instance_id") || !Id(value, "session_id") || !Id(value, "owner_id")
                    || !Id(value, "request_id") || !Positive(value, "request_seq") || TryString(value, "payload_sha256") is not { Length: 64 } digest
                    || digest.Any(c => !Uri.IsHexDigit(c))) return false;
            }
            else if (value.GetProperty("generation").GetInt64() != generation) return false;
            if (name is "claim" or "fence" && TryString(value, "owner_id") != owner) return false;
            if (name == "claim" && (!TryInt64(value, "receiver_epoch", out long epoch) || epoch < 0 || !Positive(value, "claim_seq"))) return false;
            if (name == "cancel" && (kind != ActionOwnerControlKind.Cancel || !Id(value, "attempt_id") || !Id(value, "request_id")
                || TryString(value, "reason") is not ("hazard" or "owner_stop" or "interrupt" or "supersede" or "deadline" or "shutdown"))) return false;
        }
        return true;
    }

    static string? TryString(JsonElement root, string name) =>
        root.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString()
            : null;

    static bool TryInt(JsonElement root, string name, out int value)
    {
        value = 0;
        return root.TryGetProperty(name, out JsonElement el)
            && el.ValueKind == JsonValueKind.Number
            && el.TryGetInt32(out value);
    }

    static bool TryInt64(JsonElement root, string name, out long value)
    {
        value = 0;
        return root.TryGetProperty(name, out JsonElement el)
            && el.ValueKind == JsonValueKind.Number
            && el.TryGetInt64(out value);
    }
}

public readonly record struct ActionOwnerStop(
    string RuntimeInstanceId,
    string SessionId,
    string OwnerId,
    long Generation,
    long ControlSeq
)
{
    public static bool TryParse(
        string? raw,
        out ActionOwnerStop stop,
        out CommandRejectReason reason
    )
    {
        stop = default;
        reason = CommandRejectReason.MalformedJson;
        if (string.IsNullOrWhiteSpace(raw) || raw.Length > MenuStepCommandInbox.MaxRequestBytes)
            return false;

        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(raw);
        }
        catch (JsonException)
        {
            return false;
        }

        using (doc)
        {
            JsonElement root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return false;

            if (!TryInt(root, "protocol", out int protocol)
                || protocol != MenuStepCommandInbox.ProtocolVersion)
            {
                reason = CommandRejectReason.UnsupportedProtocol;
                return false;
            }

            string runtime = TryString(root, "runtime_instance_id") ?? "";
            string session = TryString(root, "session_id") ?? "";
            string owner = TryString(root, "owner_id") ?? "";
            if (runtime.Length == 0 || runtime.Length > MenuStepCommandInbox.MaxRequestIdLength
                || session.Length == 0 || session.Length > MenuStepCommandInbox.MaxRequestIdLength)
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }

            if (owner.Length > MenuStepCommandInbox.MaxRequestIdLength)
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }

            if (!TryInt64(root, "generation", out long generation) || generation <= 0)
            {
                reason = CommandRejectReason.MalformedJson;
                return false;
            }

            long seq = TryInt64(root, "control_seq", out long parsedSeq) ? parsedSeq : 0;
            stop = new ActionOwnerStop(runtime, session, owner, generation, seq);
            reason = CommandRejectReason.None;
            return true;
        }
    }

    static string? TryString(JsonElement root, string name) =>
        root.TryGetProperty(name, out JsonElement el) && el.ValueKind == JsonValueKind.String
            ? el.GetString()
            : null;

    static bool TryInt(JsonElement root, string name, out int value)
    {
        value = 0;
        return root.TryGetProperty(name, out JsonElement el)
            && el.ValueKind == JsonValueKind.Number
            && el.TryGetInt32(out value);
    }

    static bool TryInt64(JsonElement root, string name, out long value)
    {
        value = 0;
        return root.TryGetProperty(name, out JsonElement el)
            && el.ValueKind == JsonValueKind.Number
            && el.TryGetInt64(out value);
    }
}
