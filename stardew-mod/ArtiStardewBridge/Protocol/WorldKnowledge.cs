using System.Text.Json.Serialization;

namespace ArtiStardewBridge.Protocol;

// Game-independent graph reduction. Capture/admission remains in the native adapter.
public sealed record WorldConnection(string From, string To, string Kind, string? Locked = null, bool Travel = true,
    bool Player = true, string? Id = null);
public sealed record WorldPlace
{
    [JsonPropertyName("id")] public string Id { get; init; } = "";
    [JsonPropertyName("label")] public string Label { get; init; } = "";
    [JsonPropertyName("reachable_now")] public bool ReachableNow { get; init; }
    [JsonPropertyName("go_to_available")] public bool GoToAvailable { get; init; }
    [JsonPropertyName("motor_route")] public string[] MotorRoute { get; init; } = Array.Empty<string>();
    [JsonPropertyName("route")] public string[] Route { get; init; } = Array.Empty<string>();
    [JsonPropertyName("locked")][JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Locked { get; init; }
    [JsonPropertyName("facts")] public string[] Facts { get; init; } = Array.Empty<string>();
}

public static class WorldGraph
{
    public static IReadOnlyList<WorldPlace> Resolve(string current, IReadOnlyDictionary<string, string> labels,
        IEnumerable<WorldConnection> connections, IReadOnlyDictionary<string, string[]>? facts = null)
    {
        var edges = connections.Distinct().OrderBy(e => e.To, StringComparer.Ordinal).ToArray();
        var adjacent = edges.ToLookup(e => e.From, StringComparer.Ordinal);
        var names = labels.Keys.Concat(edges.SelectMany(e => new[] { e.From, e.To })).Append(current)
            .Distinct(StringComparer.Ordinal).OrderBy(n => n, StringComparer.Ordinal).ToArray();
        // Prefer any open route over a shorter locked route. A second BFS retains a factual path to locked places.
        Dictionary<string, (string[] Route, string? Locked)> Walk(bool allowLocked, bool motor = false)
        {
            var found = new Dictionary<string, (string[], string?)>(StringComparer.Ordinal) { [current] = (Array.Empty<string>(), null) };
            var todo = new Queue<string>(); todo.Enqueue(current);
            while (todo.Count > 0)
            {
                string at = todo.Dequeue();
                foreach (var e in adjacent[at].Where(e => e.Player && (allowLocked || e.Locked is null) && (!motor || e.Travel)))
                    if (!found.ContainsKey(e.To))
                    {
                        var prior = found[at];
                        found[e.To] = (prior.Item1.Append(e.To).ToArray(), prior.Item2 ?? e.Locked);
                        todo.Enqueue(e.To);
                    }
            }
            return found;
        }
        var open = Walk(false); var all = Walk(true); var motor = Walk(false, true);
        return names.Select(id => new WorldPlace {
            Id = id, Label = labels.GetValueOrDefault(id) ?? id, ReachableNow = open.ContainsKey(id),
            GoToAvailable = motor.ContainsKey(id), MotorRoute = motor.TryGetValue(id, out var m) ? m.Route : Array.Empty<string>(),
            Route = open.TryGetValue(id, out var r) ? r.Route : all.TryGetValue(id, out r) ? r.Route : Array.Empty<string>(),
            Locked = open.ContainsKey(id) ? null : all.TryGetValue(id, out r) ? r.Locked
                : edges.Any(e=>e.To==id && !e.Player) ? "NPC-only connections; player route/unlock unknown"
                : "unknown: no player connection in loaded game data",
            Facts = facts?.GetValueOrDefault(id) ?? Array.Empty<string>()
        }).ToArray();
    }
}

public sealed record WorldDoorState(bool Valley, bool Festival, bool TownKey, bool NightMarket, bool Wednesday,
    bool SeedShopEventSeen, bool WillyHours, bool Winter, bool FirstYearGreenRainOverride,
    int FriendshipPoints, bool CommunityCenterOpen)
{
    public static readonly WorldDoorState Empty=new(false,false,false,false,false,false,false,false,false,0,false);
}

public static class WorldGateRules
{
    public static bool RawWarpAvailable(bool sourceSynced,bool runtimePresent) => !sourceSynced || runtimePresent;

    public static string? CalendarGate(bool? observedBlocked,int? syncedDaysPlayed,int threshold,bool clearedMail=false)
    {
        if (observedBlocked.HasValue) return observedBlocked.Value ? "native obstruction remains" : null;
        if (clearedMail) return null;
        if (!syncedDaysPlayed.HasValue) return "unknown: synced world day counter unavailable";
        return syncedDaysPlayed.Value>=threshold ? null : $"synced world DaysPlayed >= {threshold}; now {syncedDaysPlayed}";
    }

    public static string? Door(string[] a, int now, WorldDoorState s)
    {
        if (a.Length == 0) return "unknown: door action unavailable";
        if (a[0] == "WarpCommunityCenter")
            return s.CommunityCenterOpen ? null : "host mail ccDoorUnlock or JojaMember";
        if (a[0] == "Warp") return a.Length>=4 && int.TryParse(a[1],out _) && int.TryParse(a[2],out _)
            && !string.IsNullOrWhiteSpace(a[3]) ? null : "unknown: malformed Warp data";
        if (a[0] != "LockedDoorWarp") return "unknown: action " + a[0];
        if (a.Length < 6 || !int.TryParse(a[4], out int open) || !int.TryParse(a[5], out int close)
            || open < 0 || close <= open || close > 2800) return "unknown: malformed LockedDoorWarp data";
        if (a.Length >= 8 && (!int.TryParse(a[7], out int _) || int.Parse(a[7]) < 0))
            return "unknown: malformed friendship requirement";
        int min = a.Length >= 8 ? int.Parse(a[7]) : 0;
        string to = a[3];
        if (s.Valley && s.Festival) return "festival; stores closed";
        // Vanilla checks Wednesday using possession of the key, before its valley restriction.
        if (to == "SeedShop" && s.Wednesday && !s.SeedShopEventSeen && !s.TownKey)
            return "Wednesday; event 191393 or Key to the Town";
        if (to == "FishShop" && s.WillyHours) open = 800;
        if (s.FirstYearGreenRainOverride) return null;
        bool key = s.TownKey && s.Valley && (!s.NightMarket || to == "FishShop");
        if (!key && (now < open || now >= close)) return $"hours {open}-{close}";
        if (min > 0 && !s.Winter && s.FriendshipPoints < min) return $"friendship {a[6]} >= {min} points; now {s.FriendshipPoints}";
        return null;
    }

    // ResourceClump.performToolAction in the installed game; these are object semantics, never map coordinates.
    public static (string Tool, int Tier)? ResourceTool(int sheet) => sheet switch {
        600 => ("Axe", 1), 602 => ("Axe", 2), 622 => ("Pickaxe", 3), 672 => ("Pickaxe", 2), _ => null
    };

    /// <summary>Only call a clump a portal gate if a path exists without it and disappears with it.
    /// The native capture supplies all geometry; this solver knows no maps.</summary>
    public static string? PortalGate((int X, int Y) source, ISet<(int X, int Y)> open,
        ISet<(int X, int Y)> outside, IReadOnlyDictionary<(int X, int Y), string> clumps)
    {
        bool Reaches(bool block)
        {
            var todo = new Queue<(int X, int Y)>(); var seen = new HashSet<(int, int)>();
            todo.Enqueue(source);
            while (todo.Count > 0)
            {
                var at = todo.Dequeue();
                if (!seen.Add(at) || !open.Contains(at) || block && clumps.ContainsKey(at)) continue;
                if (outside.Contains(at)) return true;
                foreach (var n in new[] { (at.X-1, at.Y), (at.X+1, at.Y), (at.X, at.Y-1), (at.X, at.Y+1) })
                    if (!seen.Contains(n)) todo.Enqueue(n);
            }
            return false;
        }
        if (Reaches(true) || !Reaches(false)) return null;
        var distance = new Dictionary<(int X,int Y),int> { [source]=0 };
        var parent = new Dictionary<(int X,int Y),(int X,int Y)?> { [source]=null };
        var search = new PriorityQueue<(int X,int Y),int>(); search.Enqueue(source,0);
        (int X,int Y)? end=null;
        while (search.TryDequeue(out var at,out int cost))
        {
            if (cost!=distance[at]) continue;
            if (outside.Contains(at)) { end=at; break; }
            foreach (var next in new[] { (X:at.X-1,Y:at.Y),(X:at.X+1,Y:at.Y),(X:at.X,Y:at.Y-1),(X:at.X,Y:at.Y+1) })
            {
                if (!open.Contains(next)) continue;
                int candidate=cost+1+(clumps.ContainsKey(next) ? open.Count+1 : 0);
                if (distance.TryGetValue(next,out int prior) && prior<=candidate) continue;
                distance[next]=candidate; parent[next]=at; search.Enqueue(next,candidate);
            }
        }
        var reasons=new SortedSet<string>(StringComparer.Ordinal);
        while (end is { } tile)
        {
            if (clumps.TryGetValue(tile,out string? reason)) reasons.Add(reason);
            end=parent[tile];
        }
        return reasons.Count>0 ? string.Join("; ",reasons) : "unknown: portal blocked";
    }
}

public sealed record WorldDiscovery
{
    [JsonPropertyName("event_time")] public string? EventTime { get; init; }
    [JsonPropertyName("event_place")] public string? EventPlace { get; init; }
    [JsonPropertyName("seq")] public int Seq { get; init; }
    [JsonPropertyName("id")] public string Id { get; init; } = "";
    [JsonPropertyName("kind")] public string Kind { get; init; } = "unknown";
    [JsonPropertyName("subject")] public string Subject { get; init; } = "unknown";
    [JsonPropertyName("text")] public string Text { get; init; } = "discovery: unknown";
    [JsonPropertyName("source")] public string Source { get; init; } = "game_state";
    [JsonPropertyName("date")] public string Date { get; init; } = "";
}

/// <summary>PM-1 fix14e: an item entering the bag is an acquisition only when she did not already possess that item
/// kind. a22 d2:J1397-1440: the starting Axe and Hoe went slot -> cursor -> slot and surfaced as new item_obtained.
/// Seeded once per binding from what she possesses (bag + cursor); afterwards it grows only from bag entries, so a
/// return from the cursor, a menu-held swap or a chest round trip is never a gain. Item kinds only, no item names.</summary>
public sealed class PossessionBaseline
{
    readonly HashSet<string> possessed = new(StringComparer.Ordinal);
    public bool Seeded { get; private set; }
    public void Seed(IEnumerable<string> current) { this.possessed.Clear(); this.possessed.UnionWith(current); this.Seeded = true; }
    public void Reset() { this.possessed.Clear(); this.Seeded = false; }
    /// <summary>True only for the first entry of an item kind she did not possess.</summary>
    public bool Gain(string qualifiedItemId) => this.possessed.Add(qualifiedItemId);
}

/// <summary>ADR 230 N2: a person is met only when the game showed her that person. Friendship data alone never
/// is. Each way the game shows a person has its own row (id per basis), its source and the date; a row written
/// before ADR 230 from friendship data (source game_state) is never rewritten.</summary>
public static class PersonShown
{
    public const string Dialogue = "dialogue", Event = "event", SocialPage = "social_page";
    public static string Source(string basis) => "native_" + basis;
    public static string Id(string internalName, string basis) => "npc_met:" + internalName + ":" + basis;
    public static string Text(string displayName, string basis) => displayName + ": met (" + basis switch {
        Dialogue => "dialogue line with the name on screen", Event => "scene line with the name on screen",
        SocialPage => "name shown on the social page", _ => basis } + ")";
    /// <summary>The names a social page drew, matched exactly against the people's own displayed names.</summary>
    public static IEnumerable<(string Name, string Display)> OnSocialPage(IEnumerable<string> drawn,
        IEnumerable<(string Name, string Display)> people)
    {
        var lines = new HashSet<string>(drawn.Select(l => l.Trim()).Where(l => l.Length > 0), StringComparer.Ordinal);
        return people.Where(p => !string.IsNullOrWhiteSpace(p.Display) && lines.Contains(p.Display.Trim()))
            .DistinctBy(p => p.Name, StringComparer.Ordinal).ToArray();
    }
    public static bool Record(DiscoveryLedger ledger, string internalName, string displayName, string basis, string date) =>
        !string.IsNullOrWhiteSpace(internalName) && !string.IsNullOrWhiteSpace(displayName)
        && ledger.Observe("npc_met", internalName, Text(displayName, basis), date, Source(basis), Id(internalName, basis));
}

// Persisted observer ledger, never a game-state edit. Events are replayed cyclically until the controller sees them.
public sealed class DiscoveryLedger
{
    public HashSet<string> Seen { get; set; } = new(StringComparer.Ordinal);
    public Dictionary<string, bool> Accessible { get; set; } = new(StringComparer.Ordinal);
    public Dictionary<string, bool> Connections { get; set; } = new(StringComparer.Ordinal);
    public List<WorldDiscovery> Events { get; set; } = new();
    public HashSet<string> Flags { get; set; } = new(StringComparer.Ordinal);
    public bool FlagsInitialized { get; set; }

    public string? ObservationTime { get; set; }
    public string? ObservationPlace { get; set; }

    public bool Observe(string kind, string subject, string? text, string date, string source = "game_state",
        string? identity = null)
    {
        string id = identity ?? kind + ":" + subject;
        if (!this.Seen.Add(id)) return false;
        this.Events.Add(new() { Seq=this.Events.Count+1, Id = id, Kind = string.IsNullOrWhiteSpace(text) ? "unknown" : kind,
            Subject = subject, Text = string.IsNullOrWhiteSpace(text) ? "fakta game (nama atau makna belum diketahui)" : text!,
            Date = date, Source = source, EventTime = this.ObservationTime, EventPlace = this.ObservationPlace });
        return true;
    }

    public bool ObserveAccess(IEnumerable<WorldPlace> places, string date)
    {
        bool changed = false;
        bool initialized = this.Accessible.Count > 0;
        foreach (var p in places)
        {
            bool known = this.Accessible.TryGetValue(p.Id, out bool before);
            if ((known && !before || !known && initialized) && p.ReachableNow)
                changed |= this.Observe("location_unlocked", p.Id, p.Label + ": route unlocked", date);
            this.Accessible[p.Id] = p.ReachableNow;
        }
        return changed;
    }

    public bool ObserveFlags(IEnumerable<string> flags, string date)
    {
        bool changed = false;
        foreach (string flag in flags)
            if (this.Flags.Add(flag))
            {
                changed = true;
                if (!this.FlagsInitialized) continue; // cold-start state is not a newly triggered story
                if (flag.StartsWith("event:",StringComparison.Ordinal))
                    this.Observe("story_event",flag[6..],"Game event "+flag[6..]+" recorded in personal eventsSeen",date);
                else this.Observe("story_flag",flag,null,date);
            }
        this.FlagsInitialized = true;
        return changed;
    }

    public bool ObserveConnections(IEnumerable<WorldConnection> connections, string date)
    {
        bool initialized=this.Connections.Count>0, changed=false;
        foreach (var edge in connections)
        {
            if (edge.Id is null) continue;
            bool open=edge.Player && edge.Locked is null;
            bool known=this.Connections.TryGetValue(edge.Id,out bool before);
            if (open && (known && !before || !known && initialized))
                changed|=this.Observe(known ? "connection_unlocked" : "connection_discovered",edge.Id,
                    edge.From+" → "+edge.To+": "+(known ? "connection unlocked" : "new game-data connection"),date);
            this.Connections[edge.Id]=open;
        }
        return changed;
    }
}
