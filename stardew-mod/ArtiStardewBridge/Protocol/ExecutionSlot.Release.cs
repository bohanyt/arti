using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

public sealed partial class ExecutionSlot
{
    public ulong ModTick { get; private set; }
    long previousMs;
    public Func<ActiveAttempt, NativeClosureFacts?>? ObserveClosure { get; set; }
    public Action<ActiveAttempt, bool>? ObserveProgressEdge { get; set; }
    public JsonElement? CoreReceipt { get; private set; }
    public JsonElement? ProgressionReceipt { get; private set; }
    public JsonElement? GuardReceipt { get; private set; }
    public JsonElement? FestivalReceipt { get; private set; }
    public object InputIdle => new { core = CoreReceipt, progression = ProgressionReceipt };
    // B1 subscribes; it assigns occ_seq and persists the complete record in its existing ring.
    public event Action<JsonElement>? OpClosed;
    public Action<MenuStepCommandResult?>? ReceiptChanged { get; set; }

    // First statement of UpdateTicked. No frame arithmetic, including during a blackout.
    public void AdvanceTick(long nowMs)
    {
        ++this.ModTick;
        long fromMs = this.previousMs; this.previousMs = nowMs;
        // PM-1 fix14e (CRT-06): a lapse is an unannounced gap while the attempt's input could be held. An attempt
        // admitted inside the bridge's own UpdateTicked handler holds nothing until that handler returns, so its gap
        // starts there, not at the previous tick (a22 d6:J968: a 13.9 MB journal persist at admission was the "gap").
        if (this.ActiveAttempt is { Receipt: null, ArmedMs: { } armed } && armed > fromMs) fromMs = armed;
        this.tickedHandlerOpen = true;
        if (fromMs != 0 && nowMs - fromMs > 250 && this.ActiveAttempt is { Receipt: null })
            this.Neutralize(nowMs, "tick_lapse", new { from_tick = ModTick - 1, from_ms = fromMs, to_ms = nowMs });
        if (this.ActiveAttempt is { } active) {
            // PM-1 fix10c (a12): a leased attempt never outlives its lease. If its owner retired by any route
            // without cancelling it, cancel here so it neutralizes to a receipt; never a silent "running" attempt.
            if (active.UsesLease && active.CancelReason is null && active.Receipt is null
                && this.admittedOwner is { IsArmed: false }) this.Cancel("owner_retired");
            if (active.CancelReason is not null && active.Receipt is null) this.Neutralize(nowMs);
            this.RefreshClosure(active);
            if (active.Receipt is { } receipt && (!active.UsesLease || this.admittedOwner?.ReleaseAcked == true))
                this.TryRelease(active, new(active.AttemptId, active.Request.RequestId, ModTick,
                    receipt.GetProperty("frame").GetInt64(), nowMs, true), this.admittedOwner, out _);
            if (ReferenceEquals(this.ActiveAttempt, active)) this.BoundRetained(active);
        }
        if (this.ActiveAttempt is null) { this.retained = null; this.StuckReason = null; }
    }

    bool tickedHandlerOpen;
    /// <summary>Last statement of the bridge's UpdateTicked handler (also on exceptions). No game update ran inside
    /// it, so nothing an attempt admitted there could hold was consumed before now; arm its lapse clock here.</summary>
    public void EndTickedHandler(long nowMs)
    {
        if (this.tickedHandlerOpen && this.ActiveAttempt is { Receipt: null, ArmedMs: null, AdmittedInTicked: true } a)
            a.ArmedMs = nowMs;
        this.tickedHandlerOpen = false;
    }

    // PM-1 a10 d1:J1316-3907: a finished move stayed the active attempt (input_busy) until the night reset, 1,164
    // slot_busy refusals. A retained attempt ends or names its blocker within RetainedBoundTicks. It is freed only
    // when native proves it inert: an idle release receipt, its owner retired and acked, and no cleanup term held.
    public const ulong RetainedBoundTicks = 600;
    ulong retainedSince;
    ActiveAttempt? retained;
    public string? StuckReason { get; private set; }
    public Action<string>? StuckObserved { get; set; }
    void BoundRetained(ActiveAttempt a)
    {
        // A running attempt (no receipt, not cancelled) and native progression keep their own bounded lifecycles.
        if (a.Client == "progression" || a.Receipt is null && a.CancelReason is null) { this.retained = null; return; }
        if (!ReferenceEquals(this.retained, a)) { this.retained = a; this.retainedSince = ModTick; return; }
        if (ModTick - this.retainedSince < RetainedBoundTicks) return;
        bool idle = a.Receipt is { } r && r.TryGetProperty("idle", out var i) && i.ValueKind == JsonValueKind.True;
        bool retired = !a.UsesLease || this.admittedOwner is { IsArmed: false, ReleaseAcked: true };
        bool neutral = this.CleanupPending?.Invoke() != true && a.Clients.Values.All(c => c.Busy?.Invoke() != true);
        string reason = !idle ? "no_idle_receipt" : !retired ? "owner_not_retired" : !neutral ? "cleanup_pending"
            : "release_reconcile_timeout";
        if (reason != this.StuckReason) { this.StuckReason = reason; this.StuckObserved?.Invoke(reason + " attempt=" + a.AttemptId); }
        if (reason != "release_reconcile_timeout") return; // never free while input or cleanup may be held
        this.ActiveAttempt = null; this.admittedOwner = null; this.retained = null;
    }

    public void Neutralize(long nowMs, string via = "neutralize", object? lapse = null)
    {
        if (this.ActiveAttempt is not { } a) return;
        a.ReleaseVia = via; a.ReleaseLapse = lapse is null ? null : JsonSerializer.SerializeToElement(lapse);
        this.Cancel("barrier");
        if (a.Receipt is not null) return; // cancellation can synchronously finish a native client
        // Cancellation alone is never release proof. Failed cleanup leaves W1 unsatisfied.
        if (this.CleanupPending?.Invoke() == true || a.Clients.Values.Any(c => c.Busy?.Invoke() == true)) return;
        var f = this.ObserveClosure?.Invoke(a);
        if (f is null || f.RuntimeId != a.Request.RuntimeInstanceId) return;
        var t = new AttemptTerminal(a.AttemptId, a.Request.RequestId, ModTick, f.Frame, nowMs, true, a.Client, via,
            lapse is null ? null : JsonSerializer.SerializeToElement(lapse), Fault: a.ProgressionFault);
        if (a.Client != "progression" && CommandSettledJournal.IsJournalRequest(a.Request)
            && !this.journal.TryRecordTerminal(a.Request, t, out _)) return;
        this.RecordReceipt(a, t);
    }

    void RecordReceipt(ActiveAttempt a, AttemptTerminal t)
    {
        if (a.Receipt is not null) return;
        if (!a.ReadOnly && this.ObserveClosure?.Invoke(a) is { UsingTool: true } f && f.RuntimeId == a.Request.RuntimeInstanceId) a.Markers.Add("M-T");
        a.Receipt = (t with { Client = a.Client }).Receipt;
        // B3: progression releases input, preserving the core lease.
        if (a.Client == "progression") this.admittedOwner?.AckOwnedRelease();
        // ADR 181: the body guard's receipt is its own; the runtime's release proof reads CoreReceipt.
        if (a.Client == "progression") ProgressionReceipt = a.Receipt;
        else if (a.Client == BodyGuardClient) GuardReceipt = a.Receipt;
        // Festival attempts also keep their receipt separate from the runtime's input_idle.core.
        else if (a.Client == FestivalClient) FestivalReceipt = a.Receipt;
        else CoreReceipt = a.Receipt;
        this.RefreshClosure(a); this.ReceiptChanged?.Invoke(null);
    }

    public void Bracket(Action consume)
    {
        var attempt = this.BeginConsumption(); bool completed = false;
        try { consume(); completed = true; }
        finally { this.EndConsumption(attempt, completed); }
    }

    public ActiveAttempt? BeginConsumption()
    {
        var a = this.ActiveAttempt;
        if (a is null) return null;
        if (a.BracketDepth++ == 0) {
            this.ObserveProgressEdge?.Invoke(a, true);
            a.BeforeConsumption = this.ObserveClosure?.Invoke(a);
            if (a.AnswerWindow is { } answer && this.ObserveController?.Invoke(a.Request, "single_edge") is { } frame)
                answer.Begin(frame.Surface, frame.CanReceiveInput, frame.PageIndex, frame.Profession);
        }
        return a;
    }
    public void EndConsumption(ActiveAttempt? a, bool completed)
    {
        if (a is null || a.BracketDepth <= 0) return;
        a.BracketFailed |= !completed;
        if (--a.BracketDepth != 0) return;
        this.ObserveProgressEdge?.Invoke(a, false);
        var f = this.ObserveClosure?.Invoke(a); var b = a.BeforeConsumption;
        a.BracketClosed = !a.BracketFailed && f is not null;
        if (a.Delivered) a.FirstEdgeTick ??= this.ModTick;
        if (!a.ReadOnly && b is not null && f is not null && b.RuntimeId == f.RuntimeId) {
            if (b.ProposalsReadable && f.ProposalsReadable && b.Outgoing is null && f.Outgoing is not null)
            { a.Markers.Add("M-P"); a.SendFrame = f.Frame; }
            if (!b.WarpPending && f.WarpPending) a.Markers.Add("M-W");
            a.Incoming ??= b.Incoming;
            if (b.ReceiverPresences is not null && f.ReceiverPresences is not null)
                a.OpenedPresenceIds.UnionWith(f.ReceiverPresences.Except(b.ReceiverPresences));
        }
        if (a.AnswerWindow is { } answer && this.ObserveController?.Invoke(a.Request, "single_edge") is { } frame)
            a.AnswerWitness ??= answer.End(frame.Surface, frame.CanReceiveInput, frame.PageIndex, a.Delivered,
                !a.BracketFailed, frame.QuestionKey, frame.Profession);
        this.RefreshClosure(a);
        if (a.PendingResult is { } result) {
            a.PendingResult = null; this.ReceiptChanged?.Invoke(this.FinishResult(a, result));
        }
    }

    public void PublishResult(MenuStepCommandResult result)
    {
        if (this.ActiveAttempt is { } a && a.Request.RequestId == result.RequestId) {
            if (a.BracketDepth > 0) { a.PendingResult = result; return; }
            result = this.FinishResult(a, result);
        }
        this.ReceiptChanged?.Invoke(result);
    }

    // May also be called with a retained ticket after release (e.g. a late debt grant).
    public JsonElement RefreshClosure(ActiveAttempt a)
    {
        var f = this.ObserveClosure?.Invoke(a); var witnesses = new HashSet<string>();
        bool readable = f is not null && f.RuntimeId == a.Request.RuntimeInstanceId;
        if (readable && f!.DebtPending) a.Markers.Add("M-D");
        if (a.Receipt is { } r && r.GetProperty("idle").GetBoolean()) {
            witnesses.Add("CK-R");
            if (readable && f!.Frame > r.GetProperty("frame").GetInt64()) {
                if (!f.DebtPending) { witnesses.Add("CK-D"); if (!f.UsingTool) witnesses.Add("CK-T"); }
            }
        }
        if (a.BracketClosed && a.BracketDepth == 0) witnesses.Add("CK-B");
        if (readable) {
            bool receiverPending = f!.AdoptedReceiverPending || f.ReceiverPresences is null
                || a.OpenedPresenceIds.Overlaps(f.ReceiverPresences);
            if (a.Markers.Contains("M-W") && !f!.WarpPending) witnesses.Add("CK-W");
            if (f!.ControllerTerminal && !receiverPending) witnesses.Add("CK-C");
            if (a.SendFrame is { } send && f.ProposalsReadable && f.Outgoing is null && f.Frame > send) witnesses.Add("CK-P");
            if (a.Incoming is not null && f.ProposalsReadable && f.Incoming != a.Incoming && !receiverPending
                && witnesses.Contains("CK-B")) witnesses.Add("CK-I");
        }
        var required = a.Validity is { } validity && a.Request.Intent is { } intent
            ? VerbRules.Row(intent.GetProperty("verb").GetString()!, validity.RowKey).GetProperty("closure")
                .EnumerateArray().Select(x => x.GetString()!).ToHashSet()
            : new HashSet<string>();
        foreach (string marker in a.Markers) required.Add(marker switch { "M-P" => "CK-P", "M-D" => "CK-D", "M-W" => "CK-W", _ => "CK-T" });
        bool closed = readable && a.Validity is not null && a.Request.Intent is not null && a.Receipt is not null && required.IsSubsetOf(witnesses);
        var closure = a.ProvenClosure ?? JsonSerializer.SerializeToElement(new { closed, witness = witnesses.OrderBy(x => x).ToArray(),
            markers = a.Markers.OrderBy(x => x).ToArray(), frame = closed ? f?.Frame : null });
        if (closed && a.ProvenClosure is null) {
            a.ProvenClosure = closure;
            a.ClosureRecord = JsonSerializer.SerializeToElement(new { kind = "op_closed", subject = a.AttemptId,
                attempt_id = a.AttemptId, runtime_id = f!.RuntimeId, tick = ModTick, frame = f.Frame, wall_ms = f.Ms,
                facts = new { witness = witnesses.OrderBy(x => x).ToArray() } });
        }
        if (!a.ClosureEmitted && a.ClosureRecord is { } record && this.OpClosed is { } emit)
        { emit(record); a.ClosureEmitted = true; }
        return closure;
    }

    public MenuStepCommandResult FinishResult(MenuStepCommandRequest request, MenuStepCommandResult result)
        => this.FinishResult(this.ActiveAttempt is { } active && this.Owns(request) ? active : new ActiveAttempt(request), result);

    public MenuStepCommandResult FinishResult(ActiveAttempt a, MenuStepCommandResult result)
    {
        if (result.ReleaseReceipt is not null && a.Receipt is null) return result; // recovered results retain their receipt
        var request = a.Request;
        a.Delivered |= result.PressCount > 0;
        bool idle = a.ReadOnly || result.Status == MenuStepCommandResult.StatusRejected || result.CleanupConfirmed == true;
        var f = this.ObserveClosure?.Invoke(a);
        if (a.Receipt is null && idle && f is not null && f.RuntimeId == request.RuntimeInstanceId)
            this.RecordReceipt(a, new(a.AttemptId, request.RequestId, ModTick, f.Frame, result.CompletedUnixMs, true, a.Client, a.ReleaseVia, a.ReleaseLapse));
        var closure = this.RefreshClosure(a);
        result = result with { ReleaseReceipt = a.Receipt, Closure = closure };
        result = request.VerbEnvelope is not null || result.RefusalEnvelope is not null
            ? AdmissionRefusals.Attach(result, request, result.NotExecuted, this.ObserveRefusal?.Invoke().Epoch ?? 0,
                checked((long)ModTick), admitted: ReferenceEquals(this.ActiveAttempt, a), debts: this.ObserveDebts?.Invoke(), attempt: a)
            : result;
        if (a.ReadOnly && ReferenceEquals(this.ActiveAttempt, a) && a.Receipt is { } receipt) {
            if (CommandSettledJournal.IsJournalRequest(request)) this.journal.TryFinalize(request, result, false, out _);
            this.TryRelease(a, new(a.AttemptId, request.RequestId, ModTick, receipt.GetProperty("frame").GetInt64(),
                result.CompletedUnixMs, true), this.admittedOwner, out _);
        }
        return result;
    }
}

public sealed record NativeClosureFacts(string RuntimeId, long Frame, long Ms, bool DebtPending, bool UsingTool,
    bool WarpPending, bool ControllerTerminal, bool AdoptedReceiverPending, bool ProposalsReadable, string? Outgoing, string? Incoming,
    string[]? ReceiverPresences = null);
