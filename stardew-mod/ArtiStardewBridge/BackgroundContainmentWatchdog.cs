using System.Diagnostics;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

/// <summary>
/// V4 §5 / C3 + V4.1 D2 containment owner, S2 layer (#278/5791090505, #278/5791607180).
///
/// One dedicated background <see cref="Thread"/> (never ThreadPool) that polls the
/// S1 <see cref="BackgroundContainmentPolicy"/> every ~50 ms on the monotonic
/// <see cref="Stopwatch.GetTimestamp"/> clock. It is not a gameplay owner:
/// <list type="bullet">
/// <item>it calls no game API and no input API;</item>
/// <item>it enumerates no processes and never touches HOST;</item>
/// <item>it reads only state published by the game thread (a volatile policy
/// reference plus the policy's own lock-protected fields);</item>
/// <item>its single destructive action is <see cref="Environment.FailFast(string)"/>
/// on its own process.</item>
/// </list>
/// Containment is UNCERTAIN/CONTAINED only: never PASS, release, completion or
/// retry. No file I/O happens before FailFast; the message carries the compact
/// reason plus request/runtime identity. 1500 ms (T3) is the bound to decide and
/// invoke FailFast, not a guarantee of OS process-death latency.
/// </summary>
internal sealed class BackgroundContainmentWatchdog
{
    public const int PollIntervalMs = 50;
    public const string ThreadName = "ARTI-BG-Containment";

    private readonly Action<string> failFast;
    private readonly Func<long> clockMs;
    private readonly int pollIntervalMs;
    private Thread? thread;
    private int started;
    private Published? published;
    private string? lastContainment;

    /// <summary>Production: the only action is <see cref="Environment.FailFast(string)"/>.</summary>
    public BackgroundContainmentWatchdog()
        : this(Environment.FailFast, NowMs, PollIntervalMs)
    {
    }

    /// <summary>Test seam for SMAPI-free verification (fake terminator/clock). Not used by ModEntry.</summary>
    internal BackgroundContainmentWatchdog(Action<string> failFast, Func<long> clockMs, int pollIntervalMs)
    {
        this.failFast = failFast ?? throw new ArgumentNullException(nameof(failFast));
        this.clockMs = clockMs ?? throw new ArgumentNullException(nameof(clockMs));
        this.pollIntervalMs = pollIntervalMs <= 0 ? PollIntervalMs : pollIntervalMs;
    }

    /// <summary>Monotonic milliseconds from <see cref="Stopwatch.GetTimestamp"/>; overflow-safe split.</summary>
    public static long NowMs()
    {
        long ts = Stopwatch.GetTimestamp();
        long freq = Stopwatch.Frequency;
        return ts / freq * 1000 + ts % freq * 1000 / freq;
    }

    public bool IsStarted => Volatile.Read(ref this.started) != 0;

    /// <summary>Last containment message handed to the terminator (test/diagnostic only).</summary>
    public string? LastContainment => Volatile.Read(ref this.lastContainment);

    /// <summary>Policy currently watched, or null.</summary>
    public BackgroundContainmentPolicy? Current => Volatile.Read(ref this.published)?.Policy;

    /// <summary>Start the dedicated thread once. Later calls are no-ops.</summary>
    public void Start()
    {
        if (Interlocked.Exchange(ref this.started, 1) != 0)
            return;
        Thread t = new(this.Loop)
        {
            IsBackground = true,
            Name = ThreadName,
        };
        this.thread = t;
        t.Start();
    }

    /// <summary>
    /// Game thread: publish the policy for the current background request. The
    /// caller must not replace a still-armed policy (the runtime keeps new
    /// background admissions busy while one is armed); this is enforced here too.
    /// </summary>
    public bool TryPublish(BackgroundContainmentPolicy policy, string requestId, string runtimeId)
    {
        if (policy is null)
            throw new ArgumentNullException(nameof(policy));
        Published? current = Volatile.Read(ref this.published);
        if (current is not null && !ReferenceEquals(current.Policy, policy) && current.Policy.Armed)
            return false;
        Volatile.Write(ref this.published, new Published(policy, requestId ?? "", runtimeId ?? ""));
        return true;
    }

    /// <summary>
    /// One poll. Returns the containment message that was handed to the terminator,
    /// or null. Called by the dedicated thread; exposed for SMAPI-free tests.
    /// </summary>
    internal string? PollOnce()
    {
        Published? p = Volatile.Read(ref this.published);
        if (p is null)
            return null;

        string? message = null;
        try
        {
            BgContainmentDecision? decision = p.Policy.Poll(this.clockMs());
            if (decision is not null)
                message = decision.FailFastMessage;
            else
                p.LastSeenArmed = p.Policy.Armed;
        }
        catch (Exception ex)
        {
            // The containment decision itself failed. If the watched policy was armed
            // at the last successful poll, fail safe: contain, never assume release.
            if (p.LastSeenArmed || ArmedOrUnknown(p.Policy))
            {
                message = BackgroundContainmentPolicy.FailFastPrefix + "WATCHDOG_FAULT:" + ex.GetType().Name
                    + "|request=" + p.RequestId + "|runtime=" + p.RuntimeId;
            }
        }

        if (message is null)
            return null;
        Volatile.Write(ref this.lastContainment, message);
        // Single destructive action. No logging/file I/O is allowed to delay it.
        this.failFast(message);
        return message;
    }

    private static bool ArmedOrUnknown(BackgroundContainmentPolicy policy)
    {
        try
        {
            return policy.Armed;
        }
        catch
        {
            return true;
        }
    }

    private void Loop()
    {
        while (true)
        {
            try
            {
                Thread.Sleep(this.pollIntervalMs);
                this.PollOnce();
            }
            catch
            {
                // Never let the watchdog thread die on an unexpected fault; PollOnce
                // already converts a policy fault while armed into containment.
            }
        }
    }

    private sealed class Published
    {
        public Published(BackgroundContainmentPolicy policy, string requestId, string runtimeId)
        {
            this.Policy = policy;
            this.RequestId = requestId;
            this.RuntimeId = runtimeId;
        }

        public BackgroundContainmentPolicy Policy { get; }
        public string RequestId { get; }
        public string RuntimeId { get; }

        /// <summary>Only touched by the watchdog thread.</summary>
        public bool LastSeenArmed { get; set; }
    }
}

/// <summary>
/// The admitted consumer call (<c>Parent.OnGame1_UpdateControlInput</c>). The three
/// input states flow through as ref parameters, never captured, so a substituted
/// value lands in the caller's own storage exactly as a direct Parent call would.
/// </summary>
internal delegate void BgFrameConsumer<TKeyboard, TMouse, TPad>(ref TKeyboard keyboard, ref TMouse mouse, ref TPad pad);

/// <summary>
/// CT correction C2: a substituted H/R/N (or pre-H neutral) frame is credited as
/// consumed ONLY after the admitted Parent call returns normally. Kept SMAPI-free
/// (and in this file) so the guard is testable without game binaries.
/// Containment arming for H stays with the caller, BEFORE the consumer runs.
/// </summary>
internal static class BackgroundFrameCommit
{
    /// <summary>
    /// Invoke the consumer exactly once. If it throws: <paramref name="onConsumerFault"/>
    /// runs (its own faults are swallowed so they never mask the original), nothing is
    /// credited, and the consumer's exception propagates unchanged. If it returns:
    /// <paramref name="commit"/> runs once; a commit fault goes to
    /// <paramref name="onCommitFault"/> and does not escape into the game.
    /// Returns true only when the frame was consumed and committed.
    /// </summary>
    public static bool InvokeThenCommit<TKeyboard, TMouse, TPad>(
        ref TKeyboard keyboard,
        ref TMouse mouse,
        ref TPad pad,
        BgFrameConsumer<TKeyboard, TMouse, TPad> consumer,
        Action commit,
        Action onConsumerFault,
        Action<Exception> onCommitFault)
    {
        bool returned = false;
        try
        {
            consumer(ref keyboard, ref mouse, ref pad);
            returned = true;
        }
        finally
        {
            if (!returned)
            {
                try
                {
                    onConsumerFault();
                }
                catch
                {
                    // Never replace the consumer's exception.
                }
            }
        }

        try
        {
            commit();
            return true;
        }
        catch (Exception ex)
        {
            try
            {
                onCommitFault(ex);
            }
            catch
            {
                // Fail-closed handling must not throw into Game1.UpdateControlInput.
            }
            return false;
        }
    }

    /// <summary>
    /// The S1 <see cref="BgHookEntry.Run"/> bookkeeping for an admitted substituted
    /// (move) or VERIFY-admitted (Stage 2 hold) frame, applied only after the consumer
    /// returned (unlike S1 Run's finally). For VERIFY the recorded S_k is the exact
    /// expected frame the consumer actually saw.
    /// </summary>
    public static void CommitMovingFrame(IBgFramePlan plan, BgHistoryInvariant invariant, BgHookDecision decision)
    {
        BgFrameKind frame = decision.Frame ?? throw new InvalidOperationException("decision has no frame");
        BgInputState substitute = decision.Substitute ?? throw new InvalidOperationException("decision has no substitute");
        BgHistoryCheck history = decision.History ?? throw new InvalidOperationException("decision has no history check");
        if (frame != BgFrameKind.H && plan.Next == BgFrameKind.H)
            plan.RequestRelease(decision.Reason ?? "RELEASE");
        plan.OnConsumed(frame);
        invariant.RecordConsumed(substitute, history);
    }
}
