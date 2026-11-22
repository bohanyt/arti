using System.Collections.ObjectModel;

namespace ArtiStardewBridge.Protocol;

public enum SceneDeltaAccumulateResult
{
    IgnoredWithoutBaseline,
    Coalesced,
    OverflowRequiresSnapshot,
}

/// <summary>
/// Accumulator bersama untuk event membership object OBS-2B1 dan terrain
/// OBS-2B2a. Semua kind berbagi revision stream dan cap patch global.
///
/// Baseline dan DTO disimpan sebagai JsonElement ternormalisasi yang imutabel.
/// Patch pending selalu dibatasi <see cref="SceneDeltaCaps.MaxPatchOps"/>.
/// Snapshot yang masih in-flight boleh menahan event, tetapi delta tidak boleh
/// dibangun sebelum snapshot itu berhasil di-ack.
/// </summary>
public sealed class SceneDeltaAccumulator
{
    static readonly IReadOnlyDictionary<string, object> EmptyFacts =
        new ReadOnlyDictionary<string, object>(
            new SortedDictionary<string, object>(StringComparer.Ordinal)
        );

    IReadOnlyDictionary<string, object> baselineFacts = EmptyFacts;
    IReadOnlyDictionary<string, object>? stagedFacts;
    string baselineSessionId = "";
    string stagedSessionId = "";
    long stagedRevision;
    readonly SortedDictionary<string, object> pendingUpserts =
        new(StringComparer.Ordinal);
    readonly SortedSet<string> pendingRemoves = new(StringComparer.Ordinal);

    public bool HasAcknowledgedBaseline => this.baselineSessionId.Length > 0;

    public bool HasStagedBaseline => this.stagedFacts is not null;

    public bool RequiresSnapshot { get; private set; }

    public long BaselineRevision { get; private set; }

    public int PendingOperationCount =>
        this.pendingUpserts.Count + this.pendingRemoves.Count;

    public IReadOnlyDictionary<string, object> BaselineFacts => this.baselineFacts;

    /// <summary>The baseline later events coalesce against: the staged (in-flight) snapshot, else the acked one.</summary>
    public bool TryGetWorkingFacts(out IReadOnlyDictionary<string, object> working) =>
        TryGetWorkingBaseline(out working);

    public bool StageSnapshot(ObservationPayload snapshot)
    {
        if (
            snapshot.Kind != ObservationKind.Snapshot
            || !snapshot.Available
            || string.IsNullOrEmpty(snapshot.SessionId)
            || snapshot.Body?.Facts is null
        )
        {
            return false;
        }

        this.stagedFacts = Freeze(snapshot.Body.Facts);
        this.stagedSessionId = snapshot.SessionId;
        this.stagedRevision = snapshot.DomainRevision;
        this.pendingUpserts.Clear();
        this.pendingRemoves.Clear();
        this.RequiresSnapshot = false;
        return true;
    }

    public bool TryCommitStagedSnapshot(ObservationPayload sentSnapshot)
    {
        if (
            this.stagedFacts is null
            || sentSnapshot.Kind != ObservationKind.Snapshot
            || sentSnapshot.Body?.Facts is null
            || !string.Equals(
                sentSnapshot.SessionId,
                this.stagedSessionId,
                StringComparison.Ordinal
            )
            || sentSnapshot.DomainRevision != this.stagedRevision
            || !SceneFactCanonical.Equal(sentSnapshot.Body.Facts, this.stagedFacts)
        )
        {
            return false;
        }

        this.baselineFacts = this.stagedFacts;
        this.baselineSessionId = this.stagedSessionId;
        this.BaselineRevision = this.stagedRevision;
        this.stagedFacts = null;
        this.stagedSessionId = "";
        this.stagedRevision = 0;
        return true;
    }

    public SceneDeltaAccumulateResult Upsert(ScenePreparedFact fact)
    {
        if (!TryGetWorkingBaseline(out IReadOnlyDictionary<string, object>? working))
            return SceneDeltaAccumulateResult.IgnoredWithoutBaseline;
        if (this.RequiresSnapshot)
            return SceneDeltaAccumulateResult.OverflowRequiresSnapshot;

        object normalized = SceneFactCanonical.Normalize(fact.Dto);
        if (
            working.TryGetValue(fact.Id, out object? original)
            && SceneFactCanonical.Equal(original, normalized)
        )
        {
            this.pendingUpserts.Remove(fact.Id);
            this.pendingRemoves.Remove(fact.Id);
            return SceneDeltaAccumulateResult.Coalesced;
        }

        this.pendingRemoves.Remove(fact.Id);
        this.pendingUpserts[fact.Id] = normalized;
        return CheckBound();
    }

    public SceneDeltaAccumulateResult Remove(string factId)
    {
        if (!TryGetWorkingBaseline(out IReadOnlyDictionary<string, object>? working))
            return SceneDeltaAccumulateResult.IgnoredWithoutBaseline;
        if (this.RequiresSnapshot)
            return SceneDeltaAccumulateResult.OverflowRequiresSnapshot;

        this.pendingUpserts.Remove(factId);
        if (working.ContainsKey(factId))
            this.pendingRemoves.Add(factId);
        else
            this.pendingRemoves.Remove(factId);
        return CheckBound();
    }

    public bool TryBuildDelta(
        string sessionId,
        long previousRevision,
        SceneAuthorityDecision authority,
        out ObservationPayload? delta
    )
    {
        delta = null;
        if (
            !this.HasAcknowledgedBaseline
            || this.HasStagedBaseline
            || this.RequiresSnapshot
            || this.PendingOperationCount == 0
            || !string.Equals(sessionId, this.baselineSessionId, StringComparison.Ordinal)
            || previousRevision != this.BaselineRevision
        )
        {
            return false;
        }

        IReadOnlyDictionary<string, object> after = SceneDeltaDiff.Apply(
            this.baselineFacts,
            this.pendingUpserts,
            this.pendingRemoves.ToArray()
        );
        ObservationPayload candidate = SceneDeltaBuilder.Delta(
            sessionId,
            previousRevision,
            authority,
            this.baselineFacts,
            after,
            SceneDeltaCaps.MaxPatchOps
        );
        if (candidate.Truncation?.Truncated == true)
        {
            MarkOverflow();
            return false;
        }

        delta = candidate;
        return true;
    }

    /// <summary>
    /// Commit tepat patch yang benar-benar dikirim. Pending tidak dibuang saat
    /// send dimulai, sehingga event baru selama in-flight dapat direbase dan
    /// tetap menjadi patch berikutnya.
    /// </summary>
    public bool TryCommitDelta(ObservationPayload sentDelta)
    {
        if (
            !this.HasAcknowledgedBaseline
            || sentDelta.Kind != ObservationKind.Delta
            || sentDelta.Truncation?.Truncated == true
            || sentDelta.Body?.Upserts is null
            || sentDelta.Body.Removes is null
            || !string.Equals(
                sentDelta.SessionId,
                this.baselineSessionId,
                StringComparison.Ordinal
            )
            || sentDelta.DomainRevision != this.BaselineRevision + 1
        )
        {
            return false;
        }

        IReadOnlyDictionary<string, object> desired = SceneDeltaDiff.Apply(
            this.baselineFacts,
            this.pendingUpserts,
            this.pendingRemoves.ToArray()
        );
        SceneDeltaPatch exactSent = new()
        {
            Upserts = sentDelta.Body.Upserts,
            Removes = sentDelta.Body.Removes,
            ObservedCount =
                sentDelta.Body.Upserts.Count + sentDelta.Body.Removes.Count,
            SentCount =
                sentDelta.Body.Upserts.Count + sentDelta.Body.Removes.Count,
            Truncated = false,
        };
        IReadOnlyDictionary<string, object> committed = Freeze(
            SceneDeltaDiff.Apply(this.baselineFacts, exactSent)
        );

        this.baselineFacts = committed;
        this.BaselineRevision = sentDelta.DomainRevision;
        RebasePending(desired);
        return true;
    }

    public void Invalidate()
    {
        this.baselineFacts = EmptyFacts;
        this.baselineSessionId = "";
        this.BaselineRevision = 0;
        this.stagedFacts = null;
        this.stagedSessionId = "";
        this.stagedRevision = 0;
        this.pendingUpserts.Clear();
        this.pendingRemoves.Clear();
        this.RequiresSnapshot = false;
    }

    SceneDeltaAccumulateResult CheckBound()
    {
        if (this.PendingOperationCount <= SceneDeltaCaps.MaxPatchOps)
            return SceneDeltaAccumulateResult.Coalesced;
        MarkOverflow();
        return SceneDeltaAccumulateResult.OverflowRequiresSnapshot;
    }

    void MarkOverflow()
    {
        this.pendingUpserts.Clear();
        this.pendingRemoves.Clear();
        this.RequiresSnapshot = true;
    }

    bool TryGetWorkingBaseline(
        out IReadOnlyDictionary<string, object> working
    )
    {
        if (this.stagedFacts is not null)
        {
            working = this.stagedFacts;
            return true;
        }
        if (this.HasAcknowledgedBaseline)
        {
            working = this.baselineFacts;
            return true;
        }
        working = EmptyFacts;
        return false;
    }

    void RebasePending(IReadOnlyDictionary<string, object> desired)
    {
        SceneDeltaPatch remaining = SceneDeltaDiff.Compute(
            this.baselineFacts,
            desired,
            SceneDeltaCaps.MaxPatchOps
        );
        this.pendingUpserts.Clear();
        this.pendingRemoves.Clear();
        if (remaining.Truncated)
        {
            this.RequiresSnapshot = true;
            return;
        }

        foreach (KeyValuePair<string, object> pair in remaining.Upserts)
            this.pendingUpserts[pair.Key] = SceneFactCanonical.Normalize(pair.Value);
        foreach (string id in remaining.Removes)
            this.pendingRemoves.Add(id);
        this.RequiresSnapshot = false;
    }

    static IReadOnlyDictionary<string, object> Freeze(
        IReadOnlyDictionary<string, object> source
    )
    {
        SortedDictionary<string, object> normalized =
            new(StringComparer.Ordinal);
        foreach (KeyValuePair<string, object> pair in source)
        {
            if (!string.IsNullOrEmpty(pair.Key))
                normalized[pair.Key] = SceneFactCanonical.Normalize(pair.Value);
        }
        return new ReadOnlyDictionary<string, object>(normalized);
    }
}
