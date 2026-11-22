using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Identity + fingerprint OBS-3A. SMAPI-free. Display name bukan identity.
/// </summary>
public static class InventoryIdentity
{
    public const string MetaFactId = "meta:inventory";

    public static string SlotFactId(int slot) => $"slot:{slot}";

    public static string EquipmentFactId(string location) =>
        $"eq:{SceneIdentity.Token(location)}";

    public static string TypeToken(InventoryItemView item)
    {
        if (!string.IsNullOrEmpty(item.WeaponSubtype))
            return "weapon";
        if (!string.IsNullOrEmpty(item.ToolSubtype))
            return "tool";
        return item.TypeDefinitionId switch
        {
            "(O)" => "object",
            "(T)" => "tool",
            "(W)" => "weapon",
            "(H)" => "hat",
            "(B)" => "boots",
            "(R)" => "ring",
            "(S)" => "clothing",
            "(P)" => "clothing",
            "(F)" => "furniture",
            _ => "item",
        };
    }

    /// <summary>
    /// Fingerprint semantik: slot, isi, quality, tool/weapon, active slot,
    /// equipment. Bukan animasi. Dipakai supaya state sama tidak di-emit ulang.
    /// </summary>
    public static string Fingerprint(InventorySnapshotInput input)
    {
        StringBuilder text = new();
        text.Append(input.ActiveSlot).Append('|').Append(input.Slots.Count).Append('|');
        foreach (InventorySlotInput slot in input.Slots)
        {
            text.Append(slot.Slot).Append(':');
            AppendItem(text, slot.Item);
            text.Append(';');
        }
        text.Append('#');
        foreach (InventoryEquipmentInput eq in input.Equipment)
        {
            text.Append(eq.Location).Append(':');
            AppendItem(text, eq.Item);
            text.Append(';');
        }
        return text.ToString();
    }

    static void AppendItem(StringBuilder text, InventoryItemView? item)
    {
        if (item is null)
        {
            text.Append('-');
            return;
        }
        text.Append(item.QualifiedItemId).Append(',')
            .Append(item.Stack).Append(',')
            .Append(item.Quality).Append(',')
            .Append(item.Category).Append(',')
            .Append(item.TypeDefinitionId).Append(',')
            .Append(item.ToolSubtype ?? "").Append(',')
            .Append(item.ToolUpgradeLevel?.ToString() ?? "").Append(',')
            .Append(item.WeaponSubtype ?? "");
        // WS1: a water change must be re-sent (the refresh resends identical facts only).
        if (item.ToolWaterLeft is not null || item.ToolWaterMax is not null)
            text.Append(",w").Append(item.ToolWaterLeft?.ToString() ?? "").Append('/').Append(item.ToolWaterMax?.ToString() ?? "");
        // S31: an attachment-only change (bait/tackle in or out) and a same-qid instance swap must be re-sent.
        if (item.ToolInstanceToken is not null || item.ToolAttachmentSlots is not null || item.Attachments is not null)
        {
            text.Append(",a").Append(item.ToolInstanceToken ?? "").Append('/')
                .Append(item.ToolAttachmentSlots?.ToString() ?? "").Append('[');
            foreach (InventoryAttachmentView a in InventoryAttachments.Bounded(item.Attachments))
            {
                text.Append(a.Slot).Append(':').Append(a.QualifiedItemId ?? "-").Append(',')
                    .Append(a.Stack).Append(',').Append(a.Quality).Append(',').Append(a.Category).Append(',')
                    .Append(a.Uses?.ToString() ?? "").Append(';');
            }
            text.Append(']');
        }
    }
}

/// <summary>
/// S31: lifetime tool identity. Weak reference identity (the capture never keeps a tool alive);
/// one token per object instance for the life of the process. Never a save id or a hash code.
/// </summary>
public sealed class InventoryInstanceTokens
{
    sealed record Identity(long Serial);
    readonly System.Runtime.CompilerServices.ConditionalWeakTable<object, Identity> identities = new();
    long serial;

    public string Token(object instance) =>
        "ti" + this.identities.GetValue(instance, _ => new Identity(Interlocked.Increment(ref this.serial))).Serial;
}

/// <summary>S31: bounded attachment projection shared by the fingerprint and the wire.</summary>
public static class InventoryAttachments
{
    public static IEnumerable<InventoryAttachmentView> Bounded(IReadOnlyList<InventoryAttachmentView>? raw)
    {
        if (raw is null)
            yield break;
        int sent = 0;
        foreach (InventoryAttachmentView a in raw)
        {
            if (sent >= InventoryObservationCaps.Attachments)
                yield break;
            sent++;
            yield return a;
        }
    }

    public static IReadOnlyList<InventoryAttachmentFact>? Facts(IReadOnlyList<InventoryAttachmentView>? raw)
    {
        if (raw is null)
            return null;
        List<InventoryAttachmentFact> facts = new(Math.Min(raw.Count, InventoryObservationCaps.Attachments));
        foreach (InventoryAttachmentView a in Bounded(raw))
        {
            if (string.IsNullOrWhiteSpace(a.QualifiedItemId))
            {
                facts.Add(new InventoryAttachmentFact { Slot = a.Slot, Occupied = false });
                continue;
            }
            facts.Add(new InventoryAttachmentFact
            {
                Slot = a.Slot,
                Occupied = true,
                QualifiedItemId = SceneIdentity.Token(a.QualifiedItemId),
                Stack = a.Stack,
                Quality = a.Quality,
                Category = a.Category,
                Uses = a.Uses,
            });
        }
        return facts;
    }
}

/// <summary>
/// Merakit ObservationPayload snapshot/error inventaris dari DTO murni.
/// Tidak menyentuh runtime Stardew. Snapshot penuh, bukan delta 10 Hz.
/// </summary>
public static class InventorySnapshotBuilder
{
    public static ObservationPayload Snapshot(
        string sessionId,
        long domainRevision,
        InventorySnapshotInput input
    )
    {
        Dictionary<string, object> facts = new(StringComparer.Ordinal);
        int observed = 0;
        bool truncated = false;

        InventoryMetaFact meta = new()
        {
            ActiveSlot = input.ActiveSlot,
            SlotCount = input.Slots.Count,
        };
        observed += 1;
        AddFact(facts, InventoryIdentity.MetaFactId, meta);

        int slotCap = InventoryObservationCaps.Slots;
        int slotIndex = 0;
        foreach (InventorySlotInput slot in input.Slots)
        {
            observed += 1;
            if (slotIndex >= slotCap)
            {
                truncated = true;
                continue;
            }
            AddFact(facts, InventoryIdentity.SlotFactId(slot.Slot), SlotFact(slot, input.ActiveSlot));
            slotIndex++;
        }

        int eqCap = InventoryObservationCaps.Equipment;
        int eqIndex = 0;
        foreach (InventoryEquipmentInput eq in CanonicalEquipment(input.Equipment))
        {
            observed += 1;
            if (eqIndex >= eqCap)
            {
                truncated = true;
                continue;
            }
            AddFact(facts, InventoryIdentity.EquipmentFactId(eq.Location), EquipmentFact(eq));
            eqIndex++;
        }

        if (observed > facts.Count)
            truncated = true;

        return new ObservationPayload
        {
            SessionId = sessionId,
            Product = ObservationProduct.Progress,
            Domain = ObservationDomain.ProgressInventory,
            Kind = ObservationKind.Snapshot,
            DomainRevision = domainRevision,
            Provenance = ObservationProvenance.LocalAuthoritative,
            Authoritative = true,
            Available = true,
            Error = null,
            Truncation = new TruncationInfo
            {
                Truncated = truncated,
                ObservedCount = observed,
                SentCount = facts.Count,
            },
            Body = new ObservationBody { Facts = facts },
        };
    }

    public static ObservationPayload Error(
        string sessionId,
        long domainRevision,
        string reason,
        string? detail = null
    ) => new()
    {
        SessionId = sessionId,
        Product = ObservationProduct.Progress,
        Domain = ObservationDomain.ProgressInventory,
        Kind = ObservationKind.Error,
        DomainRevision = domainRevision,
        Provenance = ObservationProvenance.Unknown,
        Authoritative = false,
        Available = false,
        Error = new ObservationErrorInfo { Reason = reason, Detail = detail },
        Truncation = null,
        Body = null,
    };

    static InventorySlotFact SlotFact(InventorySlotInput slot, int activeSlot)
    {
        bool active = slot.Slot == activeSlot;
        if (slot.Item is null)
        {
            return new InventorySlotFact
            {
                Slot = slot.Slot,
                Occupied = false,
                IsActive = active,
            };
        }

        InventoryItemView item = slot.Item;
        return new InventorySlotFact
        {
            Slot = slot.Slot,
            Occupied = true,
            IsActive = active,
            QualifiedItemId = SceneIdentity.Token(item.QualifiedItemId),
            Name = BoundName(item.Name),
            DisplayName = BoundName(item.DisplayName),
            Stack = item.Stack,
            Quality = item.Quality,
            Category = item.Category,
            TypeDefinition = SceneWire.Bound(item.TypeDefinitionId, SceneWireLimits.LocationKindMax),
            TypeToken = InventoryIdentity.TypeToken(item),
            ToolSubtype = BoundName(item.ToolSubtype),
            ToolUpgradeLevel = item.ToolUpgradeLevel,
            WeaponSubtype = BoundName(item.WeaponSubtype),
            ToolWaterLeft = item.ToolWaterLeft,
            ToolWaterMax = item.ToolWaterMax,
            ToolInstance = item.ToolInstanceToken is null
                ? null
                : SceneWire.Bound(item.ToolInstanceToken, SceneWireLimits.LocationKindMax),
            AttachmentSlots = item.ToolAttachmentSlots,
            Attachments = InventoryAttachments.Facts(item.Attachments),
        };
    }

    static InventoryEquipmentFact EquipmentFact(InventoryEquipmentInput eq)
    {
        if (eq.Item is null)
        {
            return new InventoryEquipmentFact
            {
                Location = eq.Location,
                Occupied = false,
            };
        }

        InventoryItemView item = eq.Item;
        return new InventoryEquipmentFact
        {
            Location = eq.Location,
            Occupied = true,
            QualifiedItemId = SceneIdentity.Token(item.QualifiedItemId),
            Name = BoundName(item.Name),
            DisplayName = BoundName(item.DisplayName),
            Stack = item.Stack,
            Quality = item.Quality,
            Category = item.Category,
            TypeDefinition = SceneWire.Bound(item.TypeDefinitionId, SceneWireLimits.LocationKindMax),
            TypeToken = InventoryIdentity.TypeToken(item),
            ToolSubtype = BoundName(item.ToolSubtype),
            ToolUpgradeLevel = item.ToolUpgradeLevel,
            WeaponSubtype = BoundName(item.WeaponSubtype),
            ToolWaterLeft = item.ToolWaterLeft,
            ToolWaterMax = item.ToolWaterMax,
        };
    }

    static IReadOnlyList<InventoryEquipmentInput> CanonicalEquipment(
        IReadOnlyList<InventoryEquipmentInput> raw
    )
    {
        Dictionary<string, InventoryEquipmentInput> byLocation = new(StringComparer.Ordinal);
        foreach (InventoryEquipmentInput item in raw)
        {
            if (string.IsNullOrWhiteSpace(item.Location))
                continue;
            if (!byLocation.ContainsKey(item.Location))
                byLocation[item.Location] = item;
        }

        List<InventoryEquipmentInput> ordered = new(InventoryEquipmentLocation.All.Length);
        foreach (string location in InventoryEquipmentLocation.All)
        {
            if (byLocation.TryGetValue(location, out InventoryEquipmentInput? found) && found is not null)
                ordered.Add(found);
            else
                ordered.Add(new InventoryEquipmentInput { Location = location, Item = null });
        }
        return ordered;
    }

    static string? BoundName(string? value)
    {
        if (string.IsNullOrEmpty(value))
            return null;
        string bound = SceneWire.Bound(value, SceneWireLimits.DisplayNameMax);
        return bound.Length == 0 ? null : bound;
    }

    static void AddFact(Dictionary<string, object> facts, string id, object dto)
    {
        if (facts.ContainsKey(id))
            return;
        facts[id] = JsonSerializer.SerializeToElement(dto, ProtocolJson.Options);
    }
}

/// <summary>
/// Epoch inventaris per screen. Session_id mengikuti observation epoch
/// scene (hello/reconnect). Revisi domain independen. Snapshot on-demand,
/// bukan 10 Hz. Fingerprint menekan state yang tidak berubah; state yang
/// tidak berubah hanya dikirim ulang lewat <see cref="RefreshDue"/> pada
/// revisi yang SAMA, dibatasi interval.
/// </summary>
public sealed class InventoryObservationScheduler
{
    long requestId;
    long epoch = 1;

    public string SessionId { get; private set; } = "";

    public long DomainRevision { get; private set; }

    public bool SnapshotNeeded { get; private set; }

    public bool HasSession => SessionId.Length > 0;

    public string? LastSentFingerprint { get; private set; }

    public long RequestId => this.requestId;

    public long Epoch => this.epoch;

    public SceneAckToken CurrentAckToken() =>
        new(SessionId, this.epoch, this.requestId, this.DomainRevision);

    public void BindSession(string sessionId)
    {
        SessionId = sessionId ?? "";
        this.DomainRevision = 0;
        this.LastSentFingerprint = null;
        this.LastSentUnixMs = null;
        if (HasSession)
            this.RequestSnapshot();
        else
            this.SnapshotNeeded = false;
    }

    public void OnReturnedToTitle()
    {
        this.epoch++;
        this.SessionId = "";
        this.DomainRevision = 0;
        this.SnapshotNeeded = false;
        this.LastSentFingerprint = null;
        this.LastSentUnixMs = null;
        this.requestId++;
    }

    public void RequestSnapshot()
    {
        this.SnapshotNeeded = true;
        this.requestId++;
    }

    /// <summary>
    /// 10 Hz dirty-check. Fingerprint sama dengan kiriman terakhir tidak
    /// meminta snapshot baru. Bukan polling emit; hanya menandai dirty.
    /// </summary>
    public void ObserveFingerprint(string fingerprint)
    {
        if (!HasSession)
            return;
        if (this.LastSentFingerprint is not null
            && string.Equals(this.LastSentFingerprint, fingerprint, StringComparison.Ordinal))
            return;
        this.RequestSnapshot();
    }

    /// <summary>
    /// Event InventoryChanged yang tidak mengubah fakta semantik tidak boleh
    /// mengonsumsi revisi atau mengirim ulang snapshot.
    /// </summary>
    public bool TrySuppressUnchanged(string fingerprint)
    {
        if (this.LastSentFingerprint is not null
            && string.Equals(this.LastSentFingerprint, fingerprint, StringComparison.Ordinal))
        {
            this.SnapshotNeeded = false;
            return true;
        }
        return false;
    }

    public long PeekNextRevision() => this.DomainRevision + 1;

    public long ReserveSnapshotRevision()
    {
        this.DomainRevision++;
        return this.DomainRevision;
    }

    /// <summary>
    /// Unix ms of the last acknowledged inventory send (snapshot or refresh),
    /// supplied by the caller; null until the first ack after BindSession.
    /// </summary>
    public long? LastSentUnixMs { get; private set; }

    /// <summary>
    /// Unchanged-snapshot refresh (M1 attempt 8, 2026-10-06). The Python runtime
    /// treats progress.inventory as fresh only for 2 s after receipt, so an
    /// unchanged inventory is re-sent at its CURRENT DomainRevision (never
    /// reserved, never incremented) once <paramref name="intervalMs"/> has passed
    /// since the last acknowledged send. Never due while a real snapshot is
    /// needed, before the first ack after BindSession, for a fingerprint other
    /// than the last sent one, or when intervalMs &lt;= 0 (disabled). A clock that
    /// moved backwards counts as due so the refresh cannot stall.
    /// </summary>
    public bool RefreshDue(string fingerprint, long nowUnixMs, int intervalMs) =>
        intervalMs > 0
        && this.HasSession
        && !this.SnapshotNeeded
        && this.DomainRevision > 0
        && this.LastSentFingerprint is not null
        && string.Equals(this.LastSentFingerprint, fingerprint, StringComparison.Ordinal)
        && this.LastSentUnixMs is long last
        && (nowUnixMs - last >= intervalMs || nowUnixMs < last);

    public bool TryMarkSnapshotSent(SceneAckToken token, string fingerprint, long? nowUnixMs = null)
    {
        if (!IsCurrentAck(token))
            return false;
        this.SnapshotNeeded = false;
        this.LastSentFingerprint = fingerprint;
        if (nowUnixMs is long now)
            this.LastSentUnixMs = now;
        return true;
    }

    bool IsCurrentAck(SceneAckToken token) =>
        this.HasSession
        && string.Equals(token.SessionId, SessionId, StringComparison.Ordinal)
        && token.Epoch == this.epoch
        && token.RequestId == this.requestId
        && token.Revision == this.DomainRevision;
}
