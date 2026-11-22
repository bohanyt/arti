using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge.RenderTiming;

internal sealed class FarmhandRenderTiming : IDisposable
{
    private readonly InactiveSleepLease lease = new();
    private readonly Action<string> report;
    private readonly Func<GameRunner, bool> windowEligible;
    private GameRunner? watched;
    private string? reportedFailure;
    private bool disposed;

    internal FarmhandRenderTiming(Action<string> report, Func<GameRunner, bool>? windowEligible = null)
    {
        this.report = report;
        this.windowEligible = windowEligible ?? IsEligibleWindow;
    }

    internal static bool IsEligibleWindow(GameRunner runner) =>
        runner.Window.GetType().FullName == "Microsoft.Xna.Framework.SdlGameWindow"
        && Game1.graphics is not null && !Game1.graphics.IsFullScreen
        && Game1.options is not null && !Game1.options.fullscreen && !Game1.options.windowedBorderlessFullscreen
        && SdlWindowBinding.TryGetVisibleWindow(runner.Window.Handle, out _);

    public void Tick(bool enabled)
    {
        if (this.disposed) return;
        try
        {
            var runner = GameRunner.instance;
            bool eligible = enabled && Context.IsWorldReady && Context.IsMultiplayer
                && !Context.IsMainPlayer && !Context.IsSplitScreen && runner is not null
                && runner.gameInstances.Count == 1 && this.windowEligible(runner);
            this.lease.Update(runner, eligible,
                () => runner!.InactiveSleepTime, value => runner!.InactiveSleepTime = value);
            this.Watch(this.lease.Owner as GameRunner);
        }
        catch (Exception ex)
        {
            this.lease.Stop("eligibility/lifecycle failed: " + ex.GetType().Name);
        }
        this.ReportFailure();
    }

    private void Watch(GameRunner? runner)
    {
        if (ReferenceEquals(this.watched, runner)) return;
        if (this.watched is not null) this.watched.Exiting -= this.OnExiting;
        this.watched = runner;
        if (runner is not null) runner.Exiting += this.OnExiting;
    }

    private void OnExiting(object? sender, EventArgs args) => this.Restore();

    public void Restore()
    {
        this.lease.Restore();
        this.Watch(null);
        this.ReportFailure();
    }

    private void ReportFailure()
    {
        if (this.lease.Failure is not { } failure || failure == this.reportedFailure) return;
        this.reportedFailure = failure;
        this.report("S3 render timing disabled until process restart: " + failure
            + (this.lease.Owner is null ? "; no pending timing ownership." : "; original timing restoration UNCONFIRMED."));
    }

    public void Dispose()
    {
        if (this.disposed) return;
        this.disposed = true;
        this.Restore();
    }
}
