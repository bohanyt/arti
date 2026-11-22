using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge.Observation;

/// <summary>
/// Adapter G3-1 per screen. HARUS dipanggil dari game thread lewat ModEntry.
///
/// Bukan receiver/store/socket/motor kedua. Accumulator bounded di
/// <see cref="UiTextAccumulator"/>. Tidak membaca Game1 sendiri: ModEntry
/// memasok <see cref="UiTextTickSample"/> dari CaptureMenu + tracker G3-0
/// yang sama pada tick yang sama.
///
/// Project ProtocolTests hanya mengompilasi Protocol/**/*.cs — PASS C#
/// BUKAN bukti berkas ini kompil terhadap Stardew.
/// </summary>
public sealed class UiTextSnapshotCapture
{
    readonly UiTextAccumulator accumulator = new();

    public UiTextAccumulator Accumulator => this.accumulator;

    public bool HasSession => this.accumulator.HasSession;

    public bool Dirty => this.accumulator.Dirty;

    public bool Unsupported => this.accumulator.Unsupported;

    public string SessionId => this.accumulator.SessionId;

    public long DomainRevision => this.accumulator.DomainRevision;

    public void BindSession(string sessionId) => this.accumulator.BindSession(sessionId);

    public void OnReturnedToTitle() => this.accumulator.OnReturnedToTitle();

    public void AdvanceUnsupported() => this.accumulator.AdvanceUnsupported();

    public void Advance(UiTextTickSample sample) => this.accumulator.Advance(sample);

    public void AdvanceBatch(IReadOnlyList<UiTextTickSample> samples) => this.accumulator.AdvanceBatch(samples);

    public ObservationPayload? ConsumeDirtyPayload() =>
        this.accumulator.ConsumeDirtyPayload();

    public void AckSent() => this.accumulator.AckSent();

    public void NackSent() => this.accumulator.NackSent();
}
