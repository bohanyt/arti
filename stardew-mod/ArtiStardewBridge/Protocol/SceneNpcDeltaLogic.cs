using System.Runtime.CompilerServices;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Identity per-instance untuk NPC non-Monster.
///
/// Stardew menyimpan <c>GameLocation.characters</c> sebagai
/// <c>NetCollection&lt;NPC&gt;</c>; GUID entry koleksi itu unik walau beberapa
/// instance memiliki <c>NPC.Name</c> sama. SMAPI baru menaikkan REMOVE setelah
/// entry keluar, jadi GUID harus dicache saat snapshot/ADD masih bisa
/// memanggil <c>GuidOf(npc)</c>.
/// </summary>
internal sealed class SceneNpcIdentityRegistry
{
    sealed class Identity
    {
        public Identity(string internalName, string discriminator)
        {
            this.InternalName = internalName;
            this.Discriminator = discriminator;
        }

        public string InternalName { get; }

        public string Discriminator { get; }
    }

    readonly ConditionalWeakTable<object, Identity> identities = new();

    public string GetOrCreateFactId(
        object instance,
        Guid nativeMembershipId,
        string locationId,
        string internalName
    )
    {
        ArgumentNullException.ThrowIfNull(instance);
        if (nativeMembershipId == Guid.Empty)
            throw new ArgumentException(
                "NPC current-location membership GUID tidak boleh kosong.",
                nameof(nativeMembershipId)
            );

        Identity identity = this.identities.GetValue(
            instance,
            _ => new Identity(internalName ?? "", $"net-{nativeMembershipId:N}")
        );
        return SceneIdentity.NpcFactId(
            locationId,
            identity.InternalName,
            identity.Discriminator
        );
    }

    public bool TryGetFactId(
        object instance,
        string locationId,
        out string factId
    )
    {
        ArgumentNullException.ThrowIfNull(instance);
        if (this.identities.TryGetValue(instance, out Identity? identity))
        {
            factId = SceneIdentity.NpcFactId(
                locationId,
                identity.InternalName,
                identity.Discriminator
            );
            return true;
        }

        factId = "";
        return false;
    }
}

/// <summary>
/// Terapkan state membership final yang sudah dibaca sinkron dari Stardew.
/// Tidak menerima urutan Added/Removed sebagai chronology.
/// </summary>
internal static class SceneNpcDeltaLogic
{
    /// <summary>
    /// SMAPI <c>Added</c> dan <c>Removed</c> adalah dua diff set independen.
    /// Gabungkan instance terdampak dulu; jangan proses array sebagai urutan.
    /// Monster/null dibuang di sini supaya tes ProtocolTests bisa mengeksekusi
    /// union yang sama dengan handler, bukan hanya <see cref="ApplyFinalMembership"/>.
    /// </summary>
    public static HashSet<TNpc> CollectAffected<TNpc>(
        IEnumerable<TNpc?> added,
        IEnumerable<TNpc?> removed,
        Func<TNpc, bool> exclude
    ) where TNpc : class
    {
        ArgumentNullException.ThrowIfNull(added);
        ArgumentNullException.ThrowIfNull(removed);
        ArgumentNullException.ThrowIfNull(exclude);
        var affected = new HashSet<TNpc>(ReferenceEqualityComparer.Instance);
        CollectOne(affected, added, exclude);
        CollectOne(affected, removed, exclude);
        return affected;
    }

    static void CollectOne<TNpc>(
        HashSet<TNpc> affected,
        IEnumerable<TNpc?> source,
        Func<TNpc, bool> exclude
    ) where TNpc : class
    {
        foreach (TNpc? npc in source)
        {
            if (npc is null || exclude(npc))
                continue;
            affected.Add(npc);
        }
    }

    public static SceneDeltaAccumulateResult ApplyFinalMembership(
        SceneDeltaAccumulator accumulator,
        ScenePreparedFact? finalFact,
        string? knownFactId
    )
    {
        ArgumentNullException.ThrowIfNull(accumulator);
        if (finalFact.HasValue)
            return accumulator.Upsert(finalFact.Value);
        if (string.IsNullOrEmpty(knownFactId))
            return SceneDeltaAccumulateResult.IgnoredWithoutBaseline;
        return accumulator.Remove(knownFactId);
    }
}
