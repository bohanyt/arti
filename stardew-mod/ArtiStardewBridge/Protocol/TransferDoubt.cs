namespace ArtiStardewBridge.Protocol;

/// <summary>
/// PM-1 fix6 #2 (attempt 6 stop, N:24828-24829): a menu transfer's doubt is two facts, not one.
/// EFFECT doubt (did the item move?) fences only another transfer on the doubted menu. CLEANUP doubt
/// (could input still be held?) fences every input and the release ack until native held evidence reads
/// released and stays released for <see cref="SettleMs"/>; time alone never resolves it. Base folded both
/// into one process-lifetime flag that fed ExecutionSlot.CleanupPending and gated the container exit,
/// so the open chest could never close and the release ack never came (600 s SAFETY_STOP).
/// </summary>
public sealed class TransferDoubt
{
    public const long SettleMs = 1500;
    bool effect, cleanup;
    long releasedSince = -1;
    public bool EffectDoubt => effect;

    public void Note(bool uncertain, bool cleanupConfirmed)
    {
        effect |= uncertain || !cleanupConfirmed;
        if (!cleanupConfirmed) { cleanup = true; releasedSince = -1; }
    }

    /// <param name="released">native held evidence: true = the synchronous click left nothing held (no cursor item).</param>
    public bool CleanupPending(bool released, long nowMs)
    {
        if (!cleanup) return false;
        if (!released) { releasedSince = -1; return true; }
        if (releasedSince < 0) releasedSince = nowMs;
        if (nowMs - releasedSince < SettleMs) return true;
        cleanup = false;
        return false;
    }

    /// <summary>The doubted menu is gone: the next open is a fresh native observation.</summary>
    public void MenuGone() { if (!cleanup) effect = false; }
}

/// <summary>
/// PM-1 fix6 #2: a chest transfer REPLACES its ItemGrabMenu (Chest.grabItemFromInventory -> ShowMenu), which moves
/// the receiver epoch the slot binding pinned. Base read that expected replacement as a lost binding: the read after
/// the click and the release refused, so every chest transfer came back TransferUncertain, cleanup unconfirmed.
/// The transfer's own observed same-container transition re-pins the epoch once; any other change stays a loss.
/// PM-1 fix8 (a6 N:24828, a7 N:2603/9736/19565): the admitted AttemptValidity pins the ADMISSION epoch too, so the
/// re-pinned epoch still failed IsValid (facts.Epoch != admitted epoch) and the release threw "original actor lost"
/// (cleanup_confirmed=false). An epoch reached only through that own transition presents the bound epoch to it.
/// </summary>
public sealed class TransferSlotBinding
{
    readonly Func<long> epoch;
    readonly Func<long, long, bool> valid;
    readonly long bound;
    long pinned;
    bool adopt;

    public TransferSlotBinding(Func<long> epoch, Func<long, long, bool> valid)
    { this.epoch = epoch; this.valid = valid; this.pinned = this.bound = epoch(); }

    public void OwnTransition() => this.adopt = true;

    public bool Valid()
    {
        long now = this.epoch();
        if (this.adopt && now != this.pinned) { this.pinned = now; this.adopt = false; }
        return now == this.pinned ? this.valid(this.bound, this.bound) : this.valid(now, this.pinned);
    }
}
