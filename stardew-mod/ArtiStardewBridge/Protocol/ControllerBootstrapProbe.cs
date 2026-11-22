using System.Globalization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Probe TEST-ONLY untuk P5-R2 aksen (Issue #62): SATU injeksi tombol D-pad
/// lewat seam SMAPI yang sudah ada, lalu rekam keadaan sebelum/sesudah.
///
/// Kelas ini MURNI pembukuan giliran; dia tidak menyentuh SMAPI/Stardew sama
/// sekali (folder Protocol wajib bebas SMAPI supaya ProtocolTests bisa
/// mengompilnya tanpa instalasi Stardew). Pemanggil di ModEntry yang memetakan
/// arah menjadi SButton D-pad dan memanggil Helper.Input.Press.
///
/// Pelajaran mahal yang dikunci di sini:
///
/// 1. Injeksi WAJIB terjadi di UpdateTicking, bukan UpdateTicked. Bukti IL
///    P5-R1 bagian 6.5: SCore memanggil Input.ApplyOverrides() SESUDAH event
///    UpdateTicking dan SEBELUM runUpdate, jadi override yang dikirim saat
///    UpdateTicking terlihat oleh Game1.Update TICK YANG SAMA, lalu masuk ke
///    CheckGamepadMode.
/// 2. Blok snap awal (CheckGamepadMode blok f) edge-triggered pada
///    gamepadControls false-ke-true. Sekali nyala, tidak ada snap kedua dari
///    jalur itu. Karena itu probe ini SEKALI TEMBAK, bukan loop; mengulang
///    dari state yang sudah nyala bukan pengulangan kondisi yang sama.
/// 3. Rilis TIDAK gratis. Bukti live Issue #62: satu Helper.Input.Press
///    TIDAK dijamin melepas sendiri pada tick berikutnya.
///    Run 1 (ARTI inactive, gameIsActive=False): D:Pressed tetap latched
///    sepanjang 60 tick pengamatan.
///    Run 2 (ARTI active, gameIsActive=True): input lepas pada offset 5.
///    Korelasi dengan fokus/aktivitas jendela persis; sebabnya hanya
///    HYPOTHESIS (bukan sebab terukur). Motor production masa depan wajib
///    memiliki semantik rilis eksplisit; probe ini tidak
///    mengimplementasikannya.
///
/// Bukan motor production. Tidak ada TCP command, ViGEm, Harmony, atau loop.
/// </summary>
public sealed class ControllerBootstrapProbe
{
    public const string CommandName = "arti_probe_pad";
    public const int MinDelayTicks = 0;

    /// <summary>30 detik pada 60 tick/detik: cukup untuk menata jendela.</summary>
    public const int MaxDelayTicks = 1800;

    /// <summary>
    /// Offset tick (relatif tick injeksi) yang dipotret sesudah Press.
    /// Offset 0 = UpdateTicked pada tick injeksi itu sendiri: bukti tombol
    /// masuk ke input state SMAPI pada tick yang sama.
    /// Offset 1 ke atas merekam apakah override masih latched atau sudah
    /// netral, plus snap yang mengendap. Mereka TIDAK membuktikan rilis
    /// otomatis: bukti live Issue #62, run 1 tetap D:Pressed sampai offset
    /// 60; run 2 netral pada offset 5. Jangan anggap satu Press self-release.
    /// </summary>
    public static readonly IReadOnlyList<int> ObserveOffsets =
        new[] { 0, 1, 2, 3, 4, 5, 10, 20, 30, 60 };

    private static readonly string[] AllowedDirections = { "down", "right", "up", "left" };

    private ControllerProbePhase phase = ControllerProbePhase.Idle;
    private int remainingDelayTicks;
    private int offsetSincePress = -1;
    private string? direction;

    public ControllerProbePhase Phase => this.phase;

    public bool IsActive => this.phase != ControllerProbePhase.Idle;

    public string? ActiveDirection => this.direction;

    public int RemainingDelayTicks => this.remainingDelayTicks;

    public static bool TryNormalizeDirection(string? raw, out string normalized)
    {
        normalized = (raw ?? "").Trim().ToLowerInvariant();

        // Alias controller supaya "dpaddown" dan "down" sama-sama sah.
        if (normalized.StartsWith("dpad", StringComparison.Ordinal))
            normalized = normalized.Substring(4);

        if (Array.IndexOf(AllowedDirections, normalized) >= 0)
            return true;

        normalized = "";
        return false;
    }

    public static bool TryParseCommand(
        string[]? args,
        out string direction,
        out int delayTicks,
        out ControllerProbeStatus error
    )
    {
        direction = "";
        delayTicks = 0;
        if (args is null || args.Length is < 1 or > 2
            || !TryNormalizeDirection(args[0], out direction))
        {
            error = ControllerProbeStatus.RejectedInvalidDirection;
            direction = "";
            return false;
        }

        if (args.Length == 2
            && (!int.TryParse(
                    args[1], NumberStyles.None, CultureInfo.InvariantCulture, out delayTicks)
                || delayTicks < MinDelayTicks
                || delayTicks > MaxDelayTicks))
        {
            error = ControllerProbeStatus.RejectedInvalidDelay;
            direction = "";
            delayTicks = 0;
            return false;
        }

        error = ControllerProbeStatus.Started;
        return true;
    }

    /// <summary>
    /// Gerbang keras. <paramref name="enabled"/> datang dari config yang
    /// bawaannya MATI: probe ini tidak boleh bisa jalan tanpa Bohan menyalakan
    /// config secara sadar.
    /// </summary>
    public ControllerProbeStatus TryArm(
        string direction,
        int delayTicks,
        bool enabled,
        ProbeWorldContext context,
        bool menuOpen
    )
    {
        if (!TryNormalizeDirection(direction, out string key))
            return ControllerProbeStatus.RejectedInvalidDirection;
        if (delayTicks < MinDelayTicks || delayTicks > MaxDelayTicks)
            return ControllerProbeStatus.RejectedInvalidDelay;
        if (!enabled)
            return ControllerProbeStatus.RejectedDisabled;
        if (!context.IsWorldReady)
            return ControllerProbeStatus.RejectedWorldNotReady;
        if (!context.IsMultiplayer)
            return ControllerProbeStatus.RejectedNotMultiplayer;
        if (context.IsMainPlayer)
            return ControllerProbeStatus.RejectedHost;
        if (context.IsSplitScreen)
            return ControllerProbeStatus.RejectedSplitScreen;
        if (!menuOpen)
            return ControllerProbeStatus.RejectedNoMenu;
        if (this.IsActive)
            return ControllerProbeStatus.RejectedAlreadyActive;

        this.direction = key;
        this.remainingDelayTicks = delayTicks;
        this.phase = ControllerProbePhase.Waiting;
        this.offsetSincePress = -1;
        return ControllerProbeStatus.Started;
    }

    /// <summary>
    /// Dipanggil sekali per event UpdateTicking. Kind Fire berarti pemanggil
    /// harus memotret BEFORE lalu memanggil Press SEKALI pada tick ini.
    /// </summary>
    public ControllerProbeTickingResult ConsumeTicking()
    {
        if (this.phase != ControllerProbePhase.Waiting)
        {
            return new ControllerProbeTickingResult(
                ControllerProbeTickingKind.Idle, 0, null);
        }

        if (this.remainingDelayTicks > 0)
        {
            this.remainingDelayTicks--;
            if (this.remainingDelayTicks > 0)
            {
                return new ControllerProbeTickingResult(
                    ControllerProbeTickingKind.Waiting,
                    this.remainingDelayTicks,
                    this.direction
                );
            }
        }

        this.phase = ControllerProbePhase.Observing;
        this.offsetSincePress = 0;
        return new ControllerProbeTickingResult(
            ControllerProbeTickingKind.Fire, 0, this.direction);
    }

    /// <summary>
    /// Dipanggil sekali per event UpdateTicked. Mengembalikan offset yang wajib
    /// dipotret, atau -1 kalau tick ini tidak termasuk jendela pengamatan.
    /// Probe mati sendiri sesudah offset terakhir; tidak ada loop.
    /// </summary>
    public int ConsumeTicked()
    {
        if (this.phase != ControllerProbePhase.Observing)
            return -1;

        int offset = this.offsetSincePress;
        this.offsetSincePress++;

        int last = ObserveOffsets[ObserveOffsets.Count - 1];
        if (offset >= last)
        {
            this.phase = ControllerProbePhase.Idle;
            this.direction = null;
            this.offsetSincePress = -1;
        }

        return ObserveOffsets.Contains(offset) ? offset : -1;
    }

    public void Cancel()
    {
        this.phase = ControllerProbePhase.Idle;
        this.remainingDelayTicks = 0;
        this.offsetSincePress = -1;
        this.direction = null;
    }
}

public enum ControllerProbePhase
{
    Idle,
    Waiting,
    Observing,
}

public enum ControllerProbeTickingKind
{
    Idle,
    Waiting,
    Fire,
}

public readonly record struct ControllerProbeTickingResult(
    ControllerProbeTickingKind Kind,
    int RemainingDelayTicks,
    string? Direction
);

public enum ControllerProbeStatus
{
    Started,
    RejectedDisabled,
    RejectedInvalidDirection,
    RejectedInvalidDelay,
    RejectedWorldNotReady,
    RejectedNotMultiplayer,
    RejectedHost,
    RejectedSplitScreen,
    RejectedNoMenu,
    RejectedAlreadyActive,
}
