using System.Text.Json;
using System.Text.RegularExpressions;
using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Menus;
using StardewValley.Quests;
using StardewValley.SpecialOrders;
using StardewValley.SpecialOrders.Rewards;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    NativeProgressObserver progressObserver = new();
    readonly Dictionary<string, Quest> progressQuests = new();
    readonly Dictionary<string, SpecialOrder> progressOrders = new();
    Dictionary<string, string[]>? progressMailLinks;
    long progressLastCoverage, progressLastPoll;
    NativeProgressHeartbeat? progressHeartbeat;
    string? progressScope;
    string progressMonitor = Guid.NewGuid().ToString("N");
    ActiveAttempt? progressBracketAttempt;
    string? progressBracketMenu;
    string ProgressMenu => CurrentInteractiveMenu(Game1.activeClickableMenu) switch {
        JunimoNoteMenu => "bundle", MuseumMenu => "museum", _ => "other" };

    void ResetNativeProgress()
    {
        this.progressHeartbeat?.Dispose(); this.progressHeartbeat = null;
        this.progressObserver = new(); this.progressScope = null;
        this.progressBracketAttempt = null; this.progressBracketMenu = null;
        this.progressQuests.Clear(); this.progressOrders.Clear(); this.progressMailLinks = null;
        this.progressMonitor = Guid.NewGuid().ToString("N");
        this.progressLastPoll = 0; this.progressLastCoverage = 0;
    }
    void NativeProgressGap() { this.progressObserver.Gap(); this.progressHeartbeat?.Invalidate(); }
    void ProgressEdge(ActiveAttempt a, bool before)
    {
        try {
        // Reuse the slot's outer consumption bracket for all input paths. Admission alone isn't an edge.
        if (!Context.IsWorldReady || Game1.player is null) return;
        if (before) {
            if (this.ProgressMenu == "other") return; // quests are retained and sampled passively
            this.progressBracketAttempt = a; this.progressBracketMenu = this.ProgressMenu;
            this.PollNativeProgress(force: true);
        } else if (ReferenceEquals(a, this.progressBracketAttempt)) {
            this.PollNativeProgress(force: true, a, inputMenu: this.progressBracketMenu);
            this.progressBracketAttempt = null; this.progressBracketMenu = null;
        }
        } catch { this.NativeProgressGap(); } // Observability never changes execution availability.
    }
    void ObserveNativeProgressInput(Action call)
    {
        var action = this.commandInbox.ExecutionSlot.ActiveAttempt;
        string menu = "other";
        try { menu = this.ProgressMenu; } catch { this.NativeProgressGap(); }
        bool relevant = menu != "other";
        if (relevant) this.PollNativeProgress(force: true);
        try { call(); }
        finally { if (relevant) this.PollNativeProgress(force: true, action, nativeCall: true, inputMenu: menu); }
    }
    void PollNativeProgress(bool force = false, ActiveAttempt? action = null, bool nativeCall = false, string? inputMenu = null)
    {
        if (!Context.IsWorldReady || Game1.player is null || this.occurrenceRing is null) return;
        long now = NowUnixMs();
        // Passive capture is 1 Hz, with synchronous before/after reads around relevant input consumption.
        if (!force && now - this.progressLastPoll < 1000) return;
        try {
            var p = Game1.player;
            string scope = Convert.ToString(Game1.uniqueIDForThisGame, System.Globalization.CultureInfo.InvariantCulture) + ":" + p.UniqueMultiplayerID;
            if (this.progressScope != scope) {
                if (this.progressScope is not null) this.ResetNativeProgress();
                this.progressScope = scope;
            }
            var sample = this.CaptureNativeProgress(p, now);
            var correlation = action is { Client: "inbox" } && (action.Delivered || nativeCall) && action.Request.ExpectedPlayerId == p.UniqueMultiplayerID
                ? new ProgressCorrelation(action.Request.RequestId, p.UniqueMultiplayerID, inputMenu ?? sample.Menu, true) : null;
            foreach (var row in this.progressObserver.Observe(sample, correlation)) {
                this.occurrenceRing.Append("native_progress", (string)row["event_id"]!, row,
                    Game1.ticks, this.commandInbox.ExecutionSlot.ModTick, now);
                this.Monitor.Log("NATIVE_PROGRESS " + JsonSerializer.Serialize(row), LogLevel.Info);
            }
            this.progressLastPoll = now;
            this.progressHeartbeat ??= new(row => this.Monitor.Log("NATIVE_PROGRESS " + JsonSerializer.Serialize(row), LogLevel.Info), TickStallEvents.Probe);
            this.progressHeartbeat.Publish(new(this.progressMonitor, sample.Save, sample.Player, sample.WallMs,
                this.progressObserver.Deposits, this.progressObserver.Donations, this.progressObserver.Letters, this.progressObserver.Ambiguous));
        } catch (Exception ex) {
            // A failed/partial read invalidates zero evidence and rebaselines. Observation never blocks input.
            this.NativeProgressGap();
            if (now - this.progressLastCoverage >= 10000) {
                this.Monitor.Log("Native progress capture unavailable: " + ex.GetType().Name, LogLevel.Warn);
                this.progressLastCoverage = now;
            }
        }
    }
    ProgressSample CaptureNativeProgress(Farmer p, long now)
    {
        var ingredients = new List<ProgressIngredient>();
        var data = this.Helper.Reflection.GetField<StardewValley.Network.NetStringDictionary<string, Netcode.NetString>>(
            Game1.netWorldState.Value, "netBundleData").GetValue();
        if (!data.Pairs.Any()) throw new InvalidDataException("bundle native data unavailable");
        foreach (var pair in data.Pairs) {
            string[] key = pair.Key.Split('/');
            if (key.Length != 2 || !int.TryParse(key[1], out int index)
                || !Game1.netWorldState.Value.Bundles.FieldDict.TryGetValue(index, out var bits) || bits is null) continue;
            var done = Enumerable.Range(0, bits.Length).Select(i => bits[i]).ToArray();
            var bundle = ProgressRules.ParseBundle(pair.Key, pair.Value, done);
            if (bundle is null || done.Length < bundle.Needs.Length) throw new InvalidDataException("bundle native data incomplete");
            for (int i = 0; i < bundle.Needs.Length; ++i) {
                var n = bundle.Needs[i];
                ingredients.Add(new(index.ToString(), i, n.Item == "money" || n.Item.StartsWith('-') ? n.Item
                    : ItemRegistry.QualifyItemId(n.Item) ?? n.Item, n.Quantity, n.Quality, n.Done));
            }
        }
        var pieces = Game1.netWorldState.Value.MuseumPieces.Pairs.Select(x => ItemRegistry.QualifyItemId(x.Value) ?? x.Value).ToHashSet();
        IEnumerable<Item> items = p.Items.OfType<Item>().Concat(new[] { p.CursorSlotItem }.OfType<Item>());
        // These menus temporarily move the actor's bag item onto their own cursor.
        switch (CurrentInteractiveMenu(Game1.activeClickableMenu)) {
            case MuseumMenu m: items = items.Concat(new[] { m.heldItem }.OfType<Item>()); break;
            case JunimoNoteMenu j:
                items = items.Concat(new[] { j.heldItem }.OfType<Item>());
                // Native partialDonationItem is a display aggregate. Its components are the real refundable stacks.
                items = items.Concat(this.Helper.Reflection.GetField<List<Item>>(j, "partialDonationComponents").GetValue().OfType<Item>());
                break;
        }
        var inventory = items.Distinct<Item>(ReferenceEqualityComparer.Instance).GroupBy(i => (Item: i.TypeDefinitionId + i.itemId.Value, i.Quality, i.Category))
            .Select(g => new ProgressStack(g.Key.Item, g.Key.Quality, g.Key.Category, g.Sum(i => i.Stack))).ToArray();
        this.progressMailLinks ??= DataLoader.Mail(Game1.content).SelectMany(pair =>
            Regex.Matches(pair.Value, @"%item\s+quest\s+(\S+)(?:\s|%%)").Cast<Match>()
                .Select(m => (Quest: m.Groups[1].Value, Letter: pair.Key)))
            .GroupBy(x => x.Quest).ToDictionary(g => g.Key, g => g.Select(x => x.Letter).Distinct().ToArray());
        foreach (var q in p.questLog) if (q is not null && q.id.Value is { Length: > 0 } id) this.progressQuests[id] = q;
        var tasks = new List<ProgressTask>();
        // Retain observed quest instances: some native completions remove the log entry in the same update.
        foreach (var pair in this.progressQuests)
            foreach (string letter in this.progressMailLinks.GetValueOrDefault(pair.Key) ?? Array.Empty<string>())
                if (p.mailReceived.Contains(letter)) tasks.Add(new("quest", pair.Key, letter, "mail_quest_attachment",
                    pair.Value.completed.Value, true));
        foreach (var order in p.team.specialOrders) if (order is not null)
            this.progressOrders[order.questKey.Value + ":" + order.generationSeed.Value] = order;
        foreach (var pair in this.progressOrders) {
            var order = pair.Value;
            foreach (var reward in order.rewards.OfType<MailReward>().Where(r => !r.noLetter.Value))
                foreach (string letter in reward.grantedMails)
                    tasks.Add(new("special_order", pair.Key, letter, "special_order_mail_reward",
                        order.questState.Value == SpecialOrderStatus.Complete, order.participants.ContainsKey(p.UniqueMultiplayerID)));
        }
        return new(Convert.ToString(Game1.uniqueIDForThisGame, System.Globalization.CultureInfo.InvariantCulture), p.UniqueMultiplayerID, Game1.Date.TotalDays, now,
            ingredients, pieces, inventory, tasks, this.ProgressMenu);
    }
}
