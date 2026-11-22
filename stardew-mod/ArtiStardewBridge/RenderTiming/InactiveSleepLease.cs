namespace ArtiStardewBridge.RenderTiming;

// Game-thread only. Owns one runner's timing value, never input or focus.
internal sealed class InactiveSleepLease
{
    public object? Owner { get; private set; }
    public string? Failure { get; private set; }
    private Func<TimeSpan>? readOwned;
    private Action<TimeSpan>? writeOwned;
    private TimeSpan original;
    private bool restoreAttempted;

    public void Update(object? target, bool eligible, Func<TimeSpan> read, Action<TimeSpan> write)
    {
        if (this.Owner is not null && (!eligible || !ReferenceEquals(this.Owner, target)))
            this.Restore();
        if (this.Failure is not null || !eligible || target is null)
            return;

        try
        {
            if (this.Owner is not null)
            {
                if (this.readOwned!() != TimeSpan.Zero)
                    this.Stop("timing ownership changed");
                return;
            }

            TimeSpan before = read();
            if (before == TimeSpan.Zero)
                return; // someone else's zero is not ours to restore
            if (before < TimeSpan.Zero)
                throw new InvalidOperationException("negative original timing");
            this.original = before;
            this.readOwned = read;
            this.writeOwned = write;
            this.Owner = target; // retain identity before a potentially partial write
            this.restoreAttempted = false;
            write(TimeSpan.Zero);
            if (read() != TimeSpan.Zero)
                throw new InvalidOperationException("zero timing not confirmed");
        }
        catch (Exception ex)
        {
            this.Stop("apply/verification failed: " + ex.GetType().Name);
        }
    }

    public void Stop(string reason)
    {
        this.Failure ??= reason;
        this.Restore();
    }

    public void Restore()
    {
        if (this.Owner is null || this.restoreAttempted)
            return;
        this.restoreAttempted = true;
        try
        {
            TimeSpan current = this.readOwned!();
            if (current != this.original)
            {
                if (current != TimeSpan.Zero)
                    throw new InvalidOperationException("foreign timing value; refusing overwrite");
                this.writeOwned!(this.original);
                if (this.readOwned() != this.original)
                    throw new InvalidOperationException("original timing not confirmed");
            }
            this.Owner = null;
            this.readOwned = null;
            this.writeOwned = null;
        }
        catch (Exception ex)
        {
            // Keep the original runner/value. No automatic retry or new admission.
            this.Failure = (this.Failure is null ? "" : this.Failure + "; ")
                + "restore unresolved: " + ex.Message;
        }
    }
}
