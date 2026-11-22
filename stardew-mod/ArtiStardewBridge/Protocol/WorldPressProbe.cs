using System.Globalization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Probe TEST-ONLY untuk P2: tekan binding gerak yang sudah dikonfigurasi
/// lewat <c>Helper.Input.Press</c> selama N tick, lalu berhenti sendiri.
///
/// Bukan motor production. Tidak ada TCP command, ViGEm, MotorEnabled,
/// atau LLM. Penjadwalan P3 ada di <see cref="WorldPressDelayedProbe"/>;
/// kelas ini hanya menekan binding selama N tick. Pemanggil SMAPI
/// yang memasang <see cref="Action"/> Press; tidak menyentuh Position.
/// </summary>
public sealed class WorldPressProbe
{
    public const string CommandName = "arti_probe_press";
    public const int MinTickCount = 1;
    public const int MaxTickCount = 60;

    private int remainingTicks;
    private string? direction;
    private Action? press;
    private int runId;
    private int nextRunId = 1;
    private int pressCount;

    public bool IsActive => this.remainingTicks > 0;

    public int RemainingTicks => this.remainingTicks;

    public string? ActiveDirection => this.direction;

    /// <summary>
    /// Run monotonic yang dipegang probe ini. Naik hanya saat
    /// <see cref="TryArm"/> diterima. Kotak masuk mengikat permintaan Python
    /// ke nilai ini supaya run konsol <c>arti_probe_press</c> tidak pernah
    /// menutup permintaan inbox.
    /// </summary>
    public int RunId => this.runId;

    /// <summary>
    /// Berapa kali <c>Press</c> sudah dipanggil di run aktif/terakhir.
    /// Tidak direset saat run selesai sendiri; direset saat arm berikutnya.
    /// </summary>
    public int PressCount => this.pressCount;

    public static bool TryParseCommand(
        string[]? args,
        out string direction,
        out int ticks,
        out ProbePressStatus error
    )
    {
        direction = "";
        ticks = 0;
        if (args is null || args.Length == 0 || !TryNormalizeDirection(args[0], out direction))
        {
            error = ProbePressStatus.RejectedInvalidDirection;
            direction = "";
            return false;
        }

        if (args.Length != 2
            || !int.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out ticks)
            || ticks < MinTickCount
            || ticks > MaxTickCount)
        {
            error = ProbePressStatus.RejectedInvalidCount;
            ticks = 0;
            return false;
        }

        error = ProbePressStatus.Started;
        return true;
    }

    public static bool TryNormalizeDirection(string? raw, out string direction)
    {
        direction = (raw ?? "").Trim().ToLowerInvariant();
        return direction is "left" or "right" or "up" or "down";
    }

    /// <summary>
    /// Ambil binding pertama yang dikonfigurasi game, bukan WASD keras.
    /// List kosong / arah tidak dikenal = gagal.
    /// </summary>
    public static bool TryMapFirstBinding<TButton>(
        string direction,
        IReadOnlyDictionary<string, IReadOnlyList<TButton>> bindings,
        out TButton button
    )
    {
        button = default!;
        if (!TryNormalizeDirection(direction, out string key))
            return false;
        if (!bindings.TryGetValue(key, out IReadOnlyList<TButton>? list)
            || list is null
            || list.Count == 0)
            return false;
        button = list[0];
        return true;
    }

    public ProbePressStatus TryArm(
        string direction,
        int ticks,
        ProbeWorldContext context,
        Action pressEachTick
    )
    {
        if (pressEachTick is null)
            throw new ArgumentNullException(nameof(pressEachTick));
        if (!TryNormalizeDirection(direction, out string key)
            || ticks < MinTickCount
            || ticks > MaxTickCount)
        {
            return !TryNormalizeDirection(direction, out _)
                ? ProbePressStatus.RejectedInvalidDirection
                : ProbePressStatus.RejectedInvalidCount;
        }

        if (!context.IsWorldReady)
            return ProbePressStatus.RejectedWorldNotReady;
        if (!context.IsMultiplayer)
            return ProbePressStatus.RejectedNotMultiplayer;
        if (context.IsMainPlayer)
            return ProbePressStatus.RejectedHost;
        if (context.IsSplitScreen)
            return ProbePressStatus.RejectedSplitScreen;
        if (this.IsActive)
            return ProbePressStatus.RejectedAlreadyActive;

        this.direction = key;
        this.remainingTicks = ticks;
        this.press = pressEachTick;
        this.pressCount = 0;
        this.runId = this.nextRunId++;
        return ProbePressStatus.Started;
    }

    /// <summary>
    /// Satu update tick game. True hanya jika Press dipanggil tick ini.
    /// Setelah N pemanggilan sukses, probe mati sendiri.
    /// </summary>
    public bool TryConsumeTick()
    {
        if (this.remainingTicks <= 0 || this.press is null)
            return false;

        Action press = this.press;
        this.remainingTicks--;
        this.pressCount++;
        if (this.remainingTicks == 0)
        {
            this.press = null;
            this.direction = null;
        }

        press();
        return true;
    }

    public void Cancel()
    {
        this.remainingTicks = 0;
        this.direction = null;
        this.press = null;
    }
}

public readonly record struct ProbeWorldContext(
    bool IsWorldReady,
    bool IsMultiplayer,
    bool IsMainPlayer,
    bool IsSplitScreen
);

public enum ProbePressStatus
{
    Started,
    RejectedWorldNotReady,
    RejectedNotMultiplayer,
    RejectedHost,
    RejectedSplitScreen,
    RejectedInvalidDirection,
    RejectedInvalidCount,
    RejectedAlreadyActive,
    RejectedUnmappedBinding,
    RejectedInvalidDelay,
    RejectedDelayedAlreadyPending,
    RejectedImmediateProbeActive,
}
