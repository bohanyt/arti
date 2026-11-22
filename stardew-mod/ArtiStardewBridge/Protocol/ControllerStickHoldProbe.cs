using System.Globalization;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// Probe TEST-ONLY P5-R3C: tahan analog stick selama N tick, lalu SATU
/// Suppress eksplisit. Disabled-by-default. Bukan motor production.
///
/// Provenance (jangan di-merge buta):
/// - PR #70 (revision omitted): native-snap
///   guard, Suppress sebagai rilis publik SMAPI, snapshot neighbor.
/// - Spike (a local branch)
///   (revision omitted): bukti analog
///   LeftThumbstick* menggerakkan snap 102→103→104, plus perintah setup
///   menu. Implementasi hold/release spike DITOLAK (lihat cacat di bawah).
/// - Canonical P5-R1: <c>Helper.Input.Press</c> → OverrideButton(setDown
///   true); TrueUpdate mengosongkan CustomPressedKeys tiap tick (IL_0134),
///   jadi satu Press adalah pulsa satu tick kecuali di-refresh.
///
/// Cacat spike yang kontrak ini cegah:
/// 1. Model mencatat rilis selesai sebelum wrapper Suppress; wrapper malah
///    Press ulang. Di sini ConsumeTicking(Release) TIDAK menghapus utang;
///    hanya AcknowledgeRelease setelah Suppress sungguhan.
/// 2. Hold dijalankan dari UpdateTicked pada offset jarang. Di sini hold
///    hanya maju di ConsumeTicking setiap tick.
/// 3. Counter 30 tidak habis di jendela observasi. Durasi dihitung dari
///    AcknowledgePress, bukan dari ConsumeTicked.
/// 4. Cleanup cancel/title membaca utang yang sudah dibersihkan. Utang
///    driver terpisah, lunas hanya setelah Suppress dipanggil.
/// 5. Command menulis counter sebelum diterima. TryParse/TryArm
///    memvalidasi dulu; overlap/invalid tidak menulisi trial aktif.
///
/// Timing:
/// - delayTicks = jumlah ConsumeTicking yang mengembalikan Waiting sebelum
///   Press pertama. delay 0 = Press di tick ticking pertama.
/// - holdTicks = jumlah Press yang wajib di-ack. Tick ticking berikutnya
///   adalah niat Release / Suppress.
/// - UpdateTicked hanya observasi.
///
/// Batas hold 1..60 (satu detik @60fps): cukup untuk matriks laptop
/// 1/2/3/5/8/12/20/30/45/60, sama dengan cap WorldPressProbe, tidak ada
/// hold tak terbatas. Ambang repeat game tetap UNKNOWN.
///
/// Unavoidable: kalau proses mati atau event SMAPI berhenti, Suppress
/// tidak bisa dijamin.
/// </summary>
public sealed class ControllerStickHoldProbe
{
    public const string CommandName = "arti_probe_stickhold";
    public const int MinHoldTicks = 1;
    public const int MaxHoldTicks = 60;
    public const int MinDelayTicks = 0;
    public const int MaxDelayTicks = 1800;

    /// <summary>
    /// P5-R1 IL_0134: CustomPressedKeys dikosongkan tiap tick. Satu Press
    /// tidak boleh dianggap bertahan N tick. Driver wajib Press ulang.
    /// </summary>
    public const bool RefreshPressEachHeldTick = true;

    public static readonly IReadOnlyList<int> SparseObserveOffsets =
        new[] { 0, 1, 2, 3, 4, 5, 10, 20, 30, 60 };

    private static readonly string[] AllowedDirections = { "down", "right", "up", "left" };
    private static int nextRunId = 1;

    private StickHoldPhase phase = StickHoldPhase.Idle;
    private int remainingDelayTicks;
    private int remainingHoldTicks;
    private int requestedHoldTicks;
    private int pressCount;
    private int releaseIssuedCount;
    private bool releaseOwed;
    private string? direction;
    private int runId;
    private string? armedMenuType;
    private string? armedPageType;
    private int armedScreenId;
    private uint lastTickingTick;
    private bool haveLastTickingTick;
    private int observeOffset = -1;
    private bool suppressSeen;
    private bool terminalCleanup;

    public StickHoldPhase Phase => this.phase;
    public bool IsActive => this.phase != StickHoldPhase.Idle;
    public string? ActiveDirection => this.direction;
    public int RemainingDelayTicks => this.remainingDelayTicks;
    public int RemainingHoldTicks => this.remainingHoldTicks;
    public int RequestedHoldTicks => this.requestedHoldTicks;
    public int PressCount => this.pressCount;
    public int ReleaseIssuedCount => this.releaseIssuedCount;
    public bool ReleaseOwed => this.releaseOwed;
    public int RunId => this.runId;

    public static bool TryNormalizeDirection(string? raw, out string normalized)
    {
        normalized = (raw ?? "").Trim().ToLowerInvariant();
        if (normalized.StartsWith("leftthumbstick", StringComparison.Ordinal))
            normalized = normalized["leftthumbstick".Length..];
        if (normalized.StartsWith("lstick", StringComparison.Ordinal))
            normalized = normalized["lstick".Length..];
        if (normalized.StartsWith("stick", StringComparison.Ordinal))
            normalized = normalized["stick".Length..];
        if (normalized.StartsWith("dpad", StringComparison.Ordinal))
            normalized = normalized["dpad".Length..];
        if (Array.IndexOf(AllowedDirections, normalized) >= 0)
            return true;
        normalized = "";
        return false;
    }

    public static string StickButtonName(string direction)
    {
        if (!TryNormalizeDirection(direction, out string key))
            return "";
        return key switch
        {
            "down" => "LeftThumbstickDown",
            "right" => "LeftThumbstickRight",
            "up" => "LeftThumbstickUp",
            "left" => "LeftThumbstickLeft",
            _ => "",
        };
    }

    public static bool TryParseCommand(
        string[]? args,
        out string direction,
        out int holdTicks,
        out int delayTicks,
        out StickHoldStatus error
    )
    {
        direction = "";
        holdTicks = 0;
        delayTicks = 0;
        if (args is null || args.Length is < 2 or > 3
            || !TryNormalizeDirection(args[0], out direction))
        {
            error = StickHoldStatus.RejectedInvalidDirection;
            direction = "";
            return false;
        }

        if (!int.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out holdTicks)
            || holdTicks < MinHoldTicks
            || holdTicks > MaxHoldTicks)
        {
            error = StickHoldStatus.RejectedInvalidHold;
            direction = "";
            holdTicks = 0;
            return false;
        }

        if (args.Length == 3
            && (!int.TryParse(args[2], NumberStyles.None, CultureInfo.InvariantCulture, out delayTicks)
                || delayTicks < MinDelayTicks
                || delayTicks > MaxDelayTicks))
        {
            error = StickHoldStatus.RejectedInvalidDelay;
            direction = "";
            holdTicks = 0;
            delayTicks = 0;
            return false;
        }

        error = StickHoldStatus.Started;
        return true;
    }

    public StickHoldStatus TryArm(
        string direction,
        int holdTicks,
        int delayTicks,
        StickHoldRuntimeContext context
    )
    {
        if (!TryNormalizeDirection(direction, out string key))
            return StickHoldStatus.RejectedInvalidDirection;
        if (holdTicks < MinHoldTicks || holdTicks > MaxHoldTicks)
            return StickHoldStatus.RejectedInvalidHold;
        if (delayTicks < MinDelayTicks || delayTicks > MaxDelayTicks)
            return StickHoldStatus.RejectedInvalidDelay;
        if (!context.Enabled)
            return StickHoldStatus.RejectedDisabled;
        if (!context.World.IsWorldReady)
            return StickHoldStatus.RejectedWorldNotReady;
        if (!context.World.IsMultiplayer)
            return StickHoldStatus.RejectedNotMultiplayer;
        if (context.World.IsMainPlayer)
            return StickHoldStatus.RejectedHost;
        if (context.World.IsSplitScreen)
            return StickHoldStatus.RejectedSplitScreen;
        if (!context.MenuOpen)
            return StickHoldStatus.RejectedNoMenu;
        if (!context.ExistingSnapPresent)
            return StickHoldStatus.RejectedNoExistingSnap;
        if (context.OtherProbeBusy || this.IsActive || this.releaseOwed)
            return StickHoldStatus.RejectedAlreadyActive;

        this.direction = key;
        this.requestedHoldTicks = holdTicks;
        this.remainingHoldTicks = holdTicks;
        this.remainingDelayTicks = delayTicks;
        this.phase = StickHoldPhase.Waiting;
        this.pressCount = 0;
        this.releaseIssuedCount = 0;
        this.releaseOwed = false;
        this.runId = nextRunId++;
        this.armedMenuType = context.MenuType;
        this.armedPageType = context.PageType;
        this.armedScreenId = context.ScreenId;
        this.haveLastTickingTick = false;
        this.observeOffset = -1;
        this.suppressSeen = false;
        this.terminalCleanup = false;
        return StickHoldStatus.Started;
    }

    /// <summary>
    /// Satu UpdateTicking. Kind Press/Release adalah NIAT. Utang rilis tidak
    /// lunas di sini. Pemanggil wajib Press/Suppress lalu Acknowledge*.
    /// Tick <paramref name="updateTick"/> yang sama dua kali = Idle, tanpa mutasi.
    /// </summary>
    public StickHoldTickingResult ConsumeTicking(uint updateTick, StickHoldRuntimeContext context)
    {
        if (this.haveLastTickingTick && this.lastTickingTick == updateTick)
        {
            return new StickHoldTickingResult(StickHoldTickingKind.Idle, this.remainingDelayTicks, this.direction);
        }

        if (this.phase == StickHoldPhase.Idle && !this.releaseOwed)
        {
            return new StickHoldTickingResult(StickHoldTickingKind.Idle, 0, null);
        }

        this.lastTickingTick = updateTick;
        this.haveLastTickingTick = true;

        if (this.releaseOwed && this.phase is StickHoldPhase.Idle or StickHoldPhase.Releasing)
        {
            this.phase = StickHoldPhase.Releasing;
            return new StickHoldTickingResult(StickHoldTickingKind.CleanupRelease, 0, this.direction);
        }

        if (this.phase is StickHoldPhase.Waiting or StickHoldPhase.Holding)
        {
            if (!context.Enabled
                || !context.World.IsWorldReady
                || !context.World.IsMultiplayer
                || context.World.IsMainPlayer
                || context.World.IsSplitScreen)
            {
                return this.AbortForContext("guard");
            }

            bool contextChanged = context.MenuType != this.armedMenuType
                || context.PageType != this.armedPageType
                || context.ScreenId != this.armedScreenId
                || !context.MenuOpen
                || !context.ExistingSnapPresent;

            if (contextChanged)
            {
                if (this.pressCount > 0 || this.releaseOwed)
                    return this.AbortForContext("context");
                this.ResetBookkeeping(keepOwedRelease: false);
                return new StickHoldTickingResult(StickHoldTickingKind.Rejected, 0, null);
            }
        }

        if (this.phase == StickHoldPhase.Waiting)
        {
            if (this.remainingDelayTicks > 0)
            {
                this.remainingDelayTicks--;
                return new StickHoldTickingResult(
                    StickHoldTickingKind.Waiting, this.remainingDelayTicks, this.direction);
            }

            this.phase = StickHoldPhase.Holding;
        }

        if (this.phase == StickHoldPhase.Holding)
        {
            if (this.remainingHoldTicks > 0)
            {
                return new StickHoldTickingResult(StickHoldTickingKind.Press, 0, this.direction);
            }

            this.phase = StickHoldPhase.Releasing;
            return new StickHoldTickingResult(StickHoldTickingKind.Release, 0, this.direction);
        }

        if (this.phase == StickHoldPhase.Releasing)
        {
            return new StickHoldTickingResult(
                this.releaseOwed ? StickHoldTickingKind.Release : StickHoldTickingKind.Idle,
                0,
                this.direction
            );
        }

        if (this.phase == StickHoldPhase.Observing)
        {
            return new StickHoldTickingResult(StickHoldTickingKind.Idle, 0, this.direction);
        }

        return new StickHoldTickingResult(StickHoldTickingKind.Idle, 0, this.direction);
    }

    public void AcknowledgePress()
    {
        if (this.phase != StickHoldPhase.Holding || this.remainingHoldTicks <= 0)
            return;
        this.pressCount++;
        this.remainingHoldTicks--;
        this.releaseOwed = true;
        this.observeOffset = Math.Max(this.observeOffset, 0);
    }

    public void AcknowledgeRelease()
    {
        if (!this.releaseOwed)
            return;
        this.releaseOwed = false;
        this.releaseIssuedCount++;
        this.suppressSeen = true;
        this.remainingHoldTicks = 0;
        this.remainingDelayTicks = 0;
        if (this.terminalCleanup)
        {
            this.ResetBookkeeping(keepOwedRelease: false);
            this.terminalCleanup = false;
            return;
        }

        this.phase = StickHoldPhase.Observing;
        this.observeOffset = 0;
    }

    public StickHoldObserveResult ConsumeTicked(uint updateTick)
    {
        _ = updateTick;
        if (this.phase == StickHoldPhase.Idle && !this.releaseOwed)
            return StickHoldObserveResult.Skip();

        if (this.phase is StickHoldPhase.Waiting)
        {
            return new StickHoldObserveResult(
                ShouldLog: this.remainingDelayTicks % 60 == 0,
                Dense: false,
                Offset: -1,
                PhaseLabel: ControllerStickHoldLabels.Waiting,
                ReleaseIntent: false,
                SuppressAlreadyIssued: this.suppressSeen,
                RunId: this.runId,
                RequestedHoldTicks: this.requestedHoldTicks,
                RemainingHoldTicks: this.remainingHoldTicks,
                PressCount: this.pressCount,
                ReleaseIssuedCount: this.releaseIssuedCount
            );
        }

        if (this.phase is StickHoldPhase.Holding or StickHoldPhase.Releasing)
        {
            int offset = this.observeOffset < 0 ? 0 : this.observeOffset;
            if (this.phase == StickHoldPhase.Holding && this.observeOffset >= 0)
                this.observeOffset++;
            return new StickHoldObserveResult(
                ShouldLog: true,
                Dense: true,
                Offset: offset,
                PhaseLabel: this.phase == StickHoldPhase.Releasing
                    ? ControllerStickHoldLabels.ReleaseIntent
                    : ControllerStickHoldLabels.Holding,
                ReleaseIntent: this.phase == StickHoldPhase.Releasing,
                SuppressAlreadyIssued: this.suppressSeen,
                RunId: this.runId,
                RequestedHoldTicks: this.requestedHoldTicks,
                RemainingHoldTicks: this.remainingHoldTicks,
                PressCount: this.pressCount,
                ReleaseIssuedCount: this.releaseIssuedCount
            );
        }

        if (this.phase == StickHoldPhase.Observing)
        {
            int offset = this.observeOffset;
            this.observeOffset++;
            int last = SparseObserveOffsets[SparseObserveOffsets.Count - 1];
            bool log = SparseObserveOffsets.Contains(offset);
            if (offset >= last)
                this.ResetBookkeeping(keepOwedRelease: false);
            return new StickHoldObserveResult(
                ShouldLog: log,
                Dense: offset <= 5,
                Offset: log ? offset : -1,
                PhaseLabel: ControllerStickHoldLabels.AfterSuppress,
                ReleaseIntent: false,
                SuppressAlreadyIssued: true,
                RunId: this.runId,
                RequestedHoldTicks: this.requestedHoldTicks,
                RemainingHoldTicks: 0,
                PressCount: this.pressCount,
                ReleaseIssuedCount: this.releaseIssuedCount
            );
        }

        return StickHoldObserveResult.Skip();
    }

    public void RequestCleanup(string reason)
    {
        _ = reason;
        this.terminalCleanup = true;
        bool keep = this.releaseOwed || this.pressCount > this.releaseIssuedCount;
        this.ResetBookkeeping(keepOwedRelease: keep);
        if (keep)
            this.phase = StickHoldPhase.Releasing;
    }

    private StickHoldTickingResult AbortForContext(string reason)
    {
        _ = reason;
        this.terminalCleanup = true;
        bool keep = this.releaseOwed || this.pressCount > 0;
        this.ResetBookkeeping(keepOwedRelease: keep);
        if (keep)
        {
            this.phase = StickHoldPhase.Releasing;
            return new StickHoldTickingResult(StickHoldTickingKind.CleanupRelease, 0, this.direction);
        }

        return new StickHoldTickingResult(StickHoldTickingKind.Rejected, 0, null);
    }

    private void ResetBookkeeping(bool keepOwedRelease)
    {
        string? keepDir = keepOwedRelease ? this.direction : null;
        this.phase = StickHoldPhase.Idle;
        this.remainingDelayTicks = 0;
        this.remainingHoldTicks = 0;
        if (!keepOwedRelease)
        {
            this.requestedHoldTicks = 0;
            this.direction = null;
            this.armedMenuType = null;
            this.armedPageType = null;
            this.observeOffset = -1;
        }
        else
        {
            this.direction = keepDir;
        }

        this.releaseOwed = keepOwedRelease;
        this.haveLastTickingTick = false;
    }
}

public static class ControllerMenuSetup
{
    public const string CommandName = "arti_probe_menu";

    /// <summary>
    /// Setup-only: kelayakan membuka GameMenu lewat konstruktor, bukan bukti
    /// motor bisa membuka menu lewat input. Tidak mengubah state hold.
    /// </summary>
    public static StickHoldStatus Evaluate(
        bool enabled,
        ProbeWorldContext world,
        bool menuAlreadyOpen,
        bool stickHoldBusy
    )
    {
        if (!enabled)
            return StickHoldStatus.RejectedDisabled;
        if (!world.IsWorldReady)
            return StickHoldStatus.RejectedWorldNotReady;
        if (!world.IsMultiplayer)
            return StickHoldStatus.RejectedNotMultiplayer;
        if (world.IsMainPlayer)
            return StickHoldStatus.RejectedHost;
        if (world.IsSplitScreen)
            return StickHoldStatus.RejectedSplitScreen;
        if (stickHoldBusy)
            return StickHoldStatus.RejectedAlreadyActive;
        if (menuAlreadyOpen)
            return StickHoldStatus.MenuAlreadyOpen;
        return StickHoldStatus.Started;
    }
}

public static class ControllerStickHoldLabels
{
    public const string Waiting = "WAITING";
    public const string Holding = "HOLDING";
    public const string ReleaseIntent = "RELEASE_INTENT";
    public const string SuppressIssued = "SUPPRESS_ISSUED";
    public const string AfterSuppress = "AFTER_SUPPRESS";
    public const string ObservedNeutral = "OBSERVED_NEUTRAL";
}

public enum StickHoldPhase
{
    Idle,
    Waiting,
    Holding,
    Releasing,
    Observing,
}

public enum StickHoldTickingKind
{
    Idle,
    Waiting,
    Press,
    Release,
    CleanupRelease,
    Rejected,
}

public enum StickHoldInputOp
{
    None,
    Press,
    Suppress,
}

public enum StickHoldStatus
{
    Started,
    MenuAlreadyOpen,
    RejectedDisabled,
    RejectedInvalidDirection,
    RejectedInvalidHold,
    RejectedInvalidDelay,
    RejectedWorldNotReady,
    RejectedNotMultiplayer,
    RejectedHost,
    RejectedSplitScreen,
    RejectedNoMenu,
    RejectedNoExistingSnap,
    RejectedAlreadyActive,
}

public readonly record struct StickHoldRuntimeContext(
    ProbeWorldContext World,
    bool Enabled,
    bool MenuOpen,
    bool ExistingSnapPresent,
    bool OtherProbeBusy,
    string? MenuType,
    string? PageType,
    int ScreenId
);

public readonly record struct StickHoldTickingResult(
    StickHoldTickingKind Kind,
    int RemainingDelayTicks,
    string? Direction
);

public readonly record struct StickHoldObserveResult(
    bool ShouldLog,
    bool Dense,
    int Offset,
    string PhaseLabel,
    bool ReleaseIntent,
    bool SuppressAlreadyIssued,
    int RunId,
    int RequestedHoldTicks,
    int RemainingHoldTicks,
    int PressCount,
    int ReleaseIssuedCount
)
{
    public static StickHoldObserveResult Skip() => new(
        false, false, -1, "", false, false, 0, 0, 0, 0, 0);
}

public readonly record struct StickHoldDriverReport(
    StickHoldTickingKind Kind,
    StickHoldInputOp InputOp,
    string PhaseLabel,
    bool ReleaseIntent,
    bool SuppressIssued,
    int RunId
);
