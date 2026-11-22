using ArtiStardewBridge.Protocol;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Quests;
using StardewValley.Locations;

namespace ArtiStardewBridge;

/// <summary>
/// PM-1 fix13c: the actor's progress surface, letters and quest log, read-only, for the knowledge world frame
/// (keys "progress", "mail", "quest_log"). Facts only: the game's own yardsticks (earnings, skills, tools, friendships,
/// Community Center bundles, Grandpa's evaluation) and what letters/quests say. Never chooses, never writes game state,
/// never calls lazy text getters that cache (Quest.questTitle / questDescription / currentObjective): it reads their
/// already-loaded backing fields and falls back to Data/Quests. Shaping rules: Protocol/ProgressFacts.cs.
/// </summary>
internal sealed class NativeProgressFacts
{
    readonly IModHelper helper;
    readonly KnowledgeProjectionCache<Dictionary<string, object>> ccCache = new();
    readonly KnowledgeProjectionCache<Dictionary<string, object>> museumCache = new();
    readonly EventConditionCache eventConditions = new();
    public void Invalidate() { this.ccCache.Invalidate(); this.museumCache.Invalidate(); this.eventConditions.Invalidate(); }
    HashSet<string>? sessionRead;
    readonly List<string> recentRead = new();
    readonly Dictionary<string, Dictionary<string, object>> readPlaces = new();

    public NativeProgressFacts(IModHelper helper) => this.helper = helper;

    /// <summary>Rebuilt at the existing one-second knowledge cadence. Same-count bag swaps, quality,
    /// shared deposits and unlock flags can change without a clock/count change (ADR 49).</summary>
    public Dictionary<string, object> Capture(Farmer p, IEnumerable<GameLocation> locations)
    {
        var result = new Dictionary<string, object>(StringComparer.Ordinal);
        void Try(string key, Func<object> read)
        {
            try { result[key] = read(); }
            catch (Exception ex) { result[key] = new Dictionary<string, object> { ["unknown"] = "capture " + ex.GetType().Name }; }
        }
        Try("progress", () => this.Progress(p, locations.OfType<CommunityCenter>().FirstOrDefault(),
            locations.OfType<LibraryMuseum>().FirstOrDefault()));
        Try("mail", () => this.Mail(p));
        Try("quest_log", () => Quests(p));
        Try("journal_indicator", () => NativeJournalFacts.Activity(p, JournalIndicatorFact.Read(() => p.hasNewQuestActivity())));
        return result;
    }

    Dictionary<string, object> Progress(Farmer p, CommunityCenter? cc, LibraryMuseum? museum)
    {
        var tools = new Dictionary<string, object>(StringComparer.Ordinal);
        foreach (Item item in p.Items)
            if (item is Tool tool && Qualified(tool) is { Length: > 0 } qid && !tools.ContainsKey(qid))
                tools[qid] = new object[] { UiPadTable.Trim(ItemRegistry.GetData(qid)?.DisplayName ?? qid, 40), tool.UpgradeLevel };
        var friends = p.friendshipData.Pairs.Select(pair => (Name: pair.Key, Points: pair.Value?.Points ?? 0))
            .OrderByDescending(f => f.Points).ThenBy(f => f.Name, StringComparer.Ordinal).Take(ProgressRules.FriendsMax)
            .ToDictionary(f => UiPadTable.Trim(f.Name, 30), f => (object)f.Points, StringComparer.Ordinal);
        Stats s = p.stats;
        var progress = new Dictionary<string, object>
        {
            ["money_earned"] = (long)p.totalMoneyEarned, // team total (FarmerTeam), the value Grandpa's rule reads
            ["money_earned_self"] = (long)s.IndividualMoneyEarned,
            ["skills"] = new Dictionary<string, object>
            {
                ["farming"] = p.farmingLevel.Value, ["fishing"] = p.fishingLevel.Value, ["foraging"] = p.foragingLevel.Value,
                ["mining"] = p.miningLevel.Value, ["combat"] = p.combatLevel.Value, ["luck"] = p.luckLevel.Value,
            },
            ["tools"] = tools,
            ["tool_upgrading"] = p.toolBeingUpgraded.Value is { } upgrading ? Qualified(upgrading) : "",
            ["friends"] = friends,
            ["stats"] = new Dictionary<string, object>
            {
                ["crops_shipped"] = (long)s.CropsShipped, ["items_shipped"] = (long)s.ItemsShipped,
                ["fish_caught"] = (long)s.FishCaught, ["quests_completed"] = (long)s.QuestsCompleted,
                ["items_crafted"] = (long)s.ItemsCrafted, ["seeds_sown"] = (long)s.SeedsSown,
                ["items_foraged"] = (long)s.ItemsForaged, ["monsters_killed"] = (long)s.MonstersKilled,
                ["days_played"] = (long)s.DaysPlayed,
            },
            ["house_upgrade"] = p.HouseUpgradeLevel,
            ["deepest_mine_level"] = p.deepestMineLevel,
        };
        progress["cc"] = this.CommunityCenter(p, cc);
        progress["museum"] = museum is null ? new Dictionary<string, object> { ["unknown"] = "native museum unavailable" }
            : this.museumCache.Get(BagKey(p).Concat(
                Game1.netWorldState.Value.MuseumPieces.Pairs.Select(x => x.Key + ":" + x.Value)).ToArray(),
                () => new Dictionary<string, object> {
                ["bag_matches"] = p.Items.Select((item, slot) => (item, slot))
                    .Where(x => x.item is not null && museum.isItemSuitableForDonation(x.item))
                    .Select(x => (object)new object[] { x.slot, Qualified(x.item), UiPadTable.Trim(ItemRegistry.GetData(Qualified(x.item))?.DisplayName ?? Qualified(x.item), 40), x.item.Stack }).ToArray(),
                ["interaction"] = "Gunther donation dialogue opens MuseumMenu; eligibility is not counter readiness",
            });
        progress["grandpa"] = Grandpa(p);
        return progress;
    }

    /// <summary>Shared (team) Community Center state: synchronized bundle data + persistent completion + host room flags.
    /// Reads netBundleData by reflection like ModEntry.Extras (the BundleData getter can initialize the world).</summary>
    Dictionary<string, object> CommunityCenter(Farmer p, CommunityCenter? cc)
    {
        var data = this.helper.Reflection.GetField<StardewValley.Network.NetStringDictionary<string, Netcode.NetString>>(
            Game1.netWorldState.Value, "netBundleData").GetValue().Pairs.ToArray();
        // Read all synchronized values at the knowledge cadence; never key by count or game clock.
        // Separate tokens preserve slot order, stack, quality, identity, shared completion and room progression.
        var keyParts = new List<string>(BagKey(p));
        var rooms = new Dictionary<string, bool?>();
        foreach (var pair in data) {
            keyParts.Add(pair.Key); keyParts.Add(pair.Value);
            string[] key = pair.Key.Split('/');
            if (key.Length == 2 && int.TryParse(key[1], out int index)
                && Game1.netWorldState.Value.Bundles.FieldDict.TryGetValue(index, out var bits) && bits is not null)
                keyParts.Add(string.Concat(Enumerable.Range(0, bits.Length).Select(i => bits[i] ? '1' : '0')));
            else keyParts.Add("unknown");
            if (key.Length > 0 && !rooms.ContainsKey(key[0])) {
                bool? open = null;
                if (cc is not null) try { open = cc.shouldNoteAppearInArea(StardewValley.Locations.CommunityCenter.getAreaNumberFromName(key[0])); } catch { }
                rooms[key[0]] = open; keyParts.Add(open?.ToString() ?? "unknown");
            }
        }
        foreach (string flag in ProgressRules.RoomMail.Concat(new[] { "ccIsComplete", "JojaMember", "ccDoorUnlock" }))
            keyParts.Add(Game1.MasterPlayer.mailReceived.Contains(flag).ToString());
        keyParts.Add(p.hasOrWillReceiveMail("seenJunimoNote").ToString());
        keyParts.Add(p.hasOrWillReceiveMail("canReadJunimoText").ToString());
        var conditions = this.eventConditions.Capture(
            Game1.content.Load<Dictionary<string, string>>("Data/Events/Town").ToArray(),
            Game1.content.Load<Dictionary<string, string>>("Data/Events/WizardHouse").ToArray());
        keyParts.Add(this.eventConditions.Revision.ToString());
        keyParts.Add(conditions is null ? "events_pending" : "events_ready");
        return this.ccCache.Get(keyParts.ToArray(), () => {
            var bundles = new List<ProgressRules.BundleFact>();
            var matches = new List<object>();
            int matchesTotal = 0;
            var bag = p.Items.Select((item, slot) => (item, slot)).Where(x => x.item is not null)
                .Select(x => (x.slot, Id: Qualified(x.item), Stack: x.item.Stack, Quality: x.item.Quality, Category: x.item.Category)).ToArray();
            // Bound the published matches only after prioritizing native-open rooms.
            foreach (var pair in data.OrderByDescending(pair => rooms.GetValueOrDefault(pair.Key.Split('/')[0]) is true))
            {
                string[] key = pair.Key.Split('/');
                bool[]? done = null;
                if (key.Length == 2 && int.TryParse(key[1], out int index)
                    && Game1.netWorldState.Value.Bundles.FieldDict.TryGetValue(index, out var persistent) && persistent is not null)
                {
                    done = new bool[persistent.Length];
                    for (int i = 0; i < done.Length; i++)
                        done[i] = persistent[i];
                }
                if (ProgressRules.ParseBundle(pair.Key, pair.Value, done) is { } bundle)
                {
                    bundles.Add(bundle);
                    // Never infer an empty ingredient from missing synchronized completion data.
                    if (bundle.Complete || done is null || done.Length < bundle.Needs.Length) continue;
                    bool? roomOpen = rooms.GetValueOrDefault(bundle.Room);
                    var neededIds = bundle.Needs.Select(n => ItemRegistry.QualifyItemId(n.Item)).ToArray();
                    foreach (var item in bag)
                        for (int ingredient = 0; ingredient < bundle.Needs.Length; ingredient++)
                        {
                            var need = bundle.Needs[ingredient];
                            if (DonationFacts.Fits(need, item.Stack, item.Quality, item.Category, item.Id,
                                item.Id == neededIds[ingredient]))
                            {
                                ++matchesTotal;
                                if (matches.Count < 48) matches.Add(new Dictionary<string, object> {
                                    ["slot"] = item.slot, ["item"] = item.Id, ["name"] = UiPadTable.Trim(ItemRegistry.GetData(item.Id)?.DisplayName ?? item.Id, 40),
                                    ["stack"] = item.Stack, ["bundle"] = bundle.Index, ["bundle_name"] = bundle.Name,
                                    ["ingredient"] = ingredient, ["quantity"] = need.Quantity, ["quality"] = need.Quality,
                                    ["room"] = bundle.Room,
                                    ["room_open"] = roomOpen is bool open ? (object)open : "unknown",
                                });
                            }
                        }
                }
            }
            var host = Game1.MasterPlayer.mailReceived;
            string Name(string id)
            {
                if (id == "money") return "money";
                if (int.TryParse(id, out int n) && n < 0) return "category " + id;
                return ItemRegistry.GetData(ItemRegistry.QualifyItemId(id) ?? id)?.DisplayName ?? id;
            }
            var rows = bundles.OrderBy(b => b.Index).Take(ProgressRules.BundlesMax).Select(b => {
                var row = b.Wire(Name);
                row["reward_text"] = NativeBundleKnowledge.Reward(b.Reward);
                return (object)row;
            }).ToArray();
            return new Dictionary<string, object>
            {
                ["scope"] = "team (shared world)",
                ["bundles_done"] = bundles.Count(b => b.Complete), ["bundles_total"] = bundles.Count,
                ["rooms_done"] = ProgressRules.RoomMail.Count(host.Contains), ["rooms_total"] = ProgressRules.RoomMail.Length,
                ["rooms"] = ProgressRules.Rooms.Select((room, i) => (object)new object[] { room, host.Contains(ProgressRules.RoomMail[i]) }).ToArray(),
                ["cc_complete"] = host.Contains("ccIsComplete"), ["joja"] = host.Contains("JojaMember"),
                ["unlocked"] = host.Contains("ccDoorUnlock"),
                ["note_seen"] = p.hasOrWillReceiveMail("seenJunimoNote"),
                ["can_read_junimo_text"] = p.hasOrWillReceiveMail("canReadJunimoText"),
                ["unlock_location"] = "Town",
                ["unlock_conditions"] = conditions?.Unlock ?? Array.Empty<object>(),
                ["event_conditions_unknown"] = conditions is null,
                ["translation_location"] = "WizardHouse",
                ["translation_conditions"] = conditions?.Translation ?? Array.Empty<object>(),
                ["bag_matches"] = matches.Take(48).ToArray(), ["bag_matches_total"] = matchesTotal,
                ["eligibility_scope"] = "known persistent empty ingredients, full stack and minimum quality; flavored requirements unknown; room_open is native room progression, not mutex/press admission",
                ["translation"] = "JunimoNoteMenu requires this actor's canReadJunimoText; the unreadable CC note and Wizard sequence precede readable bundles",
                ["bundles"] = rows,
                ["room_rewards"] = NativeBundleKnowledge.Rooms(ProgressRules.Rooms, ProgressRules.RoomMail),
                ["junimo_note"] = NativeBundleKnowledge.Note(p.hasOrWillReceiveMail("canReadJunimoText")),
            };
        });
    }

    static IEnumerable<string> BagKey(Farmer p)
    {
        foreach (var item in p.Items) {
            yield return item is null ? "empty" : Qualified(item);
            if (item is not null) {
                yield return item.Stack.ToString(); yield return item.Quality.ToString(); yield return item.Category.ToString();
            }
        }
    }

    static Dictionary<string, object> Grandpa(Farmer p)
    {
        // The rule reads getHomeOfFarmer(player).upgradeLevel; the actor's own houseUpgradeLevel is the same home's level.
        bool marriedHouse = p.isMarriedOrRoommates() && p.HouseUpgradeLevel >= 2;
        var input = new ProgressRules.GrandpaInput((long)p.totalMoneyEarned, p.achievements.Contains(5), p.hasSkullKey,
            Game1.isLocationAccessible("CommunityCenter"), p.hasCompletedCommunityCenter(), marriedHouse, p.hasRustyKey,
            p.achievements.Contains(26), p.achievements.Contains(34),
            Utility.getNumberOfFriendsWithinThisRange(p, 1975, 999999), p.Level, p.mailReceived.Contains("petLoveMessage"));
        var (points, criteria) = ProgressRules.Grandpa(input);
        int native = Utility.getGrandpaScore(); // the game's own rule on Game1.player (this actor)
        return new Dictionary<string, object>
        {
            ["points"] = native, ["max"] = ProgressRules.GrandpaMax, ["candles"] = ProgressRules.Candles(native),
            ["criteria"] = criteria, ["mirror_agrees"] = points == native,
            ["scope"] = "this actor (Utility.getGrandpaScore reads Game1.player); the evaluation itself is a year-3 event",
        };
    }

    Dictionary<string, object> Mail(Farmer p)
    {
        var text = DataLoader.Mail(Game1.content);
        var read = p.mailReceived.Where(text.ContainsKey).OrderBy(v => v, StringComparer.Ordinal).ToList();
        if (this.sessionRead is null)
            this.sessionRead = read.ToHashSet(StringComparer.Ordinal);
        foreach (string id in read.Where(id => !this.sessionRead.Contains(id) && !this.recentRead.Contains(id))) {
            this.recentRead.Add(id);
            // A read flag alone has no reading place. Only an actually open native letter supplies it.
            if (Game1.activeClickableMenu is StardewValley.Menus.LetterViewerMenu && Game1.currentLocation is { } loc)
                this.readPlaces[id] = new() {
                    ["read_place"] = SceneIdentity.Token(loc.NameOrUniqueName),
                    ["read_place_name"] = NativeWorldKnowledge.Label(loc),
                    ["read_time"] = $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth} {Game1.timeOfDay}"
                };
        }
        while (this.recentRead.Count > ProgressRules.LettersMax) {
            this.readPlaces.Remove(this.recentRead[0]);
            this.recentRead.RemoveAt(0);
        }
        LetterFact Letter(string id, bool isRead)
        {
            var (title, summary, attachment) = ProgressRules.Letter(text.GetValueOrDefault(id), p.displayName ?? p.Name ?? "");
            return new LetterFact(id, title, summary, attachment, isRead);
        }
        Dictionary<string, object> ReadLetter(string id) {
            var row = Letter(id, true).Wire();
            if (this.readPlaces.TryGetValue(id, out var place))
                foreach (var pair in place) row[pair.Key] = pair.Value;
            return row;
        }
        var unread = Game1.mailbox.Where(id => !string.IsNullOrEmpty(id)).Distinct().Take(ProgressRules.LettersMax)
            .Select(id => (object)Letter(id, false).Wire()).ToArray();
        return new Dictionary<string, object>
        {
            ["unread_count"] = Game1.mailbox.Count,
            ["unread"] = unread,
            ["read_count"] = read.Count,
            ["read_ids"] = read.Take(ProgressRules.ReadIdsMax).ToArray(),
            ["read_this_session"] = this.recentRead.Select(id => (object)ReadLetter(id)).ToArray(),
            ["note"] = "unread = still in the mailbox; open the mailbox to read; read_ids = letters already read",
        };
    }

    static Dictionary<string, object> Quests(Farmer p)
    {
        var rows = new List<object>();
        // Current held quests precede completed history in this bounded native frame.
        foreach (var q in p.questLog.OfType<Quest>().Reverse().OrderBy(q => q.completed.Value).Take(ProgressRules.QuestsMax))
        {
            rows.Add(NativeQuestFacts.Read(q, p, name =>
                Game1.getCharacterFromName(name)?.currentLocation?.Name ?? "").Wire());
        }
        return new Dictionary<string, object>
        {
            ["active"] = p.questLog.Count(q => q is not null && !q.completed.Value),
            ["completed_total"] = (long)p.stats.QuestsCompleted,
            ["read_time"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["complete"] = p.questLog.Count(q => q is not null) <= ProgressRules.QuestsMax,
            ["quests"] = rows.ToArray(),
        };
    }

    /// <summary>Item.QualifiedItemId is a cold-getter debt site (il_d_rows D29); the same id from its two fields.</summary>
    static string Qualified(Item item) => item.itemId.Value is { Length: > 0 } id ? item.TypeDefinitionId + id : "";
}
