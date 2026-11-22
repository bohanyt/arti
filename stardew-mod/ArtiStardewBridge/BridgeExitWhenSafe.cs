using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

/// <summary>
/// ADR 256, the farmhand half of "exit when safe". It reads <c>exit_request.json</c> in her command inbox about four times a
/// second and judges every UpdateTicked tick it is called on.
/// <para>Review W1b E2: the request must name this game, and its requester process must be alive:</para>
/// <list type="bullet">
/// <item>her <c>runtime_instance_id</c>;</item>
/// <item>this save's <c>uniqueIDForThisGame</c>;</item>
/// <item>her farmer's id.</item>
/// </list>
/// <para>The bridge has no run of its own, so it echoes the request's <c>run_id</c> without claiming it. Ticks it is not
/// called on (out of the world, paused) restart the safe hold.</para>
/// <para>Review W1b E3: the order is decide, re-read the request, write the answer durably, quit. Her clean-exit record
/// (<c>mod_exit.json</c>) is written by the process-exit hook only (ModEntry), so it exists only when the process really
/// ends. A failed answer write never quits. No lambdas; every game read is a read-allowlisted query or a field.</para>
/// </summary>
internal static class BridgeExitWhenSafe
{
    static readonly ExitWhenSafe contract = new();
    static ExitWhenSafe.Request? cached;
    static int? lastReadTick; // review M4b: no sentinel (int.MinValue made now - last wrap negative, so the first read never came)
    static readonly List<ExitWhenSafe.MenuKind> chain = new();

    /// <summary>W1b E3: set once an "exiting" answer is published; ModEntry does nothing more after it.</summary>
    internal static bool Quitting { get; private set; }

    /// <summary>True when the "exiting" answer is published and the caller must quit, this tick.</summary>
    internal static bool Tick(string? folder, string runtimeId, bool handsBusy, IMonitor monitor, bool halted = false)
    {
        if (folder is null || runtimeId.Length == 0) return false;
        int now = Game1.ticks;
        if (ExitWhenSafe.ReadDue(lastReadTick, now))
        {
            lastReadTick = now;
            cached = ExitWhenSafe.ReadRequest(folder);
        }
        DateTimeOffset utc = DateTimeOffset.UtcNow;
        bool ready = Context.IsWorldReady && Game1.player is not null;
        var self = new ExitWhenSafe.Self(null, runtimeId, ready ? Game1.uniqueIDForThisGame : null,
            ready ? Game1.player!.UniqueMultiplayerID : null, ready && !halted, // review M4 E2: her halt ends a request
            ProcessId: Environment.ProcessId); // review M4d item 4: the request names this process (her recorded launch)
        string? why = null;
        if (cached is not null)
        {
            chain.Clear();
            IClickableMenu? menu = Game1.activeClickableMenu;
            for (int depth = 0; menu is not null && depth < ExitWhenSafe.MaxMenuChain; depth++)
            {
                chain.Add(new ExitWhenSafe.MenuKind(menu.GetType().Name, menu is ConfirmationDialog, menu is ShippingMenu,
                    menu is LevelUpMenu, menu is SaveGameMenu, menu is ShopMenu { heldItem: not null }));
                menu = menu.GetChildMenu();
            }
            // Review M4e item 3: an open menu of hers is left only when her game proves nothing is lost (HerBlock); a save
            // running or queued (anyone ready for sleep included), a committing menu, her press and her halt still refuse.
            why = ExitWhenSafe.Why(Game1.game1.IsSaving, Game1.newDay, Game1.showingEndOfNightStuff, ready,
                Game1.eventUp || Game1.CurrentEvent is not null, ExitWhenSafe.CommittingMenu(chain), joining: false, handsBusy,
                sleepReady: ready && Game1.netReady.GetNumberReady("sleep") > 0, notClean: ready ? HerBlock() : null);
            // Review M4 E4: no bedtime term. A retained prior-night bedtime value held a safe morning to expiry; her own
            // sleep wait is the ReadyCheckDialog on the menu chain, and the night itself is newDay / end_of_night / saving.
        }
        string date = ready ? "total_days:" + Game1.Date.TotalDays : "";
        ExitWhenSafe.Decision? decision = contract.Tick(cached, self, ExitWhenSafe.ProcessAlive, why, utc, date, Game1.timeOfDay);
        if (decision is null) return false;
        if (decision.Exit && ExitWhenSafe.ReadRequest(folder) != decision.For)
        {
            contract.Abandon(); // W1b E2: revoked or replaced since the last read: no exit
            monitor.Log("EXIT_WHEN_SAFE request changed at the decision; not exiting (ADR 256)", LogLevel.Info);
            return false;
        }
        bool ok = ExitWhenSafe.PublishDurably(folder, decision.Answer);
        if (!ok)
            monitor.Log("EXIT_WHEN_SAFE answer write failed (" + decision.State + "); not exiting (ADR 256)", LogLevel.Warn);
        bool quit = contract.Published(decision, ok, utc);
        // Review M4 E2 / M4c rule 6: judged again after the write, as the last thing before the caller's Exit() in this same
        // tick: revoked, replaced, past its deadline, her halt, a stop file written since the poll (stop.json,
        // stop_all.json), the requester gone, or a save begun = no quit, and an "aborted" answer replaces the "exiting" one.
        if (quit && ExitWhenSafe.DiedDuringPublish(decision.For, ExitWhenSafe.ReadRequest(folder),
                !halted && !StopFiles(folder) && Context.IsWorldReady && !Game1.game1.IsSaving && !Game1.newDay
                && !Game1.showingEndOfNightStuff && Game1.netReady.GetNumberReady("sleep") == 0 // review M4e item 3
                && !handsBusy && HerBlock() is null // review M4d item 2: her clean predicate again here
                && ExitWhenSafe.ProcessAlive(decision.For.RequesterPid, decision.For.RequesterImage),
                DateTimeOffset.UtcNow) is { } died)
        {
            ExitWhenSafe.PublishDurably(folder, contract.AbortedAnswer(decision.For, died, DateTimeOffset.UtcNow, date, Game1.timeOfDay));
            monitor.Log("EXIT_WHEN_SAFE request died during the answer write (" + died + "); not exiting (ADR 256)", LogLevel.Warn);
            return false;
        }
        if (quit) Quitting = true; // the caller's Exit() is the next statement (ModEntry logs after it)
        return quit;
    }

    /// <summary>Review M4d item 2 / M4e item 3: the farmhand's clean predicate (null = nothing of hers to lose). An open
    /// menu of hers is allowed only when her game verifies it: no item on her cursor, no menu on the chain holding an item
    /// in flight, no gift of hers out, no text being typed (<see cref="ExitWhenSafe.HerBlock"/>). Unreadable = blocked.
    /// Her press in flight is <c>handsBusy</c>, her halt is <c>halted</c>.</summary>
    static string? HerBlock()
    {
        try
        {
            string? holding = null;
            IClickableMenu? menu = Game1.activeClickableMenu;
            for (int depth = 0; menu is not null && depth < ExitWhenSafe.MaxMenuChain && holding is null; depth++)
            {
                if (HoldsItem(menu)) holding = menu.GetType().Name;
                menu = menu.GetChildMenu();
            }
            return ExitWhenSafe.HerBlock(new ExitWhenSafe.HerFacts(Game1.player.CursorSlotItem is not null, holding,
                Game1.player.team.GetOutgoingProposal() is not null,
                Game1.keyboardDispatcher?.Subscriber is not null || Game1.chatBox?.isActive() == true));
        }
        catch { return ExitWhenSafe.HerBlock(new ExitWhenSafe.HerFacts(false, null, false, false, Unreadable: true)); }
    }

    /// <summary>Review M4e item 3: a menu that holds an item in flight (a chest or inventory transfer, a shop purchase not
    /// yet placed, a bundle slot, a crafted item on the crafting page). Classified with <c>is</c> checks.</summary>
    static bool HoldsItem(IClickableMenu menu) =>
        menu is MenuWithInventory { heldItem: not null } || menu is ShopMenu { heldItem: not null }
        || menu is JunimoNoteMenu { heldItem: not null } || menu is GameMenu game && game.GetCurrentPage() is CraftingPage { heldItem: not null };

    /// <summary>Review M4c rule 6: her halt files, read fresh at the last veto (another process may write them any time).</summary>
    static bool StopFiles(string folder)
    {
        try { return File.Exists(Path.Combine(folder, "stop.json")) || File.Exists(Path.Combine(folder, "stop_all.json")); }
        catch { return true; } // any doubt: stopped
    }
}
