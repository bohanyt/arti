using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// OBS-2A: model delta scene murni. Diff dua peta fact ternormalisasi
/// menjadi <c>upserts</c>/<c>removes</c> deterministik, lalu merakit
/// payload <c>kind=delta</c>.
///
/// Tidak membaca runtime Stardew, tidak memasang handler SMAPI, tidak memancarkan
/// delta dari ModEntry. Reconciliation: snapshot R + delta R+1 = snapshot
/// R+1 yang di-capture ulang, untuk patch yang tidak terpotong.
///
/// Canonical: docs/stardew/OBS-2-SCENE-DELTAS.md
/// </summary>
public static class SceneDeltaCaps
{
    /// <summary>
    /// Batas operasi patch (upsert + remove) per pesan. Lebih dari ini
    /// → truncated → store stale/resync. Sengaja lebih ketat dari cap
    /// snapshot OBS-1 supaya pergantian lokasi penuh tidak dipaksa lewat
    /// delta (OBS-1 sudah snapshot-on-warp).
    /// </summary>
    public const int MaxPatchOps = 48;

    /// <summary>
    /// Sama dengan OBS-1 / receiver. OBS-2A tidak boleh menaikkan ini.
    /// </summary>
    public const int ReceiverMaxLineBytes = SceneObservationCaps.ReceiverMaxLineBytes;
}

/// <summary>
/// Patch scene: upsert mengganti fact utuh per ID, remove menghapus ID.
/// Observed/sent adalah hitungan patch pesan ini, bukan total domain.
/// </summary>
public sealed record SceneDeltaPatch
{
    public IReadOnlyDictionary<string, object> Upserts { get; init; } =
        new SortedDictionary<string, object>(StringComparer.Ordinal);

    public IReadOnlyList<string> Removes { get; init; } = Array.Empty<string>();

    public int ObservedCount { get; init; }

    public int SentCount { get; init; }

    public bool Truncated { get; init; }

    public bool IsEmpty => Upserts.Count == 0 && Removes.Count == 0;
}

/// <summary>
/// Kesetaraan fact lewat JSON kanonik (kunci objek diurutkan Ordinal).
/// Urutan properti di DTO / JsonElement tidak boleh memunculkan upsert palsu.
/// </summary>
public static class SceneFactCanonical
{
    public static bool Equal(object? left, object? right) =>
        Json(left) == Json(right);

    public static string Json(object? value)
    {
        JsonElement element = ToElement(value);
        var opts = new JsonWriterOptions
        {
            Encoder = Utf8WireJsonEncoder.Instance,
            Indented = false,
        };
        using MemoryStream stream = new();
        using (Utf8JsonWriter writer = new(stream, opts))
            Write(writer, element);
        return Encoding.UTF8.GetString(stream.ToArray());
    }

    /// <summary>
    /// Salinan JsonElement imutabel dengan object keys terurut rekursif.
    /// Dipakai baseline runtime supaya tidak menyimpan referensi DTO mutable
    /// atau bergantung pada insertion order properti.
    /// </summary>
    public static JsonElement Normalize(object? value)
    {
        using JsonDocument doc = JsonDocument.Parse(Json(value));
        return doc.RootElement.Clone();
    }

    public static JsonElement ToElement(object? value)
    {
        if (value is null)
            return JsonSerializer.SerializeToElement((object?)null, ProtocolJson.Options);
        if (value is JsonElement element)
            return element.Clone();
        return JsonSerializer.SerializeToElement(value, ProtocolJson.Options);
    }

    static void Write(Utf8JsonWriter writer, JsonElement element)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                writer.WriteStartObject();
                foreach (JsonProperty prop in element.EnumerateObject()
                             .OrderBy(p => p.Name, StringComparer.Ordinal))
                {
                    writer.WritePropertyName(prop.Name);
                    Write(writer, prop.Value);
                }
                writer.WriteEndObject();
                break;
            case JsonValueKind.Array:
                writer.WriteStartArray();
                foreach (JsonElement item in element.EnumerateArray())
                    Write(writer, item);
                writer.WriteEndArray();
                break;
            default:
                element.WriteTo(writer);
                break;
        }
    }
}

/// <summary>
/// Diff + apply murni. Tidak menyentuh revisi/session — itu tugas store
/// dan <see cref="SceneDeltaBuilder"/>.
/// </summary>
public static class SceneDeltaDiff
{
    public static SceneDeltaPatch Compute(
        IReadOnlyDictionary<string, object>? before,
        IReadOnlyDictionary<string, object>? after,
        int maxPatchOps = SceneDeltaCaps.MaxPatchOps
    )
    {
        if (maxPatchOps < 0)
            throw new ArgumentOutOfRangeException(nameof(maxPatchOps));

        Dictionary<string, object> prev = Copy(before);
        Dictionary<string, object> next = Copy(after);

        SortedSet<string> ids = new(StringComparer.Ordinal);
        foreach (string key in prev.Keys)
            ids.Add(key);
        foreach (string key in next.Keys)
            ids.Add(key);

        SortedDictionary<string, object> upserts = new(StringComparer.Ordinal);
        List<string> removes = new();

        foreach (string id in ids)
        {
            bool had = prev.TryGetValue(id, out object? oldValue);
            bool has = next.TryGetValue(id, out object? newValue);
            if (!had && has)
                upserts[id] = newValue!;
            else if (had && !has)
                removes.Add(id);
            else if (had && has && !SceneFactCanonical.Equal(oldValue, newValue))
                upserts[id] = newValue!;
        }

        removes.Sort(StringComparer.Ordinal);
        int observed = upserts.Count + removes.Count;
        if (observed == 0)
        {
            return new SceneDeltaPatch
            {
                Upserts = new SortedDictionary<string, object>(StringComparer.Ordinal),
                Removes = Array.Empty<string>(),
                ObservedCount = 0,
                SentCount = 0,
                Truncated = false,
            };
        }

        int remaining = maxPatchOps;
        List<string> sentRemoves = new();
        foreach (string id in removes)
        {
            if (remaining <= 0)
                break;
            sentRemoves.Add(id);
            remaining--;
        }

        SortedDictionary<string, object> sentUpserts = new(StringComparer.Ordinal);
        foreach (KeyValuePair<string, object> kv in upserts)
        {
            if (remaining <= 0)
                break;
            sentUpserts[kv.Key] = kv.Value;
            remaining--;
        }

        int sent = sentUpserts.Count + sentRemoves.Count;
        bool truncated = sent < observed;
        return new SceneDeltaPatch
        {
            Upserts = sentUpserts,
            Removes = sentRemoves,
            ObservedCount = observed,
            SentCount = sent,
            Truncated = truncated,
        };
    }

    /// <summary>
    /// Remove dulu, baru upsert — sama dengan <c>RichObservationStore</c>.
    /// ID yang tidak ada di peta diabaikan. Hasil kunci terurut Ordinal.
    /// </summary>
    public static IReadOnlyDictionary<string, object> Apply(
        IReadOnlyDictionary<string, object>? facts,
        SceneDeltaPatch patch
    )
    {
        SortedDictionary<string, object> next = new(StringComparer.Ordinal);
        foreach (KeyValuePair<string, object> kv in Copy(facts))
            next[kv.Key] = kv.Value;
        foreach (string id in patch.Removes)
            next.Remove(id);
        foreach (KeyValuePair<string, object> kv in patch.Upserts)
        {
            if (string.IsNullOrEmpty(kv.Key))
                continue;
            next[kv.Key] = kv.Value;
        }
        return next;
    }

    public static IReadOnlyDictionary<string, object> Apply(
        IReadOnlyDictionary<string, object>? facts,
        IReadOnlyDictionary<string, object>? upserts,
        IReadOnlyList<string>? removes
    ) => Apply(
        facts,
        new SceneDeltaPatch
        {
            Upserts = CopySorted(upserts),
            Removes = removes is null
                ? Array.Empty<string>()
                : removes.Where(id => !string.IsNullOrEmpty(id)).ToArray(),
            ObservedCount = (upserts?.Count ?? 0) + (removes?.Count ?? 0),
            SentCount = (upserts?.Count ?? 0) + (removes?.Count ?? 0),
            Truncated = false,
        }
    );

    static Dictionary<string, object> Copy(IReadOnlyDictionary<string, object>? source)
    {
        Dictionary<string, object> copy = new(StringComparer.Ordinal);
        if (source is null)
            return copy;
        foreach (KeyValuePair<string, object> kv in source)
        {
            if (string.IsNullOrEmpty(kv.Key))
                continue;
            copy[kv.Key] = kv.Value;
        }
        return copy;
    }

    static SortedDictionary<string, object> CopySorted(
        IReadOnlyDictionary<string, object>? source
    )
    {
        SortedDictionary<string, object> copy = new(StringComparer.Ordinal);
        if (source is null)
            return copy;
        foreach (KeyValuePair<string, object> kv in source)
        {
            if (string.IsNullOrEmpty(kv.Key))
                continue;
            copy[kv.Key] = kv.Value;
        }
        return copy;
    }
}

/// <summary>
/// Merakit ObservationPayload kind=delta. Revision keluaran = R+1.
/// Delta kosong sah dan tetap memajukan revision. Delta terpotong
/// (cap operasi ATAU envelope ≥ MAX_LINE_BYTES) menandai truncated
/// supaya store stale/resync — jangan diam-diam membuang op.
/// </summary>
public static class SceneDeltaBuilder
{
    public static ObservationPayload Delta(
        string sessionId,
        long previousRevision,
        SceneAuthorityDecision authority,
        IReadOnlyDictionary<string, object>? before,
        IReadOnlyDictionary<string, object>? after,
        int maxPatchOps = SceneDeltaCaps.MaxPatchOps
    )
    {
        SceneDeltaPatch patch = SceneDeltaDiff.Compute(before, after, maxPatchOps);
        ObservationPayload payload = FromPatch(sessionId, previousRevision, authority, patch);
        if (FitsReceiver(payload))
            return payload;

        // Terlalu besar untuk kawat: jangan naikkan MAX_LINE_BYTES.
        // Kirim delta kosong terpotong → stale/resync.
        SceneDeltaPatch marker = new()
        {
            Upserts = new SortedDictionary<string, object>(StringComparer.Ordinal),
            Removes = Array.Empty<string>(),
            ObservedCount = patch.ObservedCount,
            SentCount = 0,
            Truncated = patch.ObservedCount > 0,
        };
        return FromPatch(sessionId, previousRevision, authority, marker);
    }

    public static ObservationPayload FromPatch(
        string sessionId,
        long previousRevision,
        SceneAuthorityDecision authority,
        SceneDeltaPatch patch
    )
    {
        TruncationInfo? truncation = null;
        if (patch.Truncated)
        {
            truncation = new TruncationInfo
            {
                Truncated = true,
                ObservedCount = patch.ObservedCount,
                SentCount = patch.SentCount,
            };
        }
        else if (!patch.IsEmpty)
        {
            truncation = new TruncationInfo
            {
                Truncated = false,
                ObservedCount = patch.ObservedCount,
                SentCount = patch.SentCount,
            };
        }

        return new ObservationPayload
        {
            SessionId = sessionId,
            Product = ObservationProduct.Scene,
            Domain = ObservationDomain.SceneCurrentLocation,
            Kind = ObservationKind.Delta,
            DomainRevision = previousRevision + 1,
            Provenance = authority.Provenance,
            Authoritative = authority.Authoritative,
            Available = true,
            Error = null,
            Truncation = truncation,
            Body = new ObservationBody
            {
                Upserts = patch.Upserts,
                Removes = patch.Removes,
            },
        };
    }

    static bool FitsReceiver(ObservationPayload payload)
    {
        ObservationEnvelope env = ObservationEnvelope.Create(
            long.MaxValue, long.MaxValue, int.MaxValue, payload);
        string json = JsonSerializer.Serialize(env, ProtocolJson.Options);
        return Encoding.UTF8.GetByteCount(json) < SceneDeltaCaps.ReceiverMaxLineBytes;
    }
}
