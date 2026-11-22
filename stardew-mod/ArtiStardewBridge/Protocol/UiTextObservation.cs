using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Batas G3-1 dari Issue #133 §6.2 / CT-024. Bukan antrean tak terbatas.
/// Eviction eksplisit lewat <c>dropped_count</c> / <c>lost_before_seq</c>.
/// </summary>
public static class UiTextCaps
{
    public const int MaxHistoryOccurrences = 128;
    public const int MaxRetainedEncodedBytes = 64 * 1024;
    public const int TtlSeconds = 600;
    public const int NominalTicksPerSecond = 60;
    public const int TtlTicks = TtlSeconds * NominalTicksPerSecond;
    public const int MaxActiveFacts = 64;
    public const int MaxActiveUtf8Bytes = 16 * 1024;
    public const int MaxWirePayloadBytes = 32 * 1024;
    public const int MaxDeltaHistoryRecords = 16;
    public const int ReceiverMaxLineBytes = SceneObservationCaps.ReceiverMaxLineBytes;

    public static readonly string[] DeferredMenuTypes =
    {
        "ChatBox",
    };
}

/// <summary>
/// Satu tick native yang sudah di-capture G3-0 (<see cref="MenuInfo"/> +
/// token G2). SMAPI-free. Bukan izin/target.
/// </summary>
public sealed record UiTextTickSample
{
    public long CaptureTicks { get; init; }

    public string RuntimeInstanceId { get; init; } = "";

    public string CommandSessionId { get; init; } = "";

    public long PlayerId { get; init; }

    public int ScreenId { get; init; }

    public string LocationId { get; init; } = "";

    public long ContextGeneration { get; init; }

    public long UiRevision { get; init; }

    public string? MenuInstanceToken { get; init; }

    public string? MenuType { get; init; }

    public string? PageType { get; init; }

    public string Family { get; init; } = "";

    public string Source { get; init; } = "";

    public string Text { get; init; } = "";

    // ADR 219: optional presented-page provenance. Absent on the legacy path.
    public Dictionary<string, object>? Presented { get; init; }

    public string? Speaker { get; init; }

    public IReadOnlyList<Dictionary<string, object>> Options { get; init; }
        = Array.Empty<Dictionary<string, object>>();

    public IReadOnlyList<string> Labels { get; init; } = Array.Empty<string>();

    public bool DeferredFamily { get; init; }

    public bool CaptureComplete { get; init; } = true;

    public int ActiveFactCount
    {
        get
        {
            int n = this.Text.Length > 0 ? 1 : 0;
            return n + this.Options.Count + this.Labels.Count;
        }
    }

    public bool HasInScopePresentation =>
        !this.DeferredFamily
        && this.Family.Length > 0
        && (this.Text.Length > 0 || this.Options.Count > 0 || this.Labels.Count > 0 || this.Presented is not null);

    public string IdentityKey =>
        this.Family + "\u001f" + (this.MenuInstanceToken ?? "") + "\u001f" + (this.PageType ?? "");

    public static UiTextTickSample FromCapturedMenu(
        MenuInfo? menu,
        string? menuInstanceToken,
        long captureTicks,
        string runtimeInstanceId,
        string commandSessionId,
        long playerId,
        int screenId,
        string locationId,
        long contextGeneration,
        long uiRevision
    )
    {
        string menuType = menu?.MenuType ?? menu?.Name ?? "";
        bool deferred = IsDeferred(menuType);
        string family = "";
        string source = "";
        string text = "";
        string? speaker = null;
        List<Dictionary<string, object>> options = new();
        List<string> labels = new();

        bool captureComplete = true;
        if (!deferred && menu is not null)
        {
            if (string.Equals(menuType, "DialogueBox", StringComparison.Ordinal))
            {
                family = "dialogue";
                source = "DialogueBox.getCurrentString";
                text = menu.VisibleText ?? "";
                speaker = menu.Speaker;
                int remaining = RemainingFactBudget(text, options.Count, labels.Count);
                int choice = 0;
                foreach (UiComponentInfo component in menu.Components)
                {
                    if (!string.Equals(component.Role, "choice", StringComparison.Ordinal))
                        continue;
                    string label = FirstNonEmpty(component.Text, component.Label, component.Name);
                    if (remaining <= 0)
                    {
                        captureComplete = false;
                        break;
                    }

                    options.Add(new Dictionary<string, object>(StringComparer.Ordinal)
                    {
                        ["index"] = choice,
                        ["label"] = label,
                        ["selected"] = component.Selected,
                    });
                    choice++;
                    remaining--;
                }
            }
            else if (string.Equals(menuType, "LetterViewerMenu", StringComparison.Ordinal))
            {
                family = "letter";
                source = "LetterViewerMenu.currentPage";
                text = menu.VisibleText ?? "";
            }
            else if (string.Equals(menuType, "ReadyCheckDialog", StringComparison.Ordinal))
            {
                family = "ready_check";
                source = "ReadyCheckDialog.labels";
                text = menu.VisibleText ?? "";
                if (!CollectVisibleLabels(menu, labels, RemainingFactBudget(text, options.Count, 0)))
                    captureComplete = false;
            }
            else if (menuType.Length > 0)
            {
                family = "menu_label";
                source = "menu.components";
                text = menu.VisibleText ?? "";
                if (!CollectVisibleLabels(menu, labels, RemainingFactBudget(text, options.Count, 0)))
                    captureComplete = false;
            }
        }

        return new UiTextTickSample
        {
            CaptureTicks = captureTicks,
            RuntimeInstanceId = runtimeInstanceId ?? "",
            CommandSessionId = commandSessionId ?? "",
            PlayerId = playerId,
            ScreenId = screenId,
            LocationId = locationId ?? "",
            ContextGeneration = contextGeneration,
            UiRevision = uiRevision,
            MenuInstanceToken = menuInstanceToken,
            MenuType = menuType,
            PageType = menu?.PageType,
            Family = family,
            Source = source,
            Text = text,
            Speaker = speaker,
            Options = options,
            Labels = labels,
            DeferredFamily = deferred,
            CaptureComplete = captureComplete,
        };
    }

    public static bool IsDeferred(string? menuType)
    {
        if (string.IsNullOrEmpty(menuType))
            return false;
        foreach (string name in UiTextCaps.DeferredMenuTypes)
        {
            if (string.Equals(menuType, name, StringComparison.Ordinal))
                return true;
        }

        return false;
    }

    static int RemainingFactBudget(string text, int optionCount, int labelCount)
    {
        int used = (text.Length > 0 ? 1 : 0) + optionCount + labelCount;
        return Math.Max(0, UiTextCaps.MaxActiveFacts - used);
    }

    /// <returns><c>true</c> jika seluruh label unik muat dalam budget; <c>false</c> jika terpotong.</returns>
    static bool CollectVisibleLabels(MenuInfo menu, List<string> labels, int budget)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (UiComponentInfo component in menu.Components)
        {
            string label = FirstNonEmpty(component.Text, component.Label, component.Name);
            if (label.Length == 0 || !seen.Add(label))
                continue;
            if (labels.Count >= budget)
                return false;
            labels.Add(label);
        }

        return true;
    }

    static string FirstNonEmpty(params string?[] parts)
    {
        foreach (string? part in parts)
        {
            if (!string.IsNullOrWhiteSpace(part))
                return part!;
        }

        return "";
    }
}

/// <summary>
/// G3-1: merakit <c>ui.text</c> snapshot/delta. SMAPI-free. Evidence only.
/// </summary>
public static class UiTextSnapshotBuilder
{
    public const string BindingKey = "binding";
    public const string MetaKey = "meta";
    public const string OccPrefix = "occ:";

    public static string OccKey(long seq) => OccPrefix + seq.ToString(System.Globalization.CultureInfo.InvariantCulture);

    public static ObservationPayload Snapshot(
        string observationSessionId,
        long domainRevision,
        IReadOnlyDictionary<string, object> facts,
        int observedCount,
        int sentCount,
        bool truncated
    ) => new()
    {
        SessionId = observationSessionId,
        Product = ObservationProduct.Ui,
        Domain = ObservationDomain.UiText,
        Kind = ObservationKind.Snapshot,
        DomainRevision = domainRevision,
        Provenance = ObservationProvenance.LocalAuthoritative,
        Authoritative = true,
        Available = true,
        Error = null,
        Truncation = new TruncationInfo
        {
            Truncated = truncated,
            ObservedCount = observedCount,
            SentCount = sentCount,
        },
        Body = new ObservationBody { Facts = WireFacts(facts) },
    };

    public static ObservationPayload Delta(
        string observationSessionId,
        long domainRevision,
        IReadOnlyDictionary<string, object>? upserts,
        IReadOnlyList<string>? removes,
        int observedCount,
        int sentCount,
        bool truncated
    ) => new()
    {
        SessionId = observationSessionId,
        Product = ObservationProduct.Ui,
        Domain = ObservationDomain.UiText,
        Kind = ObservationKind.Delta,
        DomainRevision = domainRevision,
        Provenance = ObservationProvenance.LocalAuthoritative,
        Authoritative = true,
        Available = true,
        Error = null,
        Truncation = new TruncationInfo
        {
            Truncated = truncated,
            ObservedCount = observedCount,
            SentCount = sentCount,
        },
        Body = new ObservationBody
        {
            Upserts = upserts is null ? null : WireFacts(upserts),
            Removes = removes,
        },
    };

    public static Dictionary<string, object> WireFacts(IReadOnlyDictionary<string, object> facts)
    {
        Dictionary<string, object> wire = new(StringComparer.Ordinal);
        foreach (KeyValuePair<string, object> pair in facts)
            wire[pair.Key] = JsonSerializer.SerializeToElement(pair.Value, ProtocolJson.Options);
        return wire;
    }

    public static int PayloadUtf8Bytes(ObservationPayload payload) =>
        Encoding.UTF8.GetByteCount(JsonSerializer.Serialize(payload, ProtocolJson.Options));

    public static int EnvelopeUtf8Bytes(ObservationPayload payload, long playerId = 0, int screenId = 0)
    {
        ObservationEnvelope env = ObservationEnvelope.Create(0, playerId, screenId, payload);
        return Encoding.UTF8.GetByteCount(JsonSerializer.Serialize(env, ProtocolJson.Options) + "\n");
    }

    public static bool FitsWire(ObservationPayload payload, long playerId = 0, int screenId = 0)
    {
        int payloadBytes = PayloadUtf8Bytes(payload);
        if (payloadBytes > UiTextCaps.MaxWirePayloadBytes)
            return false;
        return EnvelopeUtf8Bytes(payload, playerId, screenId) <= UiTextCaps.ReceiverMaxLineBytes;
    }
}

sealed class UiTextOccurrence
{
    public long OccurrenceSeq;
    public string Family = "";
    public string IdentityKey = "";
    public string? InstanceToken;
    public string? PageType;
    public string Source = "";
    public string Text = "";
    public Dictionary<string, object>? Presented;
    public int TextRevision = 1;
    public string RevealStatus = "full";
    public string? Speaker;
    public List<Dictionary<string, object>> Options = new();
    public List<string> Labels = new();
    public string Status = "active";
    public bool Complete = true;
    public bool Replayed;
    public long FirstCaptureTicks;
    public long LastCaptureTicks;

    public Dictionary<string, object> ToFact()
    {
        var fact = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["occurrence_seq"] = this.OccurrenceSeq,
            ["family"] = this.Family,
            ["instance_token"] = this.InstanceToken ?? "",
            ["page_type"] = this.PageType ?? "",
            ["source"] = this.Source,
            ["text"] = this.Text,
            ["text_revision"] = this.TextRevision,
            ["reveal_status"] = this.RevealStatus,
            ["status"] = this.Status,
            ["complete"] = this.Complete,
            ["replayed"] = this.Replayed,
            ["actionable"] = false,
            ["first_capture_ticks"] = this.FirstCaptureTicks,
            ["last_capture_ticks"] = this.LastCaptureTicks,
            ["origin_occurrence_seq"] = this.OccurrenceSeq,
        };
        if (this.Speaker is { Length: > 0 })
            fact["speaker"] = this.Speaker;
        if (this.Presented is not null) fact["presented"] = this.Presented;
        if (this.Options.Count > 0)
            fact["options"] = this.Options;
        if (this.Labels.Count > 0)
            fact["labels"] = this.Labels;
        return fact;
    }

    public int EncodedBytes() =>
        Encoding.UTF8.GetByteCount(JsonSerializer.Serialize(this.ToFact(), ProtocolJson.Options));

    public int ActiveUtf8Bytes()
    {
        int n = Encoding.UTF8.GetByteCount(this.Text);
        foreach (Dictionary<string, object> option in this.Options)
        {
            if (option.TryGetValue("label", out object? label) && label is string s)
                n += Encoding.UTF8.GetByteCount(s);
        }

        foreach (string label in this.Labels)
            n += Encoding.UTF8.GetByteCount(label);
        return n;
    }

    public UiTextOccurrence Clone()
    {
        return new UiTextOccurrence
        {
            OccurrenceSeq = this.OccurrenceSeq,
            Family = this.Family,
            IdentityKey = this.IdentityKey,
            InstanceToken = this.InstanceToken,
            PageType = this.PageType,
            Source = this.Source,
            Text = this.Text,
            Presented = this.Presented,
            TextRevision = this.TextRevision,
            RevealStatus = this.RevealStatus,
            Speaker = this.Speaker,
            Options = this.Options.Select(o => new Dictionary<string, object>(o, StringComparer.Ordinal)).ToList(),
            Labels = new List<string>(this.Labels),
            Status = this.Status,
            Complete = this.Complete,
            Replayed = this.Replayed,
            FirstCaptureTicks = this.FirstCaptureTicks,
            LastCaptureTicks = this.LastCaptureTicks,
        };
    }
}

/// <summary>
/// Accumulator presented-text per screen: capture tiap tick, kawat latest-wins.
/// History bounded, eviction eksplisit lewat dropped_count/lost_before_seq.
/// </summary>
public sealed class UiTextAccumulator
{
    readonly List<UiTextOccurrence> active = new();
    readonly List<UiTextOccurrence> history = new();
    readonly List<long> pendingChanged = new();
    readonly HashSet<long> pendingRemoves = new();
    string? inflightFingerprint;
    bool dirty;
    bool unsupported;
    bool needsSnapshot = true;
    bool replayedHistory;
    long nextOccurrenceSeq = 1;
    long lastAckedRevision;
    UiTextTickSample? lastBinding;

    public string SessionId { get; private set; } = "";

    public long DomainRevision { get; private set; }

    public bool Dirty => this.dirty;

    public bool Unsupported => this.unsupported;

    public bool HasSession => this.SessionId.Length > 0;

    public bool NeedsSnapshot => this.needsSnapshot;

    public int ActiveCount => this.active.Count;

    public int HistoryCount => this.history.Count;

    public int RetainedEncodedOccurrenceBytes => this.RetainedEncodedBytes();

    public int ActiveViewFactCount
    {
        get
        {
            int n = 0;
            foreach (UiTextOccurrence occ in this.active)
            {
                if (occ.Text.Length > 0)
                    n++;
                n += occ.Options.Count;
                n += occ.Labels.Count;
            }

            return n;
        }
    }

    public int ActiveViewUtf8Bytes
    {
        get
        {
            int n = 0;
            foreach (UiTextOccurrence occ in this.active)
                n += occ.ActiveUtf8Bytes();
            return n;
        }
    }

    public bool ActivePresentationComplete =>
        this.active.Count == 0 || this.active.TrueForAll(o => o.Complete);

    public long DroppedCount { get; private set; }

    public long LostBeforeSeq { get; private set; } = 1;

    public long NextOccurrenceSeq => this.nextOccurrenceSeq;

    public IReadOnlyList<long> PendingChangedSeqs => this.pendingChanged;

    public void BindSession(string sessionId)
    {
        string next = sessionId ?? "";
        bool changed = !string.Equals(this.SessionId, next, StringComparison.Ordinal);
        this.SessionId = next;
        if (!changed)
            return;
        this.DomainRevision = 0;
        this.lastAckedRevision = 0;
        this.inflightFingerprint = null;
        this.needsSnapshot = true;
        ExpireAllActive(replayed: true);
        foreach (UiTextOccurrence occ in this.history)
            occ.Replayed = true;
        this.replayedHistory = this.history.Count > 0;
        this.pendingChanged.Clear();
        this.pendingRemoves.Clear();
        foreach (UiTextOccurrence occ in this.history)
            MarkChanged(occ.OccurrenceSeq);
        this.dirty = this.HasSession && (this.history.Count > 0 || this.active.Count > 0);
    }

    public void OnReturnedToTitle()
    {
        this.SessionId = "";
        this.DomainRevision = 0;
        this.lastAckedRevision = 0;
        this.active.Clear();
        this.history.Clear();
        this.pendingChanged.Clear();
        this.pendingRemoves.Clear();
        this.inflightFingerprint = null;
        this.dirty = false;
        this.unsupported = false;
        this.needsSnapshot = true;
        this.replayedHistory = false;
        this.nextOccurrenceSeq = 1;
        this.DroppedCount = 0;
        this.LostBeforeSeq = 1;
        this.lastBinding = null;
    }

    public void AdvanceUnsupported()
    {
        this.unsupported = true;
        this.active.Clear();
        this.history.Clear();
        this.pendingChanged.Clear();
        this.pendingRemoves.Clear();
        this.dirty = false;
        this.inflightFingerprint = null;
        this.lastBinding = null;
    }

    public void Advance(UiTextTickSample sample) => this.AdvanceBatch(new[] { sample });

    // Menu and HUD are independent simultaneous presentations. Expire only after
    // the whole native tick is known, so one never closes the other.
    public void AdvanceBatch(IReadOnlyList<UiTextTickSample> samples)
    {
        if (samples.Count == 0) return;
        this.lastBinding = samples[0];
        var keys = samples.Where(s => s.HasInScopePresentation).Select(s => s.IdentityKey).ToHashSet();
        foreach (UiTextOccurrence occ in this.active.ToArray())
            if (!keys.Contains(occ.IdentityKey)) Expire(occ, samples[0].CaptureTicks);
        foreach (UiTextTickSample sample in samples) AdvanceOne(sample);
        this.lastBinding = samples[0];
    }

    private void AdvanceOne(UiTextTickSample sample)
    {
        this.unsupported = false;
        this.lastBinding = sample;
        EvictTtl(sample.CaptureTicks);
        EvictBounds();

        if (sample.DeferredFamily || !sample.HasInScopePresentation)
        {
            EvictBounds();
            return;
        }

        UiTextOccurrence? current = FindActive(sample.IdentityKey);
        current = FindActive(sample.IdentityKey);
        if (current is null)
        {
            Mint(sample);
            EvictBounds();
            return;
        }

        if (SamePresentation(current, sample))
        {
            current.LastCaptureTicks = sample.CaptureTicks;
            current.RevealStatus = "full";
            return;
        }

        if (IsPrefixCoalesce(current.Text, sample.Text))
        {
            int previousLength = current.Text.Length;
            current.Text = sample.Text;
            current.TextRevision++;
            current.RevealStatus = sample.Text.Length > previousLength ? "prefix" : "full";
            current.Options = sample.Options.Select(o => new Dictionary<string, object>(o)).ToList();
            current.Labels = sample.Labels.ToList();
            current.Speaker = sample.Speaker;
            current.LastCaptureTicks = sample.CaptureTicks;
            current.Complete = sample.CaptureComplete;
            BoundActiveView(current);
            MarkChanged(current.OccurrenceSeq);
            this.dirty = true;
            EvictBounds();
            return;
        }

        Expire(current, sample.CaptureTicks);
        Mint(sample);
        EvictBounds();
    }

    public ObservationPayload? ConsumeDirtyPayload()
    {
        if (this.unsupported || !this.dirty || !this.HasSession)
            return null;

        ObservationPayload? payload = this.needsSnapshot || this.lastAckedRevision == 0
            ? BuildFittingSnapshot()
            : BuildFittingDelta();
        if (payload is null)
            return null;

        this.DomainRevision++;
        payload = payload with { DomainRevision = this.DomainRevision };
        this.inflightFingerprint = Fingerprint(payload);
        if (payload.Kind == ObservationKind.Snapshot)
        {
            this.pendingChanged.Clear();
            this.pendingRemoves.Clear();
            this.dirty = false;
        }
        else
        {
            this.dirty = this.pendingChanged.Count > 0 || this.pendingRemoves.Count > 0;
        }

        return payload;
    }

    public void AckSent()
    {
        this.lastAckedRevision = this.DomainRevision;
        this.inflightFingerprint = null;
        this.needsSnapshot = false;
        this.replayedHistory = false;
        if (this.pendingChanged.Count > 0 || this.pendingRemoves.Count > 0)
            this.dirty = true;
    }

    public void NackSent()
    {
        this.inflightFingerprint = null;
        this.needsSnapshot = true;
        if (!this.unsupported && this.HasSession)
            this.dirty = true;
    }

    public UiTextOccurrenceView? ActiveOccurrence()
    {
        if (this.active.Count == 0)
            return null;
        UiTextOccurrence occ = this.active[^1];
        return new UiTextOccurrenceView(
            occ.OccurrenceSeq,
            occ.Family,
            occ.Text,
            occ.TextRevision,
            occ.Status,
            occ.Replayed,
            occ.RevealStatus
        );
    }

    public IReadOnlyList<UiTextOccurrenceView> HistoryViews()
    {
        return this.history.Select(occ => new UiTextOccurrenceView(
            occ.OccurrenceSeq,
            occ.Family,
            occ.Text,
            occ.TextRevision,
            occ.Status,
            occ.Replayed,
            occ.RevealStatus
        )).ToArray();
    }

    void Mint(UiTextTickSample sample)
    {
        if (this.active.Count >= UiTextCaps.MaxActiveFacts)
            Expire(this.active[0], sample.CaptureTicks);

        var occ = new UiTextOccurrence
        {
            OccurrenceSeq = this.nextOccurrenceSeq++,
            Family = sample.Family,
            IdentityKey = sample.IdentityKey,
            InstanceToken = sample.MenuInstanceToken,
            PageType = sample.PageType,
            Source = sample.Source,
            Text = sample.Text,
            Presented = sample.Presented,
            TextRevision = 1,
            RevealStatus = "full",
            Speaker = sample.Speaker,
            Options = sample.Options.Select(o => new Dictionary<string, object>(o)).ToList(),
            Labels = sample.Labels.ToList(),
            Status = "active",
            Complete = sample.CaptureComplete,
            Replayed = false,
            FirstCaptureTicks = sample.CaptureTicks,
            LastCaptureTicks = sample.CaptureTicks,
        };
        BoundActiveView(occ);
        this.active.Add(occ);
        MarkChanged(occ.OccurrenceSeq);
        this.dirty = true;
        this.replayedHistory = false;
    }

    void Expire(UiTextOccurrence occ, long ticks)
    {
        if (!this.active.Remove(occ))
            return;
        occ.Status = "expired";
        occ.LastCaptureTicks = ticks;
        this.history.Add(occ);
        MarkChanged(occ.OccurrenceSeq);
        this.dirty = true;
    }

    void ExpireAllActive(bool replayed)
    {
        if (this.active.Count == 0)
            return;
        long ticks = this.lastBinding?.CaptureTicks ?? 0;
        foreach (UiTextOccurrence occ in this.active.ToArray())
        {
            Expire(occ, ticks);
            if (replayed)
                occ.Replayed = true;
        }
    }

    UiTextOccurrence? FindActive(string identity)
    {
        foreach (UiTextOccurrence occ in this.active)
        {
            if (string.Equals(occ.IdentityKey, identity, StringComparison.Ordinal))
                return occ;
        }

        return null;
    }

    void MarkChanged(long seq)
    {
        if (!this.pendingChanged.Contains(seq))
            this.pendingChanged.Add(seq);
    }

    static bool SamePresentation(UiTextOccurrence occ, UiTextTickSample sample)
    {
        if (!string.Equals(occ.Text, sample.Text, StringComparison.Ordinal))
            return false;
        if (occ.Options.Count != sample.Options.Count || occ.Labels.Count != sample.Labels.Count)
            return false;
        string a = JsonSerializer.Serialize(occ.Options, ProtocolJson.Options);
        string b = JsonSerializer.Serialize(sample.Options, ProtocolJson.Options);
        if (!string.Equals(a, b, StringComparison.Ordinal))
            return false;
        return occ.Labels.SequenceEqual(sample.Labels, StringComparer.Ordinal);
    }

    static bool IsPrefixCoalesce(string oldText, string nextText)
    {
        if (oldText.Length == 0 || nextText.Length == 0)
            return false;
        return nextText.StartsWith(oldText, StringComparison.Ordinal)
            || oldText.StartsWith(nextText, StringComparison.Ordinal);
    }

    void EvictTtl(long nowTicks)
    {
        for (int i = this.history.Count - 1; i >= 0; i--)
        {
            UiTextOccurrence occ = this.history[i];
            if (nowTicks - occ.LastCaptureTicks <= UiTextCaps.TtlTicks)
                continue;
            DropHistoryAt(i);
        }
    }

    void EvictBounds()
    {
        while (this.history.Count > UiTextCaps.MaxHistoryOccurrences)
            DropHistoryAt(0);
        while (RetainedEncodedBytes() > UiTextCaps.MaxRetainedEncodedBytes && this.history.Count > 0)
            DropHistoryAt(0);
        if (RetainedEncodedBytes() > UiTextCaps.MaxRetainedEncodedBytes)
            ShrinkActiveToRetainedCap();
    }

    void ShrinkActiveToRetainedCap()
    {
        foreach (UiTextOccurrence occ in this.active)
        {
            if (RetainedEncodedBytes() <= UiTextCaps.MaxRetainedEncodedBytes)
                return;
            occ.Complete = false;
            while (RetainedEncodedBytes() > UiTextCaps.MaxRetainedEncodedBytes && occ.Labels.Count > 0)
                occ.Labels.RemoveAt(occ.Labels.Count - 1);
            while (RetainedEncodedBytes() > UiTextCaps.MaxRetainedEncodedBytes && occ.Options.Count > 0)
                occ.Options.RemoveAt(occ.Options.Count - 1);
            if (RetainedEncodedBytes() > UiTextCaps.MaxRetainedEncodedBytes)
            {
                string original = occ.Text;
                int lo = 0;
                int hi = Encoding.UTF8.GetByteCount(original);
                string best = BoundUtf8(original, 0, out _);
                while (lo <= hi)
                {
                    int mid = (lo + hi) / 2;
                    occ.Text = BoundUtf8(original, mid, out _);
                    if (RetainedEncodedBytes() <= UiTextCaps.MaxRetainedEncodedBytes)
                    {
                        best = occ.Text;
                        lo = mid + 1;
                    }
                    else
                    {
                        hi = mid - 1;
                    }
                }

                occ.Text = best;
            }

            MarkChanged(occ.OccurrenceSeq);
            this.dirty = true;
        }
    }

    void DropHistoryAt(int index)
    {
        UiTextOccurrence occ = this.history[index];
        this.history.RemoveAt(index);
        this.DroppedCount++;
        if (occ.OccurrenceSeq + 1 > this.LostBeforeSeq)
            this.LostBeforeSeq = occ.OccurrenceSeq + 1;
        this.pendingRemoves.Add(occ.OccurrenceSeq);
        this.pendingChanged.Remove(occ.OccurrenceSeq);
        this.dirty = true;
    }

    int RetainedEncodedBytes()
    {
        int n = 0;
        foreach (UiTextOccurrence occ in this.active)
            n += occ.EncodedBytes();
        foreach (UiTextOccurrence occ in this.history)
            n += occ.EncodedBytes();
        return n;
    }

    static void BoundActiveView(UiTextOccurrence occ)
    {
        BoundActiveFacts(occ);
        BoundActiveUtf8Content(occ);
    }

    static void BoundActiveFacts(UiTextOccurrence occ)
    {
        int remaining = UiTextCaps.MaxActiveFacts;
        if (occ.Text.Length > 0)
            remaining--;
        if (remaining < 0)
            remaining = 0;
        if (occ.Options.Count > remaining)
        {
            occ.Options.RemoveRange(remaining, occ.Options.Count - remaining);
            occ.Complete = false;
        }

        remaining -= occ.Options.Count;
        if (remaining < 0)
            remaining = 0;
        if (occ.Labels.Count > remaining)
        {
            occ.Labels.RemoveRange(remaining, occ.Labels.Count - remaining);
            occ.Complete = false;
        }
    }

    static void BoundActiveUtf8Content(UiTextOccurrence occ)
    {
        while (occ.ActiveUtf8Bytes() > UiTextCaps.MaxActiveUtf8Bytes)
        {
            occ.Complete = false;
            if (occ.Labels.Count > 0)
            {
                occ.Labels.RemoveAt(occ.Labels.Count - 1);
                continue;
            }

            if (occ.Options.Count > 0)
            {
                occ.Options.RemoveAt(occ.Options.Count - 1);
                continue;
            }

            occ.Text = BoundUtf8(occ.Text, UiTextCaps.MaxActiveUtf8Bytes, out _);
            break;
        }
    }

    /// <summary>
    /// Potong di batas scalar Unicode. Jangan GetString di tengah leading byte
    /// (itu yang menghasilkan U+FFFD).
    /// </summary>
    static string BoundUtf8(string text, int maxBytes, out bool truncated)
    {
        if (maxBytes <= 0)
        {
            truncated = Encoding.UTF8.GetByteCount(text) > 0;
            return "";
        }

        byte[] raw = Encoding.UTF8.GetBytes(text);
        if (raw.Length <= maxBytes)
        {
            truncated = false;
            return text;
        }

        truncated = true;
        int n = maxBytes;
        if (n < raw.Length && (raw[n] & 0xC0) == 0x80)
        {
            while (n > 0 && (raw[n - 1] & 0xC0) == 0x80)
                n--;
            if (n > 0)
                n--;
        }

        if (n <= 0)
            return "";
        return new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true)
            .GetString(raw, 0, n);
    }

    Dictionary<string, object> BindingFact()
    {
        UiTextTickSample? sample = this.lastBinding;
        return new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["runtime_instance_id"] = sample?.RuntimeInstanceId ?? "",
            ["session_id"] = sample?.CommandSessionId ?? "",
            ["player_id"] = sample?.PlayerId ?? 0L,
            ["screen_id"] = sample?.ScreenId ?? 0,
            ["location_id"] = sample?.LocationId ?? "",
            ["context_generation"] = sample?.ContextGeneration ?? 0L,
            ["ui_revision"] = sample?.UiRevision ?? 0L,
            ["menu_instance_token"] = sample?.MenuInstanceToken ?? "",
            ["capture_ticks"] = sample?.CaptureTicks ?? 0L,
        };
    }

    Dictionary<string, object> MetaFact()
    {
        return new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["cursor_occurrence_seq"] = this.nextOccurrenceSeq - 1,
            ["floor_lost_before_seq"] = this.LostBeforeSeq,
            ["dropped_count"] = this.DroppedCount,
            ["replayed_history"] = this.replayedHistory,
            ["active_seqs"] = this.active.Select(o => o.OccurrenceSeq).ToArray(),
        };
    }

    ObservationPayload? BuildFittingSnapshot()
    {
        List<UiTextOccurrence> wanted = new();
        wanted.AddRange(this.active.Select(o => o.Clone()));
        wanted.AddRange(this.history.Select(o => o.Clone()));
        int observed = 2 + wanted.Count;
        while (true)
        {
            Dictionary<string, object> facts = new(StringComparer.Ordinal)
            {
                [UiTextSnapshotBuilder.BindingKey] = BindingFact(),
                [UiTextSnapshotBuilder.MetaKey] = MetaFact(),
            };
            foreach (UiTextOccurrence occ in wanted)
                facts[UiTextSnapshotBuilder.OccKey(occ.OccurrenceSeq)] = occ.ToFact();
            ObservationPayload payload = UiTextSnapshotBuilder.Snapshot(
                this.SessionId,
                this.DomainRevision + 1,
                facts,
                observed,
                facts.Count,
                truncated: facts.Count != observed
            );
            if (UiTextSnapshotBuilder.FitsWire(payload, lastBinding?.PlayerId ?? 0, lastBinding?.ScreenId ?? 0))
                return payload;
            if (wanted.Count == 0)
                return null;
            int drop = wanted.FindIndex(o => o.Status == "expired");
            if (drop < 0)
            {
                UiTextOccurrence huge = wanted[0];
                huge.Text = BoundUtf8(huge.Text, Math.Max(32, huge.Text.Length / 2), out bool cut);
                if (cut)
                    huge.Complete = false;
                else
                    wanted.RemoveAt(0);
                continue;
            }

            wanted.RemoveAt(drop);
        }
    }

    ObservationPayload? BuildFittingDelta()
    {
        int budget = UiTextCaps.MaxDeltaHistoryRecords;
        int observedOcc = this.pendingChanged.Count + this.pendingRemoves.Count;
        List<long> batch = new();
        foreach (long seq in this.pendingChanged)
        {
            if (batch.Count >= budget)
                break;
            batch.Add(seq);
        }

        int removeBudget = budget - batch.Count;
        List<string> removes = new();
        foreach (long seq in this.pendingRemoves)
        {
            if (removes.Count >= removeBudget)
                break;
            removes.Add(UiTextSnapshotBuilder.OccKey(seq));
        }

        Dictionary<string, object> upserts = new(StringComparer.Ordinal)
        {
            [UiTextSnapshotBuilder.BindingKey] = BindingFact(),
            [UiTextSnapshotBuilder.MetaKey] = MetaFact(),
        };
        foreach (long seq in batch)
        {
            UiTextOccurrence? occ = this.active.FirstOrDefault(o => o.OccurrenceSeq == seq)
                ?? this.history.FirstOrDefault(o => o.OccurrenceSeq == seq);
            if (occ is null)
            {
                if (!this.pendingRemoves.Contains(seq))
                    this.pendingRemoves.Add(seq);
                continue;
            }

            upserts[UiTextSnapshotBuilder.OccKey(seq)] = occ.ToFact();
        }

        int sentOcc = 0;
        foreach (string key in upserts.Keys)
        {
            if (key.StartsWith(UiTextSnapshotBuilder.OccPrefix, StringComparison.Ordinal))
                sentOcc++;
        }

        sentOcc += removes.Count;
        ObservationPayload payload = UiTextSnapshotBuilder.Delta(
            this.SessionId,
            this.DomainRevision + 1,
            upserts,
            removes.Count == 0 ? null : removes,
            observedCount: observedOcc,
            sentCount: sentOcc,
            truncated: observedOcc > sentOcc
        );
        if (!UiTextSnapshotBuilder.FitsWire(payload, lastBinding?.PlayerId ?? 0, lastBinding?.ScreenId ?? 0))
            return BuildFittingSnapshot();

        foreach (long seq in batch)
            this.pendingChanged.Remove(seq);
        foreach (string key in removes)
        {
            if (key.StartsWith(UiTextSnapshotBuilder.OccPrefix, StringComparison.Ordinal)
                && long.TryParse(key.AsSpan(UiTextSnapshotBuilder.OccPrefix.Length), out long seq))
            {
                this.pendingRemoves.Remove(seq);
            }
        }

        return payload;
    }

    static string Fingerprint(ObservationPayload payload) =>
        JsonSerializer.Serialize(payload, ProtocolJson.Options);
}

public readonly record struct UiTextOccurrenceView(
    long OccurrenceSeq,
    string Family,
    string Text,
    int TextRevision,
    string Status,
    bool Replayed,
    string RevealStatus
);
