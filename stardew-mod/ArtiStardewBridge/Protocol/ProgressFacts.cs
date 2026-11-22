namespace ArtiStardewBridge.Protocol;

// PM-1 fix13c (PROGRESS_GAP_AUDIT_2026-10-29 ranked 4+5, §6/§7): pure, SMAPI-free shaping of the actor's progress
// surface, mailbox letters and quest log. NativeProgressFacts.cs samples game state into these records; the knowledge
// world frame publishes them as facts (never orders). Nothing here writes game state or chooses for ARTI.
//
// Installed game IL (Stardew Valley 1.6.15, read with Mono.Cecil in a local IL probe):
//   Utility.getGrandpaScore(): Game1.player.totalMoneyEarned (FarmerTeam.totalMoneyEarned, team-shared) >= 50k / 100k /
//     200k / 300k / 500k -> +1 each, >= 1M -> +2; achievements.Contains(5) +1; hasSkullKey +1;
//     isLocationAccessible("CommunityCenter") (= Game1.player.eventsSeen 191393, the restoration ceremony) OR
//     hasCompletedCommunityCenter() -> +1, and accessible -> +2 more; isMarriedOrRoommates() AND home upgradeLevel >= 2 +1;
//     hasRustyKey +1; achievements 26 +1; achievements 34 +1; getNumberOfFriendsWithinThisRange(player, 1975, 999999)
//     >= 5 +1, >= 10 +1; Farmer.Level >= 15 +1, >= 25 +1; mailReceived "petLoveMessage" +1. Max 21.
//   Utility.getGrandpaCandlesFromScore: >= 12 -> 4, >= 8 -> 3, >= 4 -> 2, else 1.
//   Bundle(int, string, bool[], ...): data.Split('/'): [0] name, [1] reward, [2] "id qty quality" triplets (completion
//     index = triplet / 3), [3] color, [4] slots (ArgUtility.GetInt default = ingredient count), [5] texture, [6] label;
//     complete = completed ingredients >= slots.
public static class ProgressRules
{
    public const int FriendsMax = 40, LettersMax = 8, ReadIdsMax = 80, QuestsMax = 12, BundlesMax = 40,
        NeedsMax = 12, SummaryMax = 180, TitleMax = 60, ObjectiveMax = 140, DescriptionMax = 180;
    public const int GrandpaMax = 21;
    public static readonly string[] Rooms = { "Pantry", "Crafts Room", "Fish Tank", "Boiler Room", "Vault", "Bulletin Board" };
    /// <summary>Host mail flags set when a room's bundles are all turned in (MasterPlayer, synchronized).</summary>
    public static readonly string[] RoomMail = { "ccPantry", "ccCraftsRoom", "ccFishTank", "ccBoilerRoom", "ccVault", "ccBulletin" };

    public sealed record GrandpaInput(long MoneyEarned, bool MuseumAchievement, bool SkullKey, bool CcAccessible,
        bool CcComplete, bool MarriedHouse2, bool RustyKey, bool MasterAngler, bool FullShipment, int Friends1975,
        int Level, bool PetLove);

    /// <summary>(points, criteria rows [id, points earned, points possible, fact]) mirroring getGrandpaScore.</summary>
    public static (int Points, object[][] Criteria) Grandpa(GrandpaInput g)
    {
        var rows = new List<object[]>();
        void Row(string id, int earned, int possible, string fact) => rows.Add(new object[] { id, earned, possible, fact });
        int money = 0;
        foreach (long step in new long[] { 50000, 100000, 200000, 300000, 500000 })
            money += g.MoneyEarned >= step ? 1 : 0;
        money += g.MoneyEarned >= 1000000 ? 2 : 0;
        Row("earnings", money, 7, "total earned " + g.MoneyEarned + "g (team); steps 50k/100k/200k/300k/500k/1M");
        Row("museum_complete", g.MuseumAchievement ? 1 : 0, 1, "achievement A Complete Collection");
        Row("skull_key", g.SkullKey ? 1 : 0, 1, "Skull Key");
        Row("community_center", (g.CcAccessible || g.CcComplete ? 1 : 0) + (g.CcAccessible ? 2 : 0), 3,
            "Community Center complete +1, restoration ceremony seen +2");
        Row("married_house2", g.MarriedHouse2 ? 1 : 0, 1, "married and house upgraded twice");
        Row("rusty_key", g.RustyKey ? 1 : 0, 1, "Rusty Key");
        Row("master_angler", g.MasterAngler ? 1 : 0, 1, "achievement Master Angler");
        Row("full_shipment", g.FullShipment ? 1 : 0, 1, "achievement Full Shipment");
        Row("friends_8_hearts", (g.Friends1975 >= 5 ? 1 : 0) + (g.Friends1975 >= 10 ? 1 : 0), 2,
            g.Friends1975 + " friends with >= 1975 points; 5 -> +1, 10 -> +1");
        Row("skill_level", (g.Level >= 15 ? 1 : 0) + (g.Level >= 25 ? 1 : 0), 2,
            "farmer level " + g.Level + " (half of all skill levels); 15 -> +1, 25 -> +1");
        Row("pet_love", g.PetLove ? 1 : 0, 1, "pet loves you (petLoveMessage)");
        return (rows.Sum(r => (int)r[1]), rows.ToArray());
    }

    public static int Candles(int points) => points >= 12 ? 4 : points >= 8 ? 3 : points >= 4 ? 2 : 1;

    public sealed record BundleNeed(string Item, int Quantity, int Quality, bool Done);

    public sealed record BundleFact(int Index, string Room, string Name, string Reward, BundleNeed[] Needs, int Slots, int Done)
    {
        public bool Complete => this.Done >= this.Slots;

        public Dictionary<string, object> Wire(Func<string, string>? itemName = null) => new()
        {
            ["id"] = this.Index, ["room"] = UiPadTable.Trim(this.Room, 24), ["name"] = UiPadTable.Trim(this.Name, 40),
            ["done"] = this.Done, ["slots"] = this.Slots, ["complete"] = this.Complete,
            ["reward"] = UiPadTable.Trim(this.Reward, 40),
            // Remaining needs only: [item, quantity, quality]; a completed bundle carries none.
            ["needs"] = this.Complete ? Array.Empty<object>() : this.Needs.Where(n => !n.Done).Take(NeedsMax)
                .Select(n => (object)new object[] { UiPadTable.Trim(itemName?.Invoke(n.Item) ?? n.Item, 40), n.Quantity, n.Quality })
                .ToArray(),
        };
    }

    /// <summary>One Data/Bundles row ("Room/index" -> "name/reward/triplets/color/slots/texture/label") with the
    /// persistent per-ingredient completion array; null when unparseable (never guessed).</summary>
    public static BundleFact? ParseBundle(string key, string value, IReadOnlyList<bool>? completed)
    {
        string[] k = (key ?? "").Split('/');
        if (k.Length != 2 || !int.TryParse(k[1], out int index) || string.IsNullOrEmpty(value))
            return null;
        string[] f = value.Split('/');
        if (f.Length < 3)
            return null;
        string[] t = f[2].Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (t.Length == 0 || t.Length % 3 != 0)
            return null;
        var needs = new List<BundleNeed>();
        for (int i = 0; i < t.Length; i += 3)
        {
            if (!int.TryParse(t[i + 1], out int qty) || !int.TryParse(t[i + 2], out int quality))
                return null;
            bool done = completed is not null && i / 3 < completed.Count && completed[i / 3];
            needs.Add(new BundleNeed(t[i] == "-1" ? "money" : t[i], qty, t[i] == "-1" ? 0 : quality, done));
        }
        int slots = f.Length > 4 && int.TryParse(f[4], out int n) && n > 0 ? n : needs.Count;
        string label = f.Length > 6 && !string.IsNullOrWhiteSpace(f[6]) ? f[6] : f[0];
        return new BundleFact(index, k[0], label, f[1], needs.ToArray(), Math.Min(slots, needs.Count), needs.Count(x => x.Done));
    }

    /// <summary>Data/Mail text as (title, one-line summary): ^ = line break, @ = the reader's name, "%item ...%%" =
    /// an attachment (named, not expanded), "[#]" starts the title. Bounded; the text stays the game's own words.</summary>
    public static (string Title, string Summary, bool Attachment) Letter(string? raw, string readerName)
    {
        if (string.IsNullOrWhiteSpace(raw))
            return ("", "", false);
        string text = raw, title = "";
        int hash = text.IndexOf("[#]", StringComparison.Ordinal);
        if (hash >= 0)
        {
            title = text[(hash + 3)..].Trim();
            text = text[..hash];
        }
        bool attachment = false;
        for (int start = text.IndexOf('%'); start >= 0; start = text.IndexOf('%'))
        {
            int end = text.IndexOf("%%", start + 1, StringComparison.Ordinal);
            if (end < 0)
            {
                text = text[..start];
                break;
            }
            attachment |= text.AsSpan(start).StartsWith("%item", StringComparison.Ordinal);
            text = text[..start] + " " + text[(end + 2)..];
        }
        text = text.Replace("^", " ").Replace("@", readerName);
        text = string.Join(" ", text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        return (UiPadTable.Trim(title, TitleMax), UiPadTable.Trim(text, SummaryMax), attachment);
    }

    /// <summary>Data/Quests description / objective / money reward / reward text / cancelable (fields 2, 3, 6, 7, 8).</summary>
    public static (string Description, string Objective, int Money, string RewardText, bool? Cancelable) QuestRaw(string[]? raw)
    {
        if (raw is null || raw.Length < 4)
            return ("", "", 0, "", null);
        string At(int i) => raw.Length > i ? raw[i] ?? "" : "";
        int money = int.TryParse(At(6), out int m) && m > 0 ? m : 0;
        bool? cancel = bool.TryParse(At(8), out bool c) ? c : null;
        return (At(2), At(3), money, At(7), cancel);
    }
}

/// <summary>ADR 178: the game's HUD activity predicate, independent of quest counts; observation never writes.</summary>
public static class JournalIndicatorFact
{
    public static Dictionary<string, object> Read(Func<bool> nativeActivity)
    {
        object value;
        try { value = nativeActivity(); }
        catch { value = "unknown"; }
        return new() { ["new_activity"] = value, ["source"] = "Farmer.hasNewQuestActivity" };
    }
}

/// <summary>One quest-log entry with what the quest asks and how far it is (knowledge "quest_log").</summary>
public sealed record QuestLogFact(string Id, string Type, string Title, string Description, string Objective, bool Accepted,
    bool Completed, bool Daily, int DaysLeft, int Money, string RewardText, bool? Cancelable, string Target, string Item,
    int? Have, int? Need, string ProgressNote, string ItemName = "", string TargetLocation = "",
    string ProgressKind = "", int? DeadlineDay = null, int? ObservedDay = null, int? ObservedTime = null,
    string SearchLocation = "")
{
    public Dictionary<string, object?> Wire()
    {
        var row = new Dictionary<string, object?>
        {
            ["id"] = UiPadTable.Trim(this.Id, 40), ["type"] = UiPadTable.Trim(this.Type, 24),
            ["title"] = UiPadTable.Trim(this.Title, ProgressRules.TitleMax),
            ["objective"] = UiPadTable.Trim(this.Objective, ProgressRules.ObjectiveMax),
            ["description"] = UiPadTable.Trim(this.Description, ProgressRules.DescriptionMax),
            ["accepted"] = this.Accepted, ["completed"] = this.Completed, ["daily"] = this.Daily,
            ["days_left"] = this.DaysLeft, ["reward_g"] = this.Money,
            ["reward"] = UiPadTable.Trim(this.RewardText, 60), ["cancelable"] = this.Cancelable,
            ["target"] = UiPadTable.Trim(this.Target, 40), ["item"] = UiPadTable.Trim(this.Item, 60),
            ["item_name"] = UiPadTable.Trim(this.ItemName, 60), ["target_location"] = UiPadTable.Trim(this.TargetLocation, 60),
            ["progress_kind"] = UiPadTable.Trim(this.ProgressKind, 24), ["number"] = this.Need,
            ["deadline_day"] = this.DeadlineDay, ["observed_day"] = this.ObservedDay, ["observed_time"] = this.ObservedTime,
            ["search_location"] = UiPadTable.Trim(this.SearchLocation, 60),
        };
        if (this.Have is not null || this.Need is not null)
            row["progress"] = new object?[] { this.Have, this.Need };
        if (this.ProgressNote.Length > 0)
            row["progress_note"] = UiPadTable.Trim(this.ProgressNote, 80);
        return row;
    }
}

/// <summary>One letter (knowledge "mail"): read = in the actor's mailReceived; unread = still in the pending mailbox.</summary>
public sealed record LetterFact(string Id, string Title, string Summary, bool Attachment, bool Read)
{
    public Dictionary<string, object> Wire() => new()
    {
        ["id"] = UiPadTable.Trim(this.Id, 60), ["title"] = this.Title, ["summary"] = this.Summary,
        ["attachment"] = this.Attachment, ["read"] = this.Read,
    };
}
