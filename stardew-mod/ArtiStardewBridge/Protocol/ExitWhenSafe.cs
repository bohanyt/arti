using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// ADR 256, "exit when safe" (an internal design note). The decision to close a
/// session-owned game is taken inside that game, at a tick of its own. The supervisor writes <c>exit_request.json</c> in
/// the mod's own folder.
/// <para>Review W1b E2: a request is honoured only while its requester's ownership is provable from the request itself
/// and its freshness:</para>
/// <list type="bullet">
/// <item>it names this game (runtime, save and player, plus the run on the HOST) and this process (review M4d item 4:
/// <c>target_pid</c>, the recorded launch it is for; any other process refuses it);</item>
/// <item>its requester process is alive (pid + image, read by the mod);</item>
/// <item>it was issued no later than now and within <see cref="MaxHorizonSeconds"/> of its deadline;</item>
/// <item>the mod still owns its game (the HOST: its own HOST and save, no latched stop);</item>
/// <item>the file still holds the same request when the mod decides (re-read).</item>
/// </list>
/// <para>The safe hold counts only sampled ticks: a gap longer than <see cref="MaxSampleGapSeconds"/>, or a clock that went
/// back, restarts it.</para>
/// <para>Review W1b E3: a decision is published before it counts. The mod writes the answer durably (flushed and read
/// back), then calls <see cref="Published"/>. Only a published "exiting" answer may be followed by the quit; a failed
/// write never quits, and the hold starts again.</para>
/// <para>Pure and shared by both mods; the mods supply the game facts and do the file and quit work.</para>
/// </summary>
public sealed class ExitWhenSafe
{
    public const string RequestFile = "exit_request.json", AnswerFile = "exit_answer.json";
    public const double SafeHoldSeconds = 2.0, WaitingEverySeconds = 1.0;
    /// <summary>W1b E2: the longest request the mod honours (issue to deadline), and the clock slack it allows.</summary>
    public const double MaxHorizonSeconds = 180, ClockSlackSeconds = 5;
    /// <summary>W1b E2: a longer gap between sampled ticks (polling paused, world not ready) restarts the safe hold.</summary>
    public const double MaxSampleGapSeconds = 0.5;
    /// <summary>W1b E2: "exiting" is decided only with at least this much left before the deadline (I/O before the quit).</summary>
    public const double ExitMarginSeconds = 1.0;
    public const string Exiting = "exiting", Waiting = "waiting", Expired = "expired", RefusedIdentity = "refused:identity",
        RefusedStale = "refused:stale", RefusedRequesterGone = "refused:requester_gone", RefusedNotOwned = "refused:not_owned",
        // Review M4 E2: the request died after "exiting" was published (revoked, replaced, past its deadline, a stop or a
        // handoff during the write): no quit, and this answer replaces the "exiting" one.
        Aborted = "aborted",
        // Review M4b (ADR 255): the owner is using the HOST (any input of his in the last 180 s): never quit under him.
        RefusedOwnerActive = "refused:owner_active";

    readonly HashSet<string> answered = new(StringComparer.Ordinal);
    string? current;
    System.DateTimeOffset? safeSince, lastSample, retryAfter;
    System.DateTimeOffset lastWaiting = System.DateTimeOffset.MinValue;

    public sealed record Request(string RequestId, string RunId, string RuntimeId, ulong SaveId, long PlayerId,
        int RequesterPid, string RequesterImage, string Reason, System.DateTimeOffset Issued, System.DateTimeOffset Deadline,
        string? Session = null, int TargetPid = 0);

    /// <summary>What the mod knows about its own game this tick. <c>SaveId</c>/<c>PlayerId</c> are null while the world is
    /// not ready. <c>RunId</c> is null for the farmhand (it has no run of its own and never claims the request's).</summary>
    /// <para>Review M4 E2: <c>RequiredSession</c> is the controller session the request must name (the HOST: the session of
    /// the last harness heartbeat it read, so a new supervisor or a handoff ends older requests). Null = not required (the
    /// farmhand, which has no heartbeat; its own halt is part of <c>Owned</c>).</para>
    /// <para>Review M4b: <c>OwnerActive</c> = the owner gave input in this game in the last 180 s (ADR 255). A request that
    /// meets it is refused (<see cref="RefusedOwnerActive"/>), or aborted if its hold had begun.</para>
    /// <para>Review M4d item 4: <c>ProcessId</c> = this game's own process id; a request whose <c>target_pid</c> is another
    /// process is refused (<see cref="RefusedIdentity"/>, why <c>not_this_process</c>).</para>
    public sealed record Self(string? RunId, string RuntimeId, ulong? SaveId, long? PlayerId, bool Owned, string? RequiredSession = null,
        bool OwnerActive = false, int ProcessId = 0);

    /// <summary>Review M4b: the request file is read on the first call, then every 15 ticks, and again if the tick count went
    /// back (a new world). No sentinel arithmetic: an int.MinValue start wrapped negative and the first read never came.</summary>
    public static bool ReadDue(int? lastReadTick, int nowTick) =>
        lastReadTick is not int last || nowTick < last || (long)nowTick - last >= 15;

    /// <summary>One decision: the answer to publish now (null = none) and whether, once it is published, the mod quits.</summary>
    public sealed record Decision(Dictionary<string, object> Answer, bool Exit, Request For)
    {
        public string State => (string)this.Answer["state"];
    }

    /// <summary>The request, or null when the file is absent or not a complete v1 request (a malformed file is not
    /// answered). Every field is required and typed; anything else is null, never a throw.</summary>
    public static Request? Parse(string? json)
    {
        if (string.IsNullOrWhiteSpace(json)) return null;
        try
        {
            using var doc = JsonDocument.Parse(json);
            var r = doc.RootElement;
            if (r.ValueKind != JsonValueKind.Object || !r.TryGetProperty("v", out var v) || v.ValueKind != JsonValueKind.Number
                || !v.TryGetInt32(out int version) || version != 1)
                return null;
            string? Str(string name) => r.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;
            long? Int(string name) => r.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.Number && p.TryGetInt64(out long n) ? n : null;
            System.DateTimeOffset? When(string name) => Str(name) is { } s && System.DateTimeOffset.TryParse(s, null,
                System.Globalization.DateTimeStyles.AssumeUniversal, out var t) ? t : null;
            if (Str("request_id") is not { Length: > 0 } id || Str("run_id") is not { Length: > 0 } run
                || Str("runtime_id") is not { Length: > 0 } runtime
                || Str("save_id") is not { Length: > 0 } saveText || !ulong.TryParse(saveText, out ulong save)
                || Int("player_id") is not long player || Int("requester_pid") is not long pid || pid <= 0 || pid > int.MaxValue
                || Int("target_pid") is not long target || target <= 0 || target > int.MaxValue
                || Str("requester_image") is not { Length: > 0 } image
                || When("issued_utc") is not { } issued || When("deadline_utc") is not { } until)
                return null;
            return new Request(id, run, runtime, save, player, (int)pid, image, Str("reason") ?? "", issued, until,
                Str("session") is { Length: > 0 } session ? session : null, (int)target);
        }
        catch { return null; }
    }

    /// <summary>
    /// One tick. <paramref name="why"/> is null on a safe tick, else the reason it is not safe (<see cref="Why"/>).
    /// <paramref name="requesterAlive"/> is the mod's own read of the requester process. Returns the decision to publish
    /// now, or null. Nothing is counted as answered until <see cref="Published"/> says the answer landed.
    /// </summary>
    public Decision? Tick(Request? request, Self self, Func<int, string, bool> requesterAlive, string? why,
        System.DateTimeOffset now, string gameDate, int gameTime)
    {
        System.DateTimeOffset? previous = this.lastSample;
        this.lastSample = now;
        if (request is null) { this.current = null; this.safeSince = null; return null; }
        if (this.answered.Contains(request.RequestId)) return null;
        // W1b E3: after a failed write, the next attempt waits a second (no write storm on a denied folder).
        if (this.retryAfter is { } ra && now < ra && now >= ra.AddSeconds(-WaitingEverySeconds)) return null;
        if (this.current != request.RequestId)
        {
            this.current = request.RequestId; this.safeSince = null; this.lastWaiting = System.DateTimeOffset.MinValue;
        }
        // A paused poll, a long gap or a clock that went back is unobserved time: the hold starts again.
        if (previous is not { } p || now < p || (now - p).TotalSeconds > MaxSampleGapSeconds)
            this.safeSince = null;

        if (request.Issued > now.AddSeconds(ClockSlackSeconds) || request.Deadline <= request.Issued
            || (request.Deadline - request.Issued).TotalSeconds > MaxHorizonSeconds)
            return Terminal(request, RefusedStale, now, null, gameDate, gameTime);
        if (now >= request.Deadline)
            return Terminal(request, Expired, now, null, gameDate, gameTime);
        // Review M4d item 4: a request names the process it is for (the recorded launch); any other process refuses it.
        if (request.TargetPid != self.ProcessId)
            return Terminal(request, RefusedIdentity, now, "not_this_process", gameDate, gameTime);
        if (self.SaveId is not ulong saveId || self.PlayerId is not long playerId)
            return this.NotYet(request, "world_not_ready", now, gameDate, gameTime);
        if (request.RuntimeId != self.RuntimeId || request.SaveId != saveId || request.PlayerId != playerId
            || self.RunId is not null && request.RunId != self.RunId)
            return Terminal(request, RefusedIdentity, now, null, gameDate, gameTime);
        if (self.OwnerActive)
            // Review M4b: never under the owner. A request whose hold had begun is aborted; a new one is refused.
            return Terminal(request, this.current == request.RequestId && this.safeSince is not null ? Aborted : RefusedOwnerActive,
                now, "owner_active", gameDate, gameTime);
        if (!self.Owned || self.RequiredSession is not null && request.Session != self.RequiredSession)
            return Terminal(request, RefusedNotOwned, now, null, gameDate, gameTime);
        bool alive;
        try { alive = requesterAlive(request.RequesterPid, request.RequesterImage); }
        catch { alive = false; }
        if (!alive)
            return Terminal(request, RefusedRequesterGone, now, null, gameDate, gameTime);

        if (why is not null)
            return this.NotYet(request, why, now, gameDate, gameTime);
        this.safeSince ??= now;
        if ((now - this.safeSince.Value).TotalSeconds < SafeHoldSeconds)
            return this.NotYet(request, "holding_safe", now, gameDate, gameTime, keepHold: true);
        if ((request.Deadline - now).TotalSeconds < ExitMarginSeconds)
            return this.NotYet(request, "deadline_too_close", now, gameDate, gameTime);
        return new Decision(Answer(request, Exiting, now, null, gameDate, gameTime), true, request);
    }

    /// <summary>
    /// W1b E3: the mod's report on writing <paramref name="decision"/>. A published terminal answer (exiting, expired,
    /// refused) closes its request; a published waiting answer throttles the next. A failed write closes nothing: an
    /// "exiting" decision restarts the hold and the mod does not quit. Returns true only when the mod may quit now.
    /// </summary>
    public bool Published(Decision decision, bool ok, System.DateTimeOffset now)
    {
        if (!ok)
        {
            this.safeSince = null;
            this.retryAfter = now.AddSeconds(WaitingEverySeconds);
            return false;
        }
        this.retryAfter = null;
        if (decision.State != Waiting)
            this.answered.Add(decision.For.RequestId);
        return decision.Exit;
    }

    /// <summary>
    /// Review M4 E2: after "exiting" is published and before the quit, the request is judged again: it must still be on
    /// disk unchanged, before its deadline, and the mod must still own its game. Null = quit now; else the reason it died
    /// (the mod then publishes <see cref="AbortedAnswer"/> and does not quit).
    /// </summary>
    public static string? DiedDuringPublish(Request decided, Request? onDisk, bool stillOwned, System.DateTimeOffset now,
        bool ownerActive = false) =>
        ownerActive ? "owner_active" : onDisk != decided ? "request_changed" : now >= decided.Deadline ? "deadline_passed"
        : !stillOwned ? "not_owned" : null;

    /// <summary>Review M4 E2: the answer that replaces a published "exiting" whose request died before the quit.</summary>
    public Dictionary<string, object> AbortedAnswer(Request r, string why, System.DateTimeOffset now, string gameDate, int gameTime)
    {
        this.answered.Add(r.RequestId);
        this.safeSince = null;
        return Answer(r, Aborted, now, why, gameDate, gameTime);
    }

    /// <summary>W1b E2: the re-read at the decision found another request, or none: no exit, and the hold starts again.</summary>
    public void Abandon() => this.safeSince = null;

    Decision? NotYet(Request request, string why, System.DateTimeOffset now, string gameDate, int gameTime, bool keepHold = false)
    {
        if (!keepHold) this.safeSince = null;
        if ((now - this.lastWaiting).TotalSeconds < WaitingEverySeconds && now >= this.lastWaiting) return null;
        this.lastWaiting = now;
        return new Decision(Answer(request, Waiting, now, why, gameDate, gameTime), false, request);
    }

    Decision Terminal(Request request, string state, System.DateTimeOffset now, string? why, string gameDate, int gameTime)
    {
        this.safeSince = null;
        return new Decision(Answer(request, state, now, why, gameDate, gameTime), false, request);
    }

    static Dictionary<string, object> Answer(Request r, string state, System.DateTimeOffset now, string? why, string gameDate, int gameTime)
    {
        var a = new Dictionary<string, object>(StringComparer.Ordinal)
        {
            ["v"] = 1, ["request_id"] = r.RequestId, ["run_id"] = r.RunId, ["runtime_id"] = r.RuntimeId, ["state"] = state,
            ["game_date"] = gameDate, ["game_time"] = gameTime, ["utc"] = now.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ss.fffZ"),
        };
        if (why is not null) a["why"] = why;
        return a;
    }

    /// <summary>W1b E3: the answer is durable only when it is flushed to disk and reads back byte for byte.</summary>
    public static bool PublishDurably(string folder, Dictionary<string, object> answer)
    {
        try
        {
            string path = Path.Combine(folder, AnswerFile);
            string json = JsonSerializer.Serialize(answer);
            AtomicFileWriter.Write(path, json, flushToDisk: true);
            return AtomicFileWriter.ReadAllText(path) == json;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>The request file as it is now (null = gone or unreadable).</summary>
    public static Request? ReadRequest(string folder)
    {
        try
        {
            string path = Path.Combine(folder, RequestFile);
            return File.Exists(path) ? Parse(AtomicFileWriter.ReadAllText(path)) : null;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>The mod's own read of the requester process: it exists, has not exited, and runs the named image. Any
    /// failure reads as not alive.</summary>
    public static bool ProcessAlive(int pid, string image)
    {
        try
        {
            using var p = System.Diagnostics.Process.GetProcessById(pid);
            if (p.HasExited) return false;
            return string.Equals(p.ProcessName, image, StringComparison.OrdinalIgnoreCase)
                || string.Equals(p.ProcessName + ".exe", image, StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>Contract point 3: the first reason this tick is not safe, in the contract's order (null = safe).
    /// <para>Review M4d item 2: <paramref name="notClean"/> is the same clean predicate that gates every helper action on the
    /// HOST (<c>CleanBlock</c>: a menu, dialogue or prompt open, something held, a player who cannot move), and on the
    /// farmhand its equivalent (a menu of hers open, an item on her cursor, a gift in flight); not clean = not safe.</para></summary>
    public static string? Why(bool saving, bool newDay, bool endOfNight, bool worldReady, bool eventUp, string? committingMenu,
        bool joining, bool handsBusy = false, bool sleepReady = false, string? notClean = null) =>
        saving ? "saving" : newDay ? "new_day" : endOfNight ? "end_of_night" : !worldReady ? "world_not_ready"
        : sleepReady ? "sleep_ready" : eventUp ? "event" : committingMenu is { } m ? "menu:" + m : joining ? "joining"
        : handsBusy ? "hands_busy" : notClean is { } n ? "not_clean:" + n : null;

    /// <summary>
    /// Contract point 3, W1b E2: a menu that commits something, checked by kind (subclasses included: a ReadyCheckDialog is a
    /// ConfirmationDialog) on the whole chain from the active menu through its child menus. The mod passes one
    /// <see cref="MenuKind"/> per menu in the chain; the first committing one names itself (null = none).
    /// </summary>
    public static string? CommittingMenu(IEnumerable<MenuKind> chain)
    {
        foreach (MenuKind m in chain)
            if (m.Confirmation || m.Shipping || m.LevelUp || m.Saving || m.ShopItemHeld)
                return m.TypeName;
        return null;
    }

    /// <summary>One menu of the chain, classified by the mod with <c>is</c> checks (never by its exact type name).</summary>
    public readonly record struct MenuKind(string TypeName, bool Confirmation, bool Shipping, bool LevelUp, bool Saving, bool ShopItemHeld);

    /// <summary>
    /// Review M4e item 3: what her own game can verify about her hands and her open menus. An open menu of hers is no
    /// reason to stay up when the game proves there is nothing to lose (rung six's recovery from a stuck menu, as on main):
    /// <list type="bullet">
    /// <item><c>CursorItem</c>: an item held on her cursor;</item>
    /// <item><c>MenuHoldingItem</c>: a menu on her chain that holds an item in flight (a chest, shop or bundle mid-transfer,
    /// a purchase not yet placed), by its type name;</item>
    /// <item><c>GiftInFlight</c>: a gift proposal of hers still out;</item>
    /// <item><c>TextEntry</c>: a text box being typed into (naming, chat, a sign);</item>
    /// <item><c>Unreadable</c>: any of these could not be read (any doubt: it stays up).</item>
    /// </list>
    /// Her press in flight (<c>handsBusy</c>), a committing menu anywhere on the chain, a save running or queued and her
    /// halt keep their own refusals (<see cref="Why"/>, <see cref="CommittingMenu"/>, <c>Self.Owned</c>).
    /// </summary>
    public readonly record struct HerFacts(bool CursorItem, string? MenuHoldingItem, bool GiftInFlight, bool TextEntry,
        bool Unreadable = false);

    /// <summary>Review M4e item 3: null = nothing of hers to lose (an open menu may then be left by the exit); else why not.</summary>
    public static string? HerBlock(HerFacts f) =>
        f.Unreadable ? "unreadable" : f.CursorItem ? "cursor_item" : f.MenuHoldingItem is { } m ? "menu_holds_item:" + m
        : f.GiftInFlight ? "gift_in_flight" : f.TextEntry ? "text_entry" : null;

    /// <summary>The bound on the menu chain the mod walks (a menu that is its own child cannot loop the walk).</summary>
    public const int MaxMenuChain = 8;
}
