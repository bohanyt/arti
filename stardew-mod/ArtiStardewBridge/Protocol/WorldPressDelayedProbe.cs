using System.Globalization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Probe TEST-ONLY untuk P3: jadwalkan <see cref="WorldPressProbe"/> setelah
/// N update tick, supaya operator sempat memindahkan fokus ke jendela HOST.
///
/// Bukan motor production, bukan TCP, bukan ViGEm, bukan XInput, bukan
/// manipulasi fokus Windows. Tidak menyentuh Position. Binding gerak
/// diselesaikan pemanggil saat trigger, lalu memakai jalur P2 yang sama.
/// </summary>
public sealed class WorldPressDelayedProbe
{
    public const string CommandName = "arti_probe_after";
    public const int MinDelayTicks = 1;
    public const int MaxDelayTicks = 3600;

    private int remainingDelayTicks;
    private string? direction;
    private int movementTicks;

    public bool IsPending => this.remainingDelayTicks > 0;

    public int RemainingDelayTicks => this.remainingDelayTicks;

    public string? PendingDirection => this.direction;

    public int PendingMovementTicks => this.movementTicks;

    public static bool TryParseCommand(
        string[]? args,
        out int delayTicks,
        out string direction,
        out int pressTicks,
        out ProbePressStatus error
    )
    {
        delayTicks = 0;
        direction = "";
        pressTicks = 0;
        if (args is null
            || args.Length == 0
            || !int.TryParse(args[0], NumberStyles.None, CultureInfo.InvariantCulture, out delayTicks)
            || delayTicks < MinDelayTicks
            || delayTicks > MaxDelayTicks)
        {
            error = ProbePressStatus.RejectedInvalidDelay;
            delayTicks = 0;
            return false;
        }

        if (args.Length < 2 || !WorldPressProbe.TryNormalizeDirection(args[1], out direction))
        {
            error = ProbePressStatus.RejectedInvalidDirection;
            direction = "";
            return false;
        }

        if (args.Length != 3
            || !int.TryParse(args[2], NumberStyles.None, CultureInfo.InvariantCulture, out pressTicks)
            || pressTicks < WorldPressProbe.MinTickCount
            || pressTicks > WorldPressProbe.MaxTickCount)
        {
            error = ProbePressStatus.RejectedInvalidCount;
            pressTicks = 0;
            return false;
        }

        error = ProbePressStatus.Started;
        return true;
    }

    public ProbePressStatus TryArm(
        int delayTicks,
        string direction,
        int pressTicks,
        ProbeWorldContext context,
        bool immediateProbeActive
    )
    {
        if (delayTicks < MinDelayTicks || delayTicks > MaxDelayTicks)
            return ProbePressStatus.RejectedInvalidDelay;
        if (!WorldPressProbe.TryNormalizeDirection(direction, out string key))
            return ProbePressStatus.RejectedInvalidDirection;
        if (pressTicks < WorldPressProbe.MinTickCount || pressTicks > WorldPressProbe.MaxTickCount)
            return ProbePressStatus.RejectedInvalidCount;

        if (!context.IsWorldReady)
            return ProbePressStatus.RejectedWorldNotReady;
        if (!context.IsMultiplayer)
            return ProbePressStatus.RejectedNotMultiplayer;
        if (context.IsMainPlayer)
            return ProbePressStatus.RejectedHost;
        if (context.IsSplitScreen)
            return ProbePressStatus.RejectedSplitScreen;
        if (this.IsPending)
            return ProbePressStatus.RejectedDelayedAlreadyPending;
        if (immediateProbeActive)
            return ProbePressStatus.RejectedImmediateProbeActive;

        this.direction = key;
        this.remainingDelayTicks = delayTicks;
        this.movementTicks = pressTicks;
        return ProbePressStatus.Started;
    }

    /// <summary>
    /// Satu update tick game. Countdown berkurang tepat 1. Trigger hanya
    /// setelah N tick, sekali, dan hanya jika gerbang context masih lolos.
    /// Tidak menekan tombol; pemanggil yang mendelegasikan ke P2.
    /// </summary>
    public DelayedProbeTickResult ConsumeTick(ProbeWorldContext context)
    {
        if (this.remainingDelayTicks <= 0)
            return DelayedProbeTickResult.Idle();

        this.remainingDelayTicks--;
        if (this.remainingDelayTicks > 0)
        {
            return DelayedProbeTickResult.Waiting(
                this.remainingDelayTicks,
                this.direction!,
                this.movementTicks
            );
        }

        string direction = this.direction!;
        int movement = this.movementTicks;
        this.ClearState();

        if (!context.IsWorldReady)
            return DelayedProbeTickResult.Rejected(ProbePressStatus.RejectedWorldNotReady);
        if (!context.IsMultiplayer)
            return DelayedProbeTickResult.Rejected(ProbePressStatus.RejectedNotMultiplayer);
        if (context.IsMainPlayer)
            return DelayedProbeTickResult.Rejected(ProbePressStatus.RejectedHost);
        if (context.IsSplitScreen)
            return DelayedProbeTickResult.Rejected(ProbePressStatus.RejectedSplitScreen);

        return DelayedProbeTickResult.Trigger(direction, movement);
    }

    public void Cancel()
    {
        this.ClearState();
    }

    private void ClearState()
    {
        this.remainingDelayTicks = 0;
        this.direction = null;
        this.movementTicks = 0;
    }
}

public readonly record struct DelayedProbeTickResult(
    DelayedProbeTickKind Kind,
    ProbePressStatus? Rejection,
    string? Direction,
    int MovementTicks,
    int RemainingDelayTicks
)
{
    public static DelayedProbeTickResult Idle() =>
        new(DelayedProbeTickKind.Idle, null, null, 0, 0);

    public static DelayedProbeTickResult Waiting(int remaining, string direction, int movement) =>
        new(DelayedProbeTickKind.Waiting, null, direction, movement, remaining);

    public static DelayedProbeTickResult Trigger(string direction, int movement) =>
        new(DelayedProbeTickKind.Trigger, null, direction, movement, 0);

    public static DelayedProbeTickResult Rejected(ProbePressStatus status) =>
        new(DelayedProbeTickKind.Rejected, status, null, 0, 0);
}

public enum DelayedProbeTickKind
{
    Idle,
    Waiting,
    Trigger,
    Rejected,
}
