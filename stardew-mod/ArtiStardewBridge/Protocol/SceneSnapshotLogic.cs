using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Batas string di kawat OBS-1, dalam <b>byte UTF-8 terserialisasi</b>
/// (bukan UTF-16 <c>string.Length</c>). Dihitung supaya snapshot terikat
/// tetap di bawah <see cref="SceneObservationCaps.ReceiverMaxLineBytes"/>.
/// Jangan menaikkan MAX_LINE_BYTES receiver di PR ini.
///
/// Field identity (<c>location_id</c>, <c>target_location</c>, qid,
/// internal name, terrain type di ID) memakai
/// <see cref="SceneIdentity.Token"/> (ASCII, collision-safe dari raw utuh).
/// Bound UTF-8 hanya untuk metadata tampilan.
/// </summary>
public static class SceneWireLimits
{
    public const int IdentityTokenMax = 32;
    public const int LocationKindMax = 16;
    public const int NameMax = 24;
    public const int DisplayNameMax = 24;
    public const int TreeTypeMax = 24;
    public const int ClumpTypeMax = 24;
    /// <summary>WS3a: door fact Action token (LockedDoorWarp = 14 bytes).</summary>
    public const int DoorActionMax = 16;
}

public static class SceneWire
{
    public static int Utf8Bytes(string? value) =>
        string.IsNullOrEmpty(value) ? 0 : Encoding.UTF8.GetByteCount(value);

    /// <summary>
    /// Byte UTF-8 isi string JSON kawat (tanpa kutip pembungkus), memakai
    /// <see cref="ProtocolJson.Options"/>. Karakter RFC-escape
    /// (<c>"</c>, <c>\</c>, U+0000–U+001F) lebih mahal daripada byte sumber.
    /// </summary>
    public static int SerializedUtf8Bytes(string? value)
    {
        if (string.IsNullOrEmpty(value))
            return 0;
        string json = JsonSerializer.Serialize(value, ProtocolJson.Options);
        int bytes = Encoding.UTF8.GetByteCount(json);
        return bytes >= 2 ? bytes - 2 : 0;
    }

    /// <summary>
    /// Potong ke paling banyak <paramref name="maxUtf8Bytes"/> byte UTF-8
    /// di JSON kawat, tanpa memotong sekuens UTF-8 atau pasangan surrogat.
    ///
    /// Encoder kustom menahan code point 4-byte tetap mentah, tetapi escape
    /// RFC tetap 2×/6×. Bound yang hanya menghitung byte sumber lolos
    /// 24×U+0001 per field metadata — snapshot cap-penuh terukur 90066 byte,
    /// di atas MAX_LINE_BYTES. Jangan naikkan limit receiver; ketatkan Bound.
    /// </summary>
    public static string Bound(string? value, int maxUtf8Bytes)
    {
        if (string.IsNullOrEmpty(value) || maxUtf8Bytes <= 0)
            return "";
        string cut = CutToUtf8(value, maxUtf8Bytes);
        while (cut.Length > 0 && SerializedUtf8Bytes(cut) > maxUtf8Bytes)
            cut = CutToUtf8(cut, Utf8Bytes(cut) - 1);
        return cut;
    }

    static string CutToUtf8(string value, int maxUtf8Bytes)
    {
        if (maxUtf8Bytes <= 0)
            return "";
        int byteCount = Encoding.UTF8.GetByteCount(value);
        if (byteCount <= maxUtf8Bytes)
            return value;

        byte[] bytes = Encoding.UTF8.GetBytes(value);
        int len = Math.Min(maxUtf8Bytes, bytes.Length);
        while (len > 0 && len < bytes.Length && (bytes[len] & 0xC0) == 0x80)
            len--;
        string cut = Encoding.UTF8.GetString(bytes, 0, len);
        if (cut.Length > 0 && char.IsHighSurrogate(cut[^1]))
            cut = cut[..^1];
        return cut;
    }
}

/// <summary>
/// ID semantik OBS-1. Display/localized text bukan identity.
///
/// Setiap bagian di-percent-encode ke ASCII (delimiter + non-ASCII sebagai
/// byte UTF-8 %XX), lalu digabung dengan `:`. Token dihitung dari raw
/// utuh — jangan Bound/truncate dulu. Kalau hasil encode melebihi
/// <see cref="SceneWireLimits.IdentityTokenMax"/> byte, prefix terbatas
/// + sidik SHA-256 dari token asli.
/// </summary>
public static class SceneIdentity
{
    public const int MaxToken = SceneWireLimits.IdentityTokenMax;

    public static string Token(string? raw)
    {
        if (string.IsNullOrEmpty(raw))
            return "_";
        string trimmed = TrimSpacesAndTabs(raw);
        if (trimmed.Length == 0)
            return "_";
        string encoded = PercentEncode(trimmed);
        if (encoded.Length == 0)
            return "_";
        // encoded selalu ASCII; ukur byte UTF-8 kawat, bukan UTF-16 Length.
        if (SceneWire.Utf8Bytes(encoded) <= MaxToken)
            return encoded;

        string hash = FingerprintHex(trimmed);
        int budget = MaxToken - 1 - SceneWire.Utf8Bytes(hash);
        if (budget < 1)
            return SceneWire.Utf8Bytes(hash) <= MaxToken ? hash : SceneWire.Bound(hash, MaxToken);
        string prefix = SceneWire.Bound(encoded, budget);
        return prefix + "~" + hash;
    }

    public static string SessionId(string saveToken, long playerId, long epoch) =>
        $"obs:{Token(saveToken)}:{playerId}:{epoch}";

    public static string LocationFactId(string locationId) =>
        $"loc:{Token(locationId)}";

    public static string ObjectFactId(string locationId, int x, int y, string qualifiedItemId) =>
        $"obj:{Token(locationId)}:{x},{y}:{Token(qualifiedItemId)}";

    public static string TerrainFactId(string locationId, int x, int y, string terrainType) =>
        $"tf:{Token(locationId)}:{x},{y}:{Token(terrainType)}";

    public static string NpcFactId(string locationId, string internalName, string? discriminator = null) =>
        discriminator is null
            ? $"npc:{Token(locationId)}:{Token(internalName)}"
            : $"npc:{Token(locationId)}:{Token(internalName)}:{Token(discriminator)}";

    public static string MonsterFactId(string locationId, string ephemeralId) =>
        $"mon:{Token(locationId)}:{Token(ephemeralId)}";

    public static string MonsterEphemeral(int ordinal) =>
        $"eph-{ordinal}";

    public static bool IsEphemeralMonsterId(string factId)
    {
        int last = factId.LastIndexOf(':');
        if (last < 0 || last + 1 >= factId.Length)
            return false;
        return factId.StartsWith("mon:", StringComparison.Ordinal)
            && factId[(last + 1)..].StartsWith("eph-", StringComparison.Ordinal);
    }

    public static string WarpFactId(
        string locationId, int sx, int sy, string target, int tx, int ty) =>
        $"warp:{Token(locationId)}:{sx},{sy}->{Token(target)}:{tx},{ty}";

    public static string DoorFactId(string locationId, int x, int y, string target) =>
        $"door:{Token(locationId)}:{x},{y}:{Token(target)}";

    /// <summary>WS3a: a building human door (absolute tile) leading to <paramref name="target"/> (indoors NameOrUniqueName).</summary>
    public static string BuildingDoorFactId(string locationId, int x, int y, string target) =>
        $"bdoor:{Token(locationId)}:{x},{y}:{Token(target)}";

    public static string ClumpFactId(string locationId, int x, int y, string discriminator) =>
        $"clump:{Token(locationId)}:{x},{y}:{Token(discriminator)}";

    static string TrimSpacesAndTabs(string s)
    {
        int start = 0;
        int end = s.Length - 1;
        while (start <= end && (s[start] == ' ' || s[start] == '\t'))
            start++;
        while (end >= start && (s[end] == ' ' || s[end] == '\t'))
            end--;
        return start > end ? "" : s[start..(end + 1)];
    }

    static string PercentEncode(string s)
    {
        StringBuilder sb = new(s.Length);
        Span<byte> buf = stackalloc byte[4];
        foreach (Rune rune in s.EnumerateRunes())
        {
            if (rune.Value < 128 && IsIdentitySafeAscii(rune.Value))
            {
                sb.Append((char)rune.Value);
                continue;
            }

            int n = rune.EncodeToUtf8(buf);
            for (int i = 0; i < n; i++)
            {
                sb.Append('%');
                sb.Append(buf[i].ToString("X2"));
            }
        }
        return sb.ToString();
    }

    static bool IsIdentitySafeAscii(int code) =>
        code is (>= '0' and <= '9')
            or (>= 'A' and <= 'Z')
            or (>= 'a' and <= 'z')
            or '.' or '_' or '(' or ')' or '-' or ',';

    static string FingerprintHex(string raw)
    {
        byte[] hash = SHA256.HashData(Encoding.UTF8.GetBytes(raw));
        return Convert.ToHexString(hash, 0, 8).ToLowerInvariant();
    }
}

/// <summary>
/// Kualifikasi item object di sisi protocol (tanpa Stardew). Capture runtime
/// memakai <c>ItemRegistry.QualifyItemId</c>; helper ini cadangan murni + tes.
/// Sudah qualified (`(X)...`) dibiarkan; selain itu diawali <c>(O)</c>.
/// </summary>
public static class SceneHarvestIds
{
    public static string QualifyObjectItemId(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
            return "";
        string trimmed = raw.Trim();
        string qualified = trimmed.StartsWith("(", StringComparison.Ordinal) && trimmed.Contains(')')
            ? trimmed
            : "(O)" + trimmed;
        return qualified;
    }
}

public readonly record struct SceneAuthorityDecision(
    string Provenance,
    bool Authoritative,
    bool CanSnapshot,
    string? ErrorReason
);

/// <summary>
/// Keputusan authority murni. Tidak membaca Game1.
/// Hanya lokasi farmer saat ini yang boleh jadi scene.current_location.
/// </summary>
public static class SceneAuthority
{
    public static SceneAuthorityDecision Classify(
        bool hasCurrentLocation,
        bool isCurrentFarmerLocation,
        bool locationIsActiveSynced
    )
    {
        if (!hasCurrentLocation)
            return new(ObservationProvenance.Unknown, false, false, "unavailable");
        if (!isCurrentFarmerLocation)
            return new(ObservationProvenance.Unknown, false, false, "not_authoritative");
        if (!locationIsActiveSynced)
            return new(ObservationProvenance.ShadowStalePossible, false, true, null);
        return new(ObservationProvenance.ActiveSynced, true, true, null);
    }
}

/// <summary>
/// Cap koleksi OBS-1. Sengaja ketat supaya snapshot terikat tetap
/// muat di MAX_LINE_BYTES receiver (64 KiB) tanpa menaikkan limit.
/// </summary>
public static class SceneObservationCaps
{
    public const int Warps = 16;
    public const int Doors = 16;
    public const int Objects = 40;
    public const int Terrain = 40;
    public const int Npcs = 16;
    public const int Monsters = 16;
    public const int Clumps = 16;
    /// <summary>WS3a: building human doors with an interior, nearest-first (8: worst-case snapshot headroom ~4.7 KB).</summary>
    public const int BuildingDoors = 8;
    public const int ReceiverMaxLineBytes = 64 * 1024;
}

/// <summary>
/// Satu kategori yang sudah dibatasi saat traversal: <see cref="Sent"/>
/// paling banyak = cap, <see cref="Observed"/> = semua entitas yang dilihat.
/// </summary>
public readonly record struct SceneFactBatch(IReadOnlyList<ScenePreparedFact> Sent, int Observed)
{
    public static SceneFactBatch Empty { get; } = new(Array.Empty<ScenePreparedFact>(), 0);

    public static SceneFactBatch From(IReadOnlyList<ScenePreparedFact>? items, int cap)
    {
        SceneFactBag bag = new(cap);
        if (items is null)
            return bag.ToBatch();
        foreach (ScenePreparedFact item in items)
            bag.TryAdd(() => item);
        return bag.ToBatch();
    }
}

/// <summary>
/// Kantong traversal: setiap tawaran menambah Observed, tetapi factory
/// (pembangun DTO) hanya dipanggil selama Sent masih di bawah cap.
/// </summary>
public sealed class SceneFactBag
{
    readonly List<ScenePreparedFact> sent;

    public SceneFactBag(int cap)
    {
        if (cap < 0)
            throw new ArgumentOutOfRangeException(nameof(cap));
        Cap = cap;
        this.sent = new List<ScenePreparedFact>(cap);
    }

    public int Cap { get; }

    public int Observed { get; private set; }

    public int FactoryInvocations { get; private set; }

    public IReadOnlyList<ScenePreparedFact> Sent => this.sent;

    public SceneFactBatch ToBatch() => new(this.sent, Observed);

    public bool TryAdd(Func<ScenePreparedFact> factory)
    {
        this.Observed++;
        if (this.sent.Count >= this.Cap)
            return false;
        this.sent.Add(factory());
        this.FactoryInvocations++;
        return true;
    }
}

/// <summary>
/// Merakit ObservationPayload snapshot/error dari DTO yang sudah di-capture.
/// Tidak menyentuh runtime Stardew. Builder menerima facts yang sudah
/// di-cap plus observed_count; tidak membangun DTO untuk entitas di atas cap.
/// </summary>
public static class SceneSnapshotBuilder
{
    public static ObservationPayload WithGather(ObservationPayload snapshot, ScenePreparedFact gather,
        ScenePreparedFact? affordances = null)
    {
        if (snapshot.Body?.Facts is null || snapshot.Truncation is null) return snapshot;
        var facts = snapshot.Body.Facts.ToDictionary(p => p.Key, p => p.Value);
        facts[gather.Id] = SceneFactCanonical.Normalize(gather.Dto);
        if (affordances is { } extra)
            facts[extra.Id] = SceneFactCanonical.Normalize(extra.Dto);
        int added = affordances is null ? 1 : 2;
        var result = snapshot with {
            Body = new ObservationBody { Facts = facts },
            Truncation = snapshot.Truncation with {
                ObservedCount = snapshot.Truncation.ObservedCount + added,
                SentCount = snapshot.Truncation.SentCount + added } };
        // Reserve envelope overhead without enlarging the receiver budget.
        if (JsonSerializer.SerializeToUtf8Bytes(result, ProtocolJson.Options).Length > SceneObservationCaps.ReceiverMaxLineBytes - 1024)
        {
            if (affordances is { } dropped)
            {
                facts.Remove(dropped.Id);
                result = result with { Truncation = result.Truncation! with { Truncated = true, SentCount = facts.Count } };
                if (JsonSerializer.SerializeToUtf8Bytes(result, ProtocolJson.Options).Length <= SceneObservationCaps.ReceiverMaxLineBytes - 1024)
                    return result;
            }
            facts[gather.Id] = SceneFactCanonical.Normalize(GatherFeedback.Unavailable("scene_payload_limit"));
            if (JsonSerializer.SerializeToUtf8Bytes(result, ProtocolJson.Options).Length > SceneObservationCaps.ReceiverMaxLineBytes - 1024)
                return Error(snapshot.SessionId, snapshot.DomainRevision, "result_too_large");
        }
        return result;
    }

    public static ObservationPayload Snapshot(
        string sessionId,
        long domainRevision,
        SceneAuthorityDecision authority,
        SceneLocationFact location,
        IReadOnlyList<ScenePreparedFact> warps,
        IReadOnlyList<ScenePreparedFact> doors,
        IReadOnlyList<ScenePreparedFact> objects,
        IReadOnlyList<ScenePreparedFact> terrain,
        IReadOnlyList<ScenePreparedFact> npcs,
        IReadOnlyList<ScenePreparedFact> monsters,
        IReadOnlyList<ScenePreparedFact> clumps
    ) => Snapshot(
        sessionId,
        domainRevision,
        authority,
        location,
        SceneFactBatch.From(warps, SceneObservationCaps.Warps),
        SceneFactBatch.From(doors, SceneObservationCaps.Doors),
        SceneFactBatch.From(objects, SceneObservationCaps.Objects),
        SceneFactBatch.From(terrain, SceneObservationCaps.Terrain),
        SceneFactBatch.From(npcs, SceneObservationCaps.Npcs),
        SceneFactBatch.From(monsters, SceneObservationCaps.Monsters),
        SceneFactBatch.From(clumps, SceneObservationCaps.Clumps)
    );

    public static ObservationPayload Snapshot(
        string sessionId,
        long domainRevision,
        SceneAuthorityDecision authority,
        SceneLocationFact location,
        SceneFactBatch warps,
        SceneFactBatch doors,
        SceneFactBatch objects,
        SceneFactBatch terrain,
        SceneFactBatch npcs,
        SceneFactBatch monsters,
        SceneFactBatch clumps,
        SceneFactBatch? buildingDoors = null
    )
    {
        Dictionary<string, object> facts = new(StringComparer.Ordinal);
        int observed = 0;
        bool truncated = false;

        observed += 1;
        AddFact(facts, $"loc:{location.LocationId}", location);

        truncated |= AddCategory(facts, warps, SceneObservationCaps.Warps, ref observed);
        truncated |= AddCategory(facts, doors, SceneObservationCaps.Doors, ref observed);
        truncated |= AddCategory(facts, objects, SceneObservationCaps.Objects, ref observed);
        truncated |= AddCategory(facts, terrain, SceneObservationCaps.Terrain, ref observed);
        truncated |= AddCategory(facts, npcs, SceneObservationCaps.Npcs, ref observed);
        truncated |= AddCategory(facts, monsters, SceneObservationCaps.Monsters, ref observed);
        truncated |= AddCategory(facts, clumps, SceneObservationCaps.Clumps, ref observed);
        if (buildingDoors is { } doorsBatch)
            truncated |= AddCategory(facts, doorsBatch, SceneObservationCaps.BuildingDoors, ref observed);
        if (observed > facts.Count)
            truncated = true;

        return new ObservationPayload
        {
            SessionId = sessionId,
            Product = ObservationProduct.Scene,
            Domain = ObservationDomain.SceneCurrentLocation,
            Kind = ObservationKind.Snapshot,
            DomainRevision = domainRevision,
            Provenance = authority.Provenance,
            Authoritative = authority.Authoritative,
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
        Product = ObservationProduct.Scene,
        Domain = ObservationDomain.SceneCurrentLocation,
        Kind = ObservationKind.Error,
        DomainRevision = domainRevision,
        Provenance = ObservationProvenance.Unknown,
        Authoritative = false,
        Available = false,
        Error = new ObservationErrorInfo { Reason = reason, Detail = detail },
        Truncation = null,
        Body = null,
    };

    static bool AddCategory(
        Dictionary<string, object> facts,
        SceneFactBatch batch,
        int cap,
        ref int observed
    )
    {
        observed += batch.Observed;
        IReadOnlyList<ScenePreparedFact> sent = batch.Sent;
        int take = Math.Min(sent.Count, cap);
        for (int i = 0; i < take; i++)
            AddFact(facts, sent[i].Id, sent[i].Dto);
        return batch.Observed > take;
    }

    static void AddFact(Dictionary<string, object> facts, string id, object dto)
    {
        if (facts.ContainsKey(id))
            return;
        facts[id] = JsonSerializer.SerializeToElement(dto, ProtocolJson.Options);
    }
}

/// <summary>
/// Token ack yang diambil di game thread sebelum kiriman, lalu dipakai
/// untuk menolak completion session/request lama tanpa mutasi.
/// </summary>
public readonly record struct SceneAckToken(
    string SessionId,
    long Epoch,
    long RequestId,
    long Revision
);

/// <summary>
/// Epoch observation per screen. Opaque session_id; reconnect/title
/// membuka epoch baru. Snapshot on-demand, bukan 10 Hz.
/// </summary>
public sealed class SceneObservationScheduler
{
    long epoch = 1;
    long requestId;

    public string SessionId { get; private set; } = "";

    public long DomainRevision { get; private set; }

    public bool SnapshotNeeded { get; private set; }

    public bool HasSession => SessionId.Length > 0;

    public string? LastLocationId { get; private set; }

    /// <summary>ID permintaan snapshot saat ini. Dipakai supaya warp
    /// selama kiriman tidak terhapus oleh ack yang basi.</summary>
    public long RequestId => this.requestId;

    public long Epoch => this.epoch;

    public SceneAckToken CurrentAckToken() =>
        new(SessionId, this.epoch, this.requestId, this.DomainRevision);

    public void BeginSession(string saveToken, long playerId)
    {
        SessionId = SceneIdentity.SessionId(saveToken, playerId, this.epoch);
        this.DomainRevision = 0;
        this.LastLocationId = null;
        this.RequestSnapshot();
    }

    public void OnReconnect(string saveToken, long playerId)
    {
        this.epoch++;
        BeginSession(saveToken, playerId);
    }

    public void OnReturnedToTitle()
    {
        this.epoch++;
        this.SessionId = "";
        this.DomainRevision = 0;
        this.SnapshotNeeded = false;
        this.LastLocationId = null;
        this.requestId++;
    }

    public void OnLocalWarp(string? newLocationId)
    {
        if (string.IsNullOrWhiteSpace(newLocationId))
            return;
        // Event warp adalah boundary lokasi meski token nama kebetulan sama.
        // Selalu invalidasikan request lama dan minta snapshot penuh.
        this.RequestSnapshot();
    }

    public void RequestSnapshot()
    {
        this.SnapshotNeeded = true;
        this.requestId++;
    }

    public long PeekNextRevision() => this.DomainRevision + 1;

    /// <summary>
    /// Cadangkan revision untuk snapshot/error yang akan dikirim.
    /// Dipanggil di game thread saat payload dibuat. Kiriman gagal boleh
    /// mengonsumsi revision; ack tidak mengalokasikan revision.
    /// </summary>
    public long ReserveSnapshotRevision()
    {
        this.DomainRevision++;
        return this.DomainRevision;
    }

    /// <summary>
    /// Delta memakai watermark reserve yang sama dengan snapshot. Reserve
    /// terjadi sebelum send agar warp saat send in-flight tetap membuat
    /// snapshot berikutnya R+1, bahkan jika completion delta kemudian stale.
    /// Baseline fact tetap baru di-commit sesudah ack sukses.
    /// </summary>
    public long ReserveDeltaRevision()
    {
        if (!this.HasSession || this.SnapshotNeeded)
            throw new InvalidOperationException(
                "Delta hanya boleh di-reserve sesudah baseline snapshot ack."
            );
        this.DomainRevision++;
        return this.DomainRevision;
    }

    /// <summary>
    /// Ack memakai session/epoch saat ini plus requestId yang diberikan.
    /// Untuk jalur produksi, pakai <see cref="TryMarkSnapshotSent"/> dengan
    /// token yang di-capture sebelum kirim.
    /// </summary>
    public bool MarkSnapshotSent(string? locationId, long capturedRequestId) =>
        TryMarkSnapshotSent(
            new SceneAckToken(
                SessionId,
                this.epoch,
                capturedRequestId,
                this.DomainRevision
            ),
            locationId
        );

    /// <summary>
    /// Hanya mutasi jika token (session, epoch, request) masih cocok.
    /// Token session lama setelah title/session baru = nol mutasi.
    /// Ack tidak memajukan <see cref="DomainRevision"/> — revision sudah
    /// di-reserve saat payload dibuat.
    /// </summary>
    public bool TryMarkSnapshotSent(SceneAckToken token, string? locationId)
    {
        if (!IsCurrentAck(token))
            return false;

        this.SnapshotNeeded = false;
        if (!string.IsNullOrWhiteSpace(locationId))
            this.LastLocationId = locationId;
        return true;
    }

    /// <summary>
    /// Ack delta tidak mengubah watermark revision; revision sudah di-reserve.
    /// Request/session/revision yang basi ditolak tanpa mutasi.
    /// </summary>
    public bool TryMarkDeltaSent(SceneAckToken token) =>
        !this.SnapshotNeeded && IsCurrentAck(token);

    bool IsCurrentAck(SceneAckToken token) =>
        this.HasSession
        && string.Equals(token.SessionId, SessionId, StringComparison.Ordinal)
        && token.Epoch == this.epoch
        && token.RequestId == this.requestId
        && token.Revision == this.DomainRevision;
}

/// <summary>
/// Urutan traversal OBS-1 untuk kategori ber-tile: terdekat dari farmer dulu
/// (Chebyshev, lalu Manhattan, lalu x, y) supaya cap kategori yang ketat
/// (<see cref="SceneObservationCaps"/>) memuat tetangga farmer, bukan 40 entitas
/// pertama menurut enumerasi peta. Live 2026-10-06 (M1 attempt 10f): Farm dengan
/// 2954 fakta mengirim 109 fakta yang tidak satu pun berada dalam radius 4 petak,
/// sehingga katalog approach kosong setelah pohon yang bersebelahan habis.
/// Observed/Sent tidak berubah; hanya urutan tawaran ke <see cref="SceneFactBag"/>.
/// </summary>
public static class SceneTraversalOrder
{
    public static List<(int X, int Y, T Value)> Nearest<T>(
        IEnumerable<(int X, int Y, T Value)> items,
        int originX,
        int originY,
        Func<T, bool>? priority = null
    )
    {
        List<(int X, int Y, T Value)> ordered = new(items);
        ordered.Sort((a, b) =>
        {
            int pa = priority?.Invoke(a.Value) == true ? 0 : 1;
            int pb = priority?.Invoke(b.Value) == true ? 0 : 1;
            if (pa != pb)
                return pa.CompareTo(pb);
            int da = Math.Max(Math.Abs(a.X - originX), Math.Abs(a.Y - originY));
            int db = Math.Max(Math.Abs(b.X - originX), Math.Abs(b.Y - originY));
            if (da != db)
                return da.CompareTo(db);
            int ma = Math.Abs(a.X - originX) + Math.Abs(a.Y - originY);
            int mb = Math.Abs(b.X - originX) + Math.Abs(b.Y - originY);
            if (ma != mb)
                return ma.CompareTo(mb);
            if (a.X != b.X)
                return a.X.CompareTo(b.X);
            return a.Y.CompareTo(b.Y);
        });
        return ordered;
    }
}
