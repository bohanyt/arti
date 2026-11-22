using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge.Observation;

/// <summary>
/// Adapter G3-0 per screen. HARUS dipanggil dari game thread lewat ModEntry.
///
/// Bukan receiver/store/socket/motor kedua. Accumulator bounded latest-wins
/// di <see cref="UiContextAccumulator"/>. Facts memakai vocabulary G2 yang
/// sama. Tidak membaca Game1 sendiri: ModEntry yang memasok snapshot G2.
///
/// Project ProtocolTests hanya mengompilasi Protocol/**/*.cs — PASS C#
/// BUKAN bukti berkas ini kompil terhadap Stardew.
/// </summary>
public sealed class UiContextSnapshotCapture
{
    readonly UiContextAccumulator accumulator = new();

    public UiContextAccumulator Accumulator => this.accumulator;

    public bool HasSession => this.accumulator.HasSession;

    public bool Dirty => this.accumulator.Dirty;

    public bool Unsupported => this.accumulator.Unsupported;

    public string SessionId => this.accumulator.SessionId;

    public long DomainRevision => this.accumulator.DomainRevision;

    public string? InflightFingerprint => this.accumulator.InflightFingerprint;

    public void BindSession(string sessionId) => this.accumulator.BindSession(sessionId);

    public void OnReturnedToTitle() => this.accumulator.OnReturnedToTitle();

    public void AdvanceUnsupported() => this.accumulator.AdvanceUnsupported();

    public void Advance(IReadOnlyDictionary<string, object> facts) =>
        this.accumulator.Advance(facts);

    public ObservationPayload? ConsumeDirtySnapshot() =>
        this.accumulator.ConsumeDirtySnapshot();

    public ObservationPayload? ConsumeRefreshSnapshot(long nowUnixMs, int intervalMs) =>
        this.accumulator.ConsumeRefreshSnapshot(nowUnixMs, intervalMs);

    public void AckSent(long? nowUnixMs = null) => this.accumulator.AckSent(nowUnixMs);

    public void NackSent() => this.accumulator.NackSent();
}
