using System.Text.Json;
using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Serialisasi kawat. Encoder UTF-8 longgar supaya batas
/// <see cref="SceneWire.Bound"/> dalam byte UTF-8 = ukuran di NDJSON,
/// termasuk code point 4-byte. Lihat <see cref="Utf8WireJsonEncoder"/>.
/// </summary>
public static class ProtocolJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        Encoder = Utf8WireJsonEncoder.Instance,
    };
}

/// <summary>
/// BRAIN_OBSERVATION — kontrak OBS-0.
///
/// Terpisah dari <see cref="StatePayload"/> (MOTOR_STATE / ev=state).
/// Jangan memindahkan map/NPC/crop/inventory penuh ke state 10 Hz.
/// Jangan men-serialize graf objek Stardew mentah. Body bersifat opaq;
/// adapter domain diisi OBS-1+.
///
/// Canonical: docs/stardew/OBS-0-CONTRACT.md
/// </summary>
public static class ObservationProvenance
{
    public const string LocalAuthoritative = "LOCAL_AUTHORITATIVE";
    public const string ActiveSynced = "ACTIVE_SYNCED";
    public const string TeamSynced = "TEAM_SYNCED";
    public const string StaticData = "STATIC_DATA";
    public const string ShadowStalePossible = "SHADOW_STALE_POSSIBLE";
    public const string Derived = "DERIVED";
    public const string Unknown = "UNKNOWN";

    public static bool IsLiveAuthoritative(string provenance) =>
        provenance is LocalAuthoritative or ActiveSynced or TeamSynced;

    /// <summary>
    /// Farmhand shadow location tidak boleh diklaim authoritative.
    /// </summary>
    public static bool IsValidAuthorityClaim(string provenance, bool? authoritative) =>
        !string.Equals(provenance, ShadowStalePossible, StringComparison.Ordinal)
        || authoritative != true;
}

/// <summary>
/// Produk sah untuk <c>ev=observation</c> / <see cref="ObservationPayload"/>.
/// <c>state.fast</c> adalah nama konseptual MOTOR_STATE pada <c>ev=state</c>
/// (<see cref="StatePayload"/>), bukan product observation.
/// </summary>
public static class ObservationProduct
{
    public const string Scene = "scene";
    public const string Ui = "ui";
    public const string Progress = "progress";
    public const string Knowledge = "knowledge";
}

public static class ObservationKind
{
    public const string Snapshot = "snapshot";
    public const string Delta = "delta";
    public const string Error = "error";
    public const string Manifest = "manifest";
}

public static class ObservationDomain
{
    public const string SceneCurrentLocation = "scene.current_location";
    public const string UiActiveMenu = "ui.active_menu";
    /// <summary>
    /// G3-0: snapshot konteks UI sekarang. Vocabulary facts = G2
    /// <c>InspectContextQuery.BuildSnapshot</c>. Bukan history HUD/chat/letter.
    /// </summary>
    public const string UiContext = "ui.context";
    /// <summary>
    /// G3-1: current + bounded recent presented-text evidence. Product
    /// <see cref="ObservationProduct.Ui"/>. Bukan HUD/chat/letter, bukan
    /// permission/target authority — itu tetap <see cref="UiContext"/>.
    /// </summary>
    public const string UiText = "ui.text";
    public const string ProgressPlayer = "progress.player";
    /// <summary>
    /// OBS-3A: snapshot inventaris/peralatan farmer ARTI.
    /// OBS-0 membekukan starter <see cref="ProgressPlayer"/>; domain baru
    /// diizinkan tanpa mengganti mesin revisi. Inventaris punya revisi
    /// sendiri supaya skill/quest nanti tidak ikut naik.
    /// </summary>
    public const string ProgressInventory = "progress.inventory";
    public const string KnowledgeCatalog = "knowledge.catalog";
    public const string KnowledgeWorld = "knowledge.world";
}

public static class ObservationEvent
{
    public const string Name = "observation";
}

public sealed record TruncationInfo
{
    [JsonPropertyName("truncated")]
    public bool Truncated { get; init; }

    [JsonPropertyName("observed_count")]
    public int ObservedCount { get; init; }

    [JsonPropertyName("sent_count")]
    public int SentCount { get; init; }
}

public sealed record ObservationErrorInfo
{
    [JsonPropertyName("reason")]
    public string Reason { get; init; } = "";

    [JsonPropertyName("detail")]
    public string? Detail { get; init; }
}

/// <summary>
/// Isi opaq. Grammar per kind (fail closed di receiver Python):
/// snapshot = facts saja; delta = upserts/removes; manifest = catalog_id;
/// error = body null.
/// </summary>
public sealed record ObservationBody
{
    [JsonPropertyName("facts")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public IReadOnlyDictionary<string, object>? Facts { get; init; }

    [JsonPropertyName("upserts")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public IReadOnlyDictionary<string, object>? Upserts { get; init; }

    [JsonPropertyName("removes")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public IReadOnlyList<string>? Removes { get; init; }

    [JsonPropertyName("catalog_id")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? CatalogId { get; init; }

    [JsonPropertyName("game_version")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? GameVersion { get; init; }

    [JsonPropertyName("content_hash")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ContentHash { get; init; }

    [JsonPropertyName("entry_count")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? EntryCount { get; init; }
}

public sealed record ObservationPayload
{
    [JsonPropertyName("session_id")]
    public string SessionId { get; init; } = "";

    [JsonPropertyName("product")]
    public string Product { get; init; } = "";

    [JsonPropertyName("domain")]
    public string Domain { get; init; } = "";

    [JsonPropertyName("kind")]
    public string Kind { get; init; } = "";

    [JsonPropertyName("domain_revision")]
    public long DomainRevision { get; init; }

    [JsonPropertyName("provenance")]
    public string Provenance { get; init; } = ObservationProvenance.Unknown;

    [JsonPropertyName("authoritative")]
    public bool? Authoritative { get; init; }

    [JsonPropertyName("available")]
    public bool Available { get; init; } = true;

    [JsonPropertyName("error")]
    public ObservationErrorInfo? Error { get; init; }

    [JsonPropertyName("truncation")]
    public TruncationInfo? Truncation { get; init; }

    [JsonPropertyName("body")]
    public ObservationBody? Body { get; init; }
}

/// <summary>
/// Amplop <c>observation</c>. Bentuk luar HARUS sama dengan hello/state
/// (protocol/ev/seq/player_id/screen_id) supaya SequenceGate yang sama
/// bisa dipakai.
/// </summary>
public sealed record ObservationEnvelope
{
    [JsonPropertyName("protocol")]
    public int Protocol { get; init; } = 1;

    [JsonPropertyName("ev")]
    public string Event { get; init; } = ObservationEvent.Name;

    [JsonPropertyName("seq")]
    public long Sequence { get; init; }

    [JsonPropertyName("player_id")]
    public long PlayerId { get; init; }

    [JsonPropertyName("screen_id")]
    public int ScreenId { get; init; }

    [JsonPropertyName("payload")]
    public ObservationPayload Payload { get; init; } = new();

    public static ObservationEnvelope Create(
        long seq,
        long playerId,
        int screenId,
        ObservationPayload payload
    ) => new()
    {
        Sequence = seq,
        PlayerId = playerId,
        ScreenId = screenId,
        Payload = payload,
    };
}

/// <summary>
/// G3-0: merakit <c>ui.context</c> dari facts G2
/// <see cref="InspectContextQuery.BuildSnapshot"/>. SMAPI-free. Bukan
/// store/receiver/transport kedua dan bukan antrean.
/// </summary>
public static class UiContextSnapshotBuilder
{
    public static ObservationPayload Snapshot(
        string sessionId,
        long domainRevision,
        IReadOnlyDictionary<string, object> g2Facts
    )
    {
        Dictionary<string, object> facts = WireFacts(g2Facts);
        return new ObservationPayload
        {
            SessionId = sessionId,
            Product = ObservationProduct.Ui,
            Domain = ObservationDomain.UiContext,
            Kind = ObservationKind.Snapshot,
            DomainRevision = domainRevision,
            Provenance = ObservationProvenance.LocalAuthoritative,
            Authoritative = true,
            Available = true,
            Error = null,
            Truncation = new TruncationInfo
            {
                Truncated = false,
                ObservedCount = facts.Count,
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
        Product = ObservationProduct.Ui,
        Domain = ObservationDomain.UiContext,
        Kind = ObservationKind.Error,
        DomainRevision = domainRevision,
        Provenance = ObservationProvenance.Unknown,
        Authoritative = false,
        Available = false,
        Error = new ObservationErrorInfo { Reason = reason, Detail = detail },
        Truncation = null,
        Body = null,
    };

    public static string Fingerprint(IReadOnlyDictionary<string, object> g2Facts)
    {
        return JsonSerializer.Serialize(g2Facts, ProtocolJson.Options);
    }

    public static Dictionary<string, object> WireFacts(IReadOnlyDictionary<string, object> g2Facts)
    {
        Dictionary<string, object> facts = new(StringComparer.Ordinal);
        foreach (KeyValuePair<string, object> pair in g2Facts)
            facts[pair.Key] = JsonSerializer.SerializeToElement(pair.Value, ProtocolJson.Options);
        return facts;
    }
}

/// <summary>
/// Accumulator UI sekarang per screen: latest-wins, bounded, tanpa antrean.
/// Capture/dirty boleh maju saat kiriman sebelumnya masih inflight.
/// Consume hanya untuk satu snapshot kawat; HOST/split-screen gagal tertutup.
/// </summary>
public sealed class UiContextAccumulator
{
    Dictionary<string, object>? currentFacts;
    string? lastFingerprint;
    string? inflightFingerprint;
    bool dirty;
    bool unsupported;
    bool inflightRefresh;

    public string SessionId { get; private set; } = "";

    /// <summary>
    /// Unix ms of the last acknowledged ui.context send (snapshot or refresh),
    /// supplied by the caller; null until the first ack after BindSession.
    /// </summary>
    public long? LastSentUnixMs { get; private set; }

    public bool InflightRefresh => this.inflightRefresh;

    public long DomainRevision { get; private set; }

    public bool Dirty => this.dirty;

    public bool Unsupported => this.unsupported;

    public bool HasSession => this.SessionId.Length > 0;

    public string? InflightFingerprint => this.inflightFingerprint;

    public IReadOnlyDictionary<string, object>? CurrentFacts => this.currentFacts;

    public void BindSession(string sessionId)
    {
        string next = sessionId ?? "";
        bool changed = !string.Equals(this.SessionId, next, StringComparison.Ordinal);
        this.SessionId = next;
        if (!changed)
            return;
        this.DomainRevision = 0;
        this.lastFingerprint = null;
        this.inflightFingerprint = null;
        this.inflightRefresh = false;
        this.LastSentUnixMs = null;
        if (this.HasSession && this.currentFacts is not null && !this.unsupported)
            this.dirty = true;
        else
            this.dirty = false;
    }

    public void OnReturnedToTitle()
    {
        this.SessionId = "";
        this.DomainRevision = 0;
        this.currentFacts = null;
        this.lastFingerprint = null;
        this.inflightFingerprint = null;
        this.inflightRefresh = false;
        this.LastSentUnixMs = null;
        this.dirty = false;
        this.unsupported = false;
    }

    /// <summary>
    /// HOST / split-screen: jangan memakai fakta layar lain. Tidak emit.
    /// </summary>
    public void AdvanceUnsupported()
    {
        this.unsupported = true;
        this.currentFacts = null;
        this.dirty = false;
        this.inflightFingerprint = null;
        this.inflightRefresh = false;
    }

    public void Advance(IReadOnlyDictionary<string, object> facts)
    {
        this.unsupported = false;
        Dictionary<string, object> copy = new(facts, StringComparer.Ordinal);
        this.currentFacts = copy;
        string fingerprint = UiContextSnapshotBuilder.Fingerprint(copy);
        if (!string.Equals(this.lastFingerprint, fingerprint, StringComparison.Ordinal)
            && !string.Equals(this.inflightFingerprint, fingerprint, StringComparison.Ordinal))
        {
            this.dirty = true;
        }
    }

    public ObservationPayload? ConsumeDirtySnapshot()
    {
        if (this.unsupported
            || !this.dirty
            || !this.HasSession
            || this.currentFacts is null)
        {
            return null;
        }

        this.DomainRevision++;
        this.inflightFingerprint = UiContextSnapshotBuilder.Fingerprint(this.currentFacts);
        this.inflightRefresh = false;
        this.dirty = false;
        return UiContextSnapshotBuilder.Snapshot(
            this.SessionId,
            this.DomainRevision,
            this.currentFacts
        );
    }

    /// <summary>
    /// Unchanged-snapshot refresh (M1 attempt 8, 2026-10-06). The Python runtime
    /// treats ui.context as fresh only for 2 s after receipt, so the last
    /// acknowledged facts are re-sent at the SAME DomainRevision (never
    /// incremented) once <paramref name="intervalMs"/> has passed since the last
    /// acknowledged send. Only when supported, bound, not dirty, nothing inflight,
    /// the current facts equal the last sent ones, and at least one send has been
    /// acknowledged since BindSession (so the first send is always the normal
    /// snapshot). intervalMs &lt;= 0 disables. A clock that moved backwards counts as
    /// due so the refresh cannot stall.
    /// </summary>
    public ObservationPayload? ConsumeRefreshSnapshot(long nowUnixMs, int intervalMs)
    {
        if (intervalMs <= 0
            || this.unsupported
            || this.dirty
            || !this.HasSession
            || this.currentFacts is null
            || this.inflightFingerprint is not null
            || this.lastFingerprint is null
            || this.DomainRevision <= 0
            || this.LastSentUnixMs is not long last
            || (nowUnixMs - last < intervalMs && nowUnixMs >= last))
        {
            return null;
        }

        string fingerprint = UiContextSnapshotBuilder.Fingerprint(this.currentFacts);
        if (!string.Equals(this.lastFingerprint, fingerprint, StringComparison.Ordinal))
            return null;

        this.inflightFingerprint = fingerprint;
        this.inflightRefresh = true;
        return UiContextSnapshotBuilder.Snapshot(
            this.SessionId,
            this.DomainRevision,
            this.currentFacts
        );
    }

    public void AckSent(long? nowUnixMs = null)
    {
        if (this.inflightFingerprint is not null && nowUnixMs is long sentAt)
            this.LastSentUnixMs = sentAt;
        this.lastFingerprint = this.inflightFingerprint;
        this.inflightFingerprint = null;
        this.inflightRefresh = false;
        if (this.currentFacts is not null)
        {
            string now = UiContextSnapshotBuilder.Fingerprint(this.currentFacts);
            if (!string.Equals(this.lastFingerprint, now, StringComparison.Ordinal))
                this.dirty = true;
        }
    }

    public void NackSent()
    {
        bool refresh = this.inflightRefresh;
        this.inflightFingerprint = null;
        this.inflightRefresh = false;
        if (this.currentFacts is null || this.unsupported || !this.HasSession)
            return;
        // A failed refresh carried facts already acknowledged at this revision:
        // nothing new to deliver, so no dirty (which would burn a revision). The
        // next refresh retries once due; a real change was already marked dirty
        // by Advance.
        if (refresh && string.Equals(this.lastFingerprint,
                UiContextSnapshotBuilder.Fingerprint(this.currentFacts), StringComparison.Ordinal))
            return;
        this.dirty = true;
    }
}
