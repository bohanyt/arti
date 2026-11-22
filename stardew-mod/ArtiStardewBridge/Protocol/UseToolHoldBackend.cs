namespace ArtiStardewBridge.Protocol;

/// <summary>
/// G2-2 — pondasi SMAPI-free Hold/Release tombol pakai-alat setara pemain.
/// Bukan owner kedua: nasehat lease/stop/fault/expiry/release disuntik dari
/// <see cref="NativeActionOwner.Advise"/> dan kecocokan lease yang sudah ada.
/// Bukan op inbox, bukan transport per-frame Python, bukan fishing.
///
/// Adapter native kelak memetakan binding pertama
/// <c>Options.useToolButton</c> ke tombol terkonfigurasi lalu mengeksekusi
/// Press/Suppress. Backend ini hanya menyimpan niat/keadaan pengendali.
/// Daftar kosong gagal tertutup; tidak ada fallback binding gerak.
/// </summary>
public sealed class UseToolHoldBackend
{
    UseToolHoldPhase phase = UseToolHoldPhase.Idle;
    UseToolHoldBindingFence? pin;
    bool suppressEmitted;
    string? lastReason;

    public UseToolHoldPhase Phase => this.phase;
    public bool HasOwnedInflight =>
        this.phase is UseToolHoldPhase.Holding or UseToolHoldPhase.ReleaseOwed;
    public bool ReleaseOwed => this.phase == UseToolHoldPhase.ReleaseOwed;

    /// <summary>
    /// Binding pertama yang dikonfigurasi game, bukan tombol keras.
    /// List kosong/null gagal tertutup.
    /// </summary>
    public static bool TryMapFirstUseToolButton<TButton>(
        IReadOnlyList<TButton>? useToolButtons,
        out TButton button
    )
    {
        button = default!;
        if (useToolButtons is null || useToolButtons.Count == 0)
            return false;
        TButton first = useToolButtons[0];
        if (first is null)
            return false;
        if (first is string token && token.Length == 0)
            return false;
        button = first;
        return true;
    }

    public UseToolHoldTickResult Tick(
        MutationAdvice advice,
        bool leaseMatches,
        UseToolHoldIntent intent,
        UseToolHoldBindingFence current,
        bool bindingMapped,
        Action? press = null,
        Action? suppress = null,
        string? leaseRejectReason = null
    )
    {
        if (this.phase == UseToolHoldPhase.ReleaseOwed)
            return this.CarryDebt();

        bool ownerBlocks = OwnerBlocks(advice, leaseMatches, leaseRejectReason, out string ownerReason);
        bool hostSplit = HostOrSplit(current, out string hostReason);
        bool unmapped = !bindingMapped;

        if (this.phase == UseToolHoldPhase.Holding)
        {
            if (ownerBlocks)
                return this.BeginRelease(ownerReason, suppress);
            if (unmapped)
                return this.BeginRelease("UnmappedBinding", suppress);
            if (hostSplit)
                return this.BeginRelease(hostReason, suppress);
            if (this.pin is UseToolHoldBindingFence pin
                && FenceMismatch(pin, current, out string fenceReason))
            {
                return this.BeginRelease(fenceReason, suppress);
            }

            if (intent is UseToolHoldIntent.Release or UseToolHoldIntent.Neutral)
                return this.BeginRelease(null, suppress);

            // Hold sehat yang sudah aktif: idempoten, bukan kewajiban lepas.
            return new UseToolHoldTickResult(
                UseToolHoldPhase.Holding,
                UseToolHoldOutput.None,
                null,
                RequestOwnedRelease: false);
        }

        if (intent != UseToolHoldIntent.Hold)
        {
            return new UseToolHoldTickResult(
                UseToolHoldPhase.Idle,
                UseToolHoldOutput.None,
                null,
                RequestOwnedRelease: false);
        }

        if (ownerBlocks)
            return Reject(ownerReason);
        if (unmapped)
            return Reject("UnmappedBinding");
        if (hostSplit)
            return Reject(hostReason);

        this.pin = current;
        this.phase = UseToolHoldPhase.Holding;
        this.lastReason = null;
        this.suppressEmitted = false;
        press?.Invoke();
        // Press sekali; utang Suppress/release baru muncul saat Hold harus dinetralisasi.
        return new UseToolHoldTickResult(
            UseToolHoldPhase.Holding,
            UseToolHoldOutput.Press,
            null,
            RequestOwnedRelease: false);
    }

    public void AcknowledgeCleanup()
    {
        if (this.phase != UseToolHoldPhase.ReleaseOwed)
            return;
        this.phase = UseToolHoldPhase.Idle;
        this.pin = null;
        this.suppressEmitted = false;
        this.lastReason = null;
    }

    UseToolHoldTickResult BeginRelease(string? reason, Action? suppress)
    {
        this.lastReason = reason;
        this.phase = UseToolHoldPhase.ReleaseOwed;
        if (!this.suppressEmitted)
        {
            this.suppressEmitted = true;
            suppress?.Invoke();
            return new UseToolHoldTickResult(
                UseToolHoldPhase.ReleaseOwed,
                UseToolHoldOutput.Suppress,
                reason,
                RequestOwnedRelease: true);
        }

        return this.CarryDebt();
    }

    UseToolHoldTickResult CarryDebt() =>
        new(
            UseToolHoldPhase.ReleaseOwed,
            UseToolHoldOutput.None,
            this.lastReason,
            RequestOwnedRelease: true);

    static UseToolHoldTickResult Reject(string reason) =>
        new(UseToolHoldPhase.Idle, UseToolHoldOutput.None, reason, RequestOwnedRelease: false);

    static bool OwnerBlocks(
        MutationAdvice advice,
        bool leaseMatches,
        string? leaseRejectReason,
        out string reason
    )
    {
        if (!leaseMatches)
        {
            reason = string.IsNullOrEmpty(leaseRejectReason)
                ? "LeaseInvalid"
                : leaseRejectReason;
            return true;
        }

        if (advice.StopLatched)
        {
            reason = nameof(CommandRejectReason.StopLatched);
            return true;
        }

        if (advice.LeaseExpired)
        {
            reason = nameof(CommandRejectReason.LeaseExpired);
            return true;
        }

        if (string.Equals(
            advice.RejectReason,
            nameof(CommandRejectReason.ControlRecordFault),
            StringComparison.Ordinal))
        {
            reason = nameof(CommandRejectReason.ControlRecordFault);
            return true;
        }

        if (advice.RequestOwnedRelease)
        {
            reason = string.IsNullOrEmpty(advice.ReleaseReason)
                ? "ReleaseRequested"
                : advice.ReleaseReason;
            return true;
        }

        reason = "";
        return false;
    }

    static bool HostOrSplit(UseToolHoldBindingFence fence, out string reason)
    {
        if (fence.IsMainPlayer)
        {
            reason = "Host";
            return true;
        }

        if (fence.IsSplitScreen)
        {
            reason = "SplitScreen";
            return true;
        }

        reason = "";
        return false;
    }

    static bool FenceMismatch(
        UseToolHoldBindingFence pin,
        UseToolHoldBindingFence current,
        out string reason
    )
    {
        if (HostOrSplit(current, out reason))
            return true;
        if (!string.Equals(pin.RuntimeInstanceId, current.RuntimeInstanceId, StringComparison.Ordinal))
        {
            reason = nameof(CommandRejectReason.RuntimeMismatch);
            return true;
        }

        if (!string.Equals(pin.SessionId, current.SessionId, StringComparison.Ordinal))
        {
            reason = nameof(CommandRejectReason.SessionMismatch);
            return true;
        }

        if (pin.PlayerId != current.PlayerId)
        {
            reason = nameof(CommandRejectReason.PlayerMismatch);
            return true;
        }

        if (pin.ScreenId != current.ScreenId)
        {
            reason = nameof(CommandRejectReason.ScreenMismatch);
            return true;
        }

        if (pin.ContextGeneration != current.ContextGeneration)
        {
            reason = nameof(CommandRejectReason.BindingMismatch);
            return true;
        }

        if (!string.Equals(pin.LocationId, current.LocationId, StringComparison.Ordinal))
        {
            reason = nameof(CommandRejectReason.LocationMismatch);
            return true;
        }

        reason = "";
        return false;
    }
}

public enum UseToolHoldIntent
{
    Neutral,
    Hold,
    Release,
}

public enum UseToolHoldPhase
{
    Idle,
    Holding,
    ReleaseOwed,
}

public enum UseToolHoldOutput
{
    None,
    Press,
    Suppress,
}

public readonly record struct UseToolHoldBindingFence(
    bool IsMainPlayer,
    bool IsSplitScreen,
    string RuntimeInstanceId,
    string SessionId,
    long PlayerId,
    int ScreenId,
    long ContextGeneration,
    string LocationId
);

/// <summary>
/// Hasil satu tick backend. <see cref="RequestOwnedRelease"/> adalah
/// kewajiban cleanup/release milik backend (fase <c>ReleaseOwed</c>)
/// sampai <c>AcknowledgeCleanup</c>. Hold sehat (awal maupun berulang)
/// bukan kewajiban itu — jangan dibaca sebagai perintah lepas owner.
/// </summary>
public readonly record struct UseToolHoldTickResult(
    UseToolHoldPhase Phase,
    UseToolHoldOutput Output,
    string? Reason,
    bool RequestOwnedRelease
);
