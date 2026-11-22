using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Locations;
using StardewValley.Tools;
using StardewValley.Menus;
using StardewValley.TerrainFeatures;
using StardewValley.Extensions;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Reflection;
using Map = xTile.Map;

namespace ArtiStardewBridge;

/// <summary>Read-only topology and personal discoveries. Never loads a save, invokes an action, or creates a location.
/// Remote maps supply static topology only; remote objects/NPCs never become authoritative scene targets.</summary>
internal sealed class NativeWorldKnowledge
{
    readonly IModHelper helper;
    DiscoveryLedger ledger = new();
    readonly PossessionBaseline possession = new();
    string ledgerPath = "";
    string boundIdentity = "";
    string stamp = "";
    bool ledgerDirty;
    Dictionary<string, object> graph = new();
    readonly Dictionary<GameLocation, (object Map, string Date, List<(Point Tile, string Action, bool Touch)> Actions)> mapActions = new();
    readonly Dictionary<(GameLocation Location,int X,int Y), (string Signature,string? Reason)> portalCache = new();
    readonly MenuInstanceTokenTracker unknownItems = new();
    readonly string instanceEpoch = Guid.NewGuid().ToString("N");
    readonly Dictionary<GameLocation,Map?> maps = new();
    HashSet<GameLocation> synced = new();
    Dictionary<string,GameLocation> worldLocations = new(StringComparer.Ordinal);
    NativeProgressFacts? progress;
    string calendarDate = "";
    object[] calendar = Array.Empty<object>();
    public NativeWorldKnowledge(IModHelper helper) => this.helper = helper;
    public void InvalidateMaps() { this.progress?.Invalidate(); this.mapActions.Clear(); this.portalCache.Clear(); this.maps.Clear(); }
    // Reuse the existing map-action observation. These are approach hints, never press authorization.
    public IEnumerable<(Point Tile, string Shop)> LocalCounters(GameLocation location) =>
        this.mapActions.TryGetValue(location, out var cached)
            ? cached.Actions.Where(a => !a.Touch).Select(a => (a.Tile, ShopCounterRule.ShopId(ArgUtility.SplitBySpace(a.Action))))
                .Where(a => a.Item2 is not null).Select(a => (a.Tile, a.Item2!))
            : Enumerable.Empty<(Point, string)>();

    void Bind()
    {
        Farmer p = Game1.player;
        string identity = $"{Game1.uniqueIDForThisGame}:{p.UniqueMultiplayerID}";
        if (this.boundIdentity == identity) return;
        string path = "data/discoveries-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant() + ".json";
        this.ledger = (File.Exists(Path.Combine(this.helper.DirectoryPath, path)) ? JsonSerializer.Deserialize<DiscoveryLedger>(AtomicFileWriter.ReadAllText(Path.Combine(this.helper.DirectoryPath, path)), new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) : null) ?? new();
        this.ledger.Events=this.ledger.Events.Select((e,i)=>e with { Seq=i+1 }).ToList();
        this.boundIdentity = identity;
        this.ledgerPath = path; this.stamp = "";
        this.possession.Reset();
        this.progress = null;
        this.calendarDate = "";
    }

    // The same qualified-id read as Item() (TypeDefinitionId + itemId), never the computed QualifiedItemId getter.
    static string? Kind(Item? item)
    {
        if (item is null) return null;
        try { return item.itemId.Value is { Length: > 0 } id ? item.TypeDefinitionId + id : null; }
        catch { return null; }
    }

    static IEnumerable<string> Possessed(Farmer p)
    {
        var kinds = new List<string>();
        foreach (var item in p.Items) if (Kind(item) is { } kind) kinds.Add(kind);
        if (Kind(p.CursorSlotItem) is { } held) kinds.Add(held);
        return kinds;
    }

    string Date {
        get {
            string date = $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}";
            int clock = NativeWorldClock.Now; // authoritative world clock, including client continuations
            this.ledger.ObservationTime = date + " " + clock;
            this.ledger.ObservationPlace = SceneIdentity.Token(Game1.currentLocation.NameOrUniqueName);
            return date;
        }
    }
    void Persist()
    {
        this.ledgerDirty = true;
        string path = Path.Combine(this.helper.DirectoryPath, this.ledgerPath);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var saved = AtomicFileWriter.TryWrite(path, JsonSerializer.Serialize(this.ledger));
        if (!saved.Ok) { System.Diagnostics.Trace.TraceError("FAULT discovery_ledger_write " + saved.Error?.GetType().Name); return; }
        this.ledgerDirty = false;
    }

    public void ObserveTransient()
    {
        this.Bind();
        var p = Game1.player;
        if (!this.possession.Seeded) this.possession.Seed(Possessed(p));
        string date = this.Date, current = SceneIdentity.Token(Game1.currentLocation.NameOrUniqueName);
        bool changed = this.ledger.Observe("location_entered", current, Label(Game1.currentLocation), date);
        if (Game1.currentLocation.currentEvent is { } ev)
            changed |= this.ledger.Observe("story_event", ev.id ?? (date+":unknown-event"),
                string.IsNullOrEmpty(ev.id) ? null : "Game event " + ev.id + " at " + Label(Game1.currentLocation), date);
        if (Game1.farmEvent is { } rare)
            changed |= this.ledger.Observe("rare_event", date+":"+rare.GetType().Name, null, date);
        changed |= this.ObservePeopleShown(date);
        if (changed) this.Persist();
    }

    /// <summary>ADR 230 N2: a person becomes known only when the game shows her that person: a portrait dialogue or
    /// scene line draws the speaker's name on screen, or the social page draws the name. Friendship data never does.</summary>
    bool ObservePeopleShown(string date)
    {
        bool changed = false;
        var menu = Game1.activeClickableMenu;
        // The box draws the speaker's name with the portrait (portrait shown and the player's portrait option on).
        if (menu is DialogueBox { characterDialogue: { showPortrait: true, speaker: NPC npc } } && Game1.options.showPortraits)
            changed |= PersonShown.Record(this.ledger, npc.Name, npc.displayName,
                Game1.eventUp ? PersonShown.Event : PersonShown.Dialogue, date);
        if (menu is GameMenu game && game.GetCurrentPage() is SocialPage)
        {
            // Draw-time text of this menu (ModEntry.GenericUiText.cs); empty when that reader is off, then nothing.
            var drawn = GenericUiRenderedText.Current("menu", menu, exact: true);
            if (drawn.Length > 0)
            {
                // Candidates are the social page's own rows (her friendship entries); each counts only when the page
                // drew that person's display name. The friendship entry alone never does.
                var people = new List<(string, string)>();
                foreach (string name in Game1.player.friendshipData.Keys)
                    if (Game1.getCharacterFromName(name) is { } npc2) people.Add((name, npc2.displayName));
                foreach (var (name, display) in PersonShown.OnSocialPage(drawn, people))
                    changed |= PersonShown.Record(this.ledger, name, display, PersonShown.SocialPage, date);
            }
        }
        return changed;
    }

    public void Obtained(IEnumerable<Item> items)
    {
        this.Bind();
        var entered = items.Where(i => i is not null).ToList();
        // Not yet seeded this binding (no tick observed): what she held before this event is the baseline.
        if (!this.possession.Seeded)
            this.possession.Seed(Possessed(Game1.player).Except(entered.Select(Kind).OfType<string>()));
        bool changed = false;
        foreach (var item in entered)
        {
            // PM-1 fix14e: only a net possession gain is an acquisition; unknown kinds keep the old path.
            if (Kind(item) is { } qid && !this.possession.Gain(qid)) continue;
            changed |= this.Item(item, "item_seen", this.Date);
            changed |= this.Item(item, "item_obtained", this.Date);
        }
        if (changed) this.Persist();
    }

    public Dictionary<string, object> Capture()
    {
        this.Bind();
        this.ObserveTransient();
        Farmer p = Game1.player;
        this.maps.Clear();
        this.synced=this.helper.Multiplayer.GetActiveLocations().ToHashSet();
        this.synced.Add(Game1.currentLocation);
        string date = $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}";
        string mapRevision = date+":"+string.Join("|",Game1.MasterPlayer.mailReceived.OrderBy(v=>v,StringComparer.Ordinal))
            +":"+string.Join("|",p.mailReceived.OrderBy(v=>v,StringComparer.Ordinal))
            +":"+string.Join("|",p.eventsSeen.OrderBy(v=>v,StringComparer.Ordinal));
        // Map references/buildings can change without a clock change, so rebuild at the caller's one-second cadence.
        var locations = new Dictionary<string, GameLocation>(StringComparer.Ordinal);
        Utility.ForEachLocation(l => { locations[l.NameOrUniqueName] = l; return true; }, true, true);
        locations[Game1.currentLocation.NameOrUniqueName] = Game1.currentLocation;
        this.worldLocations=locations;
        foreach (var stale in this.mapActions.Keys.Where(l => !locations.Values.Contains(l)).ToArray())
            this.mapActions.Remove(stale);
        foreach (var stale in this.portalCache.Keys.Where(k=>!locations.Values.Contains(k.Location)).ToArray())
            this.portalCache.Remove(stale);
        var labels = locations.ToDictionary(k => SceneIdentity.Token(k.Key), k => Label(k.Value), StringComparer.Ordinal);
        var facts = new Dictionary<string, string[]>(StringComparer.Ordinal);
        var edges = new List<WorldConnection>();
        var unknown = new List<string>();
        // Uninstantiated locations remain visible. "Default" is an inheritance template, not a place.
        foreach (var pair in DataLoader.Locations(Game1.content))
            if (pair.Key != "Default") labels.TryAdd(SceneIdentity.Token(pair.Key), pair.Value.DisplayName ?? pair.Key);

        foreach (var loc in locations.Values)
        {
            string from = SceneIdentity.Token(loc.NameOrUniqueName);
            Map? map=this.MapFor(loc);
            var data = loc.GetData();
            var meanings = new List<string>();
            if (loc.buildings.OfType<StardewValley.Buildings.ShippingBin>().Any(b => b.daysOfConstructionLeft.Value <= 0))
                meanings.Add("shipping bin: choose items to ship; payment after sleep");
            var usable = NativeUsablePlaces.Read(loc, p);
            if (usable.Fishing) meanings.Add("fishing: reachable stand facing fishable water now; fishing rod required");
            if (data?.Forage?.Count > 0) meanings.Add("forage: " + data.Forage.Count + " game-data entries");
            if (UsePlaceFacts.Soil(usable.Ground && UsePlaceFacts.MapAllowsSeeds(data?.CanPlantHere, loc.IsFarm),
                    usable.Tilled, usable.Planted, usable.Dry) is { } soil)
                meanings.Add(soil);
            facts[from] = (facts.GetValueOrDefault(from) ?? Array.Empty<string>()).Concat(meanings).Distinct().ToArray();
            // These connections are implemented by native location classes rather than map Warp properties.
            // Ticket prices and unlocks are read from this game instance. No travel action is invented for them.
            if (loc is BusStop bus)
            {
                string? gate = !Game1.MasterPlayer.mailReceived.Contains("ccVault") ? "host ccVault: bus repair (Vault bundles/Joja)"
                    : FareLock(bus.TicketPrice,"bus ticket") is { } busFare ? busFare
                    : Game1.netWorldState.Value.canDriveYourselfToday.Value ? null
                    : !this.synced.Contains(loc) ? "unknown: remote bus driver readiness; inspect the bus stop"
                    : !loc.characters.Any(n=>n.Name=="Pam") ? "bus driver Pam is not at the stop"
                    : "unknown: exact native driver readiness; inspect the bus";
                edges.Add(new(from, nameof(Desert), "native:bus", gate, false, Id:"native:"+from+":bus"));
            }
            if (loc is IslandWest)
                edges.Add(new(from,"QiNutRoom","native:walnut-room",
                    IslandWest.IsQiWalnutRoomDoorUnlocked(out int foundWalnuts) ? null
                    : "walnut room: native found count "+foundWalnuts+"/100",false,Id:"native:"+from+":walnut-room"));
            if (loc is BusStop or Mountain or Town)
                MinecartConnections(loc,"Default",edges); // native class checkAction uses this data-defined network
            foreach (var warp in this.synced.Contains(loc) ? loc.warps.ToArray() : Array.Empty<Warp>())
                if (warp is not null && !string.IsNullOrWhiteSpace(warp.TargetName))
                    edges.Add(new(from, SceneIdentity.Token(warp.TargetName), "warp",
                        warp.npcOnly.Value ? "NPC-only connection" : DestinationLock(loc, warp.TargetName, p)
                            ?? PortalLock(loc, warp.X, warp.Y, p), true, !warp.npcOnly.Value,
                            SceneIdentity.WarpFactId(loc.NameOrUniqueName,warp.X,warp.Y,warp.TargetName,warp.TargetX,warp.TargetY)));
            foreach (string property in new[] { "Warp", "NPCWarp" })
                if (map?.Properties.TryGetValue(property, out var raw) == true)
                {
                    string[] values = ArgUtility.SplitBySpace(raw?.ToString() ?? "");
                    for (int i = 0; i + 4 < values.Length; i += 5)
                        if (int.TryParse(values[i], out int x) && int.TryParse(values[i+1], out int y))
                        {
                            string target = values[i+2];
                            bool active = WorldGateRules.RawWarpAvailable(this.synced.Contains(loc),
                                this.synced.Contains(loc) && loc.warps.Any(w => w.X == x && w.Y == y && w.TargetName == target));
                            edges.Add(new(from, SceneIdentity.Token(target), "map:" + property,
                                property == "NPCWarp" ? "NPC-only connection" : DestinationLock(loc, target, p)
                                    ?? PortalLock(loc, x, y, p) ?? (active ? null : "unknown: inactive game map connection"), active, property!="NPCWarp",
                                    "map:"+from+":"+property+":"+x+","+y+"->"+target+":"+values[i+3]+","+values[i+4]));
                        }
                }
            foreach (var pair in this.synced.Contains(loc) ? loc.doors.Pairs.ToArray() : Array.Empty<KeyValuePair<Point,string>>())
            {
                string? action = Property(map,pair.Key.X,pair.Key.Y,"Action","Buildings");
                string to = pair.Value;
                edges.Add(new(from, SceneIdentity.Token(to), "door", DoorLock(loc, p, pair.Key, action, to),
                    this.MotorDoor(loc,p,pair.Key,action),
                    Id:SceneIdentity.DoorFactId(loc.NameOrUniqueName,pair.Key.X,pair.Key.Y,to)));
            }
            foreach (var b in loc.buildings)
            {
                if (b.GetIndoors() is { } inside)
                    edges.Add(new(from, SceneIdentity.Token(inside.NameOrUniqueName), "building_door",
                        b.daysOfConstructionLeft.Value > 0 ? "building under construction: " + b.daysOfConstructionLeft.Value + " days" : null,
                        Id:SceneIdentity.BuildingDoorFactId(loc.NameOrUniqueName,b.tileX.Value+b.humanDoor.X,b.tileY.Value+b.humanDoor.Y,inside.NameOrUniqueName)));
                var buildingData = b.GetData();
                if (buildingData is not null)
                    for (int by=0; by<b.tilesHigh.Value; by++)
                        for (int bx=0; bx<b.tilesWide.Value; bx++)
                            if (buildingData.GetActionAtTile(bx,by) is { } action)
                            {
                                string[] a=ArgUtility.SplitBySpace(action);
                                if (a.Length>0 && ActionDestination(a) is { } target)
                                    edges.Add(new(from,SceneIdentity.Token(target),"building_action:"+a[0],
                                        b.daysOfConstructionLeft.Value>0 ? "building under construction" :
                                        DoorLock(loc,p,new Point(b.tileX.Value+bx,b.tileY.Value+by),action,target),false,
                                        Id:"building_action:"+from+":"+(b.tileX.Value+bx)+","+(b.tileY.Value+by)+":"+target));
                            }
            }
            // GameLocation.doors excludes non-Warp actions. Read the actual map, including inactive/locked connections.
            var layer = map?.GetLayer("Buildings");
            if (layer is null) { unknown.Add(from + ": map unavailable"); continue; }
            if (!this.mapActions.TryGetValue(loc, out var cached) || !ReferenceEquals(cached.Map, map)
                || cached.Date != mapRevision || this.synced.Contains(loc))
            {
                var actions = new List<(Point, string, bool)>();
                foreach (string layerName in new[] { "Buildings", "Back" })
                {
                    var actionLayer=map!.GetLayer(layerName);
                    if (actionLayer is null) continue;
                    bool touch=layerName=="Back";
                    for (int y=0; y<actionLayer.LayerHeight; y++)
                        for (int x=0; x<actionLayer.LayerWidth; x++)
                        {
                            string? action=Property(map,x,y,touch ? "TouchAction" : "Action",layerName);
                            if (!string.IsNullOrWhiteSpace(action)) actions.Add((new Point(x,y),action,touch));
                        }
                }
                this.mapActions[loc] = cached = (map!, mapRevision, actions);
            }
            foreach (var (tile, action, touch) in cached.Actions)
                {
                    string[] a = ArgUtility.SplitBySpace(action);
                    if (!touch && ShopCounterRule.Meaning(a) is { } counterMeaning)
                    {
                        facts[from] = (facts.GetValueOrDefault(from) ?? Array.Empty<string>())
                            .Append(counterMeaning).Distinct().ToArray();
                    }
                    if (!touch && a[0]=="Minecart")
                    {
                        MinecartConnections(loc,a.Length>=2 ? a[1] : "Default",edges);
                        continue;
                    }
                    string? target = touch ? ((a[0] is "Warp" or "MagicWarp") && a.Length>=4 ? a[1] : null) : ActionDestination(a);
                    if (target is null) continue;
                    if (!touch && a[0] == "WarpCommunityCenter")
                    {
                        string destination = SceneIdentity.Token(target);
                        facts[destination] = (facts.GetValueOrDefault(destination) ?? Array.Empty<string>())
                            .Append("entry status now: " + (DoorLock(loc, p, tile, action, target) ?? "open")
                                + "; native WarpCommunityCenter has no opening-hour arguments").Distinct().ToArray();
                    }
                    if (!touch && a[0] == "LockedDoorWarp" && a.Length >= 6
                        && int.TryParse(a[4], out int open) && int.TryParse(a[5], out int close))
                    {
                        if (target == "FishShop" && this.DoorState(loc, p, a).WillyHours) open = 800;
                        string destination = SceneIdentity.Token(target);
                        facts[destination] = (facts.GetValueOrDefault(destination) ?? Array.Empty<string>())
                            .Append("entry hours " + open + "-" + close + "; door " + from + " " + tile.X + "," + tile.Y
                                + "; counter availability depends on the native owner position")
                            .Append("entry status now: " + (DoorLock(loc, p, tile, action, target) ?? "open")
                                + "; entry access is separate from counter readiness").Distinct().ToArray();
                    }
                    string? locked = touch ? (a.Length>=5 && !p.mailReceived.Contains(a[4]) ? "personal mail "+a[4] : null)
                        : DoorLock(loc,p,tile,action,target);
                    edges.Add(new(from, SceneIdentity.Token(target), (touch ? "touch:" : "action:") + a[0],
                        locked, !touch && this.MotorDoor(loc,p,tile,action),
                        Id:(touch ? "touch:" : "action:")+from+":"+tile.X+","+tile.Y+":"+target));
                }
        }
        string current = SceneIdentity.Token(Game1.currentLocation.NameOrUniqueName);
        var rest = NativeRestFacts.Capture(p, locations.Values, Game1.currentLocation, NativeWorldClock.Now);
        var places = WorldGraph.Resolve(current, labels, edges, facts);
        this.graph = new() { ["places"] = places, ["connections"] = edges.Distinct().Select(e =>
            new object?[] { e.From, e.To, e.Kind, e.Locked, e.Travel, e.Player, e.Id }).ToArray(), ["current"] = current,
            ["home"] = rest["home"]!,
            ["complete"] = unknown.Count == 0, ["unknown"] = unknown.ToArray() };

        // Gate transitions are independent of actor movement through a directed graph.
        bool changed = this.ledger.ObserveAccess(places.Select(place => place with {
            ReachableNow = edges.Any(e => e.To == place.Id && e.Locked is null) }), date);
        changed |= this.ledger.ObserveConnections(edges,date);
        changed |= this.ledger.ObserveFlags(p.mailReceived.Select(v=>"self_mail:"+v)
            .Concat(Game1.MasterPlayer.mailReceived.Select(v=>"host_mail:"+v)).Concat(p.eventsSeen.Select(v=>"event:"+v)),date);
        changed |= this.ledger.Observe("location_entered", current,
            Label(Game1.currentLocation) + "; " + string.Join("; ", facts.GetValueOrDefault(current) ?? Array.Empty<string>()), date);
        foreach (var item in p.Items.Where(i => i is not null))
            changed |= this.Item(item, "item_seen", date);
        foreach (var item in Game1.currentLocation.objects.Values.Where(i => i is not null))
            changed |= this.Item(item, "item_seen", date);
        foreach (var debris in Game1.currentLocation.debris)
        {
            if (debris.item is { } item) changed |= this.Item(item, "item_seen", date);
            else if (!string.IsNullOrEmpty(debris.itemId.Value))
            {
                try
                {
                    string qid = ItemRegistry.QualifyItemId(debris.itemId.Value);
                    string? name = ItemRegistry.GetData(qid)?.DisplayName;
                    changed |= this.ledger.Observe("item_seen", qid, name is null ? null : name+": materialized ground item", date);
                }
                catch { changed |= this.ledger.Observe("item_seen",this.UnknownSubject(debris),null,date); }
            }
        }
        if (Game1.activeClickableMenu is ShopMenu shop)
            foreach (var ware in shop.forSale)
                if (ware is Item item) changed |= this.Item(item,"item_seen",date);
                else if (ware is not null) changed |= this.ledger.Observe("item_seen",this.UnknownSubject(ware),null,date);
        if (Game1.activeClickableMenu is ItemGrabMenu grab && grab.ItemsToGrabMenu?.actualInventory is { } contents)
            foreach (var item in contents.Where(i => i is not null)) changed |= this.Item(item, "item_seen", date);
        // ADR 230 N2: friendship data no longer writes npc_met (attempt 85 "Kent: met" with no scene). People she was
        // shown are recorded in ObservePeopleShown; older friendship rows stay in this ledger, never rewritten.
        if (changed || this.ledgerDirty || this.stamp != date)
        {
            this.Persist();
            this.stamp = date;
        }
        var result = new Dictionary<string, object> { ["world_graph"] = this.graph,
            ["visible_tiles"] = new[] { Game1.viewport.X / Game1.tileSize, Game1.viewport.Y / Game1.tileSize,
                (Game1.viewport.X + Game1.viewport.Width - 1) / Game1.tileSize,
                (Game1.viewport.Y + Game1.viewport.Height - 1) / Game1.tileSize },
            // ADR 220: personal save identities only. These flags establish no
            // displayed recipe ingredients, letter body, prices or historic date.
            ["personal_save_state"] = new Dictionary<string, object> {
                ["places_visited"] = p.locationsVisited.OrderBy(v => v, StringComparer.Ordinal).ToArray(),
                ["recipes_known"] = p.craftingRecipes.Keys.Concat(p.cookingRecipes.Keys)
                    .Distinct().OrderBy(v => v, StringComparer.Ordinal).ToArray(),
                ["mail_read"] = p.mailReceived.OrderBy(v => v, StringComparer.Ordinal).ToArray()
            },
            ["discovery_total"] = this.ledger.Events.Count, ["scope"] = this.boundIdentity,
            ["date"] = date, ["clock"] = NativeWorldClock.Now, ["game_facts"] = this.RodFacts(p), ["rest"] = rest };
        try
        {
            if (this.calendarDate != date)
            {
                this.calendar = NativeCalendarFacts.Capture();
                this.calendarDate = date;
            }
            result["calendar"] = this.calendar;
        }
        catch { result["calendar_unknown"] = "native calendar capture unavailable"; }
        // PM-1 fix13c: progress surface, letters and quest log (facts; a failed read stays an explicit unknown).
        this.progress ??= new NativeProgressFacts(this.helper);
        try { foreach (var fact in this.progress.Capture(p, locations.Values)) result[fact.Key] = fact.Value; }
        catch (Exception ex) { result["progress"] = new Dictionary<string, object> { ["unknown"] = "capture " + ex.GetType().Name }; }
        foreach (var discovery in this.ledger.Events)
            result["discovery:" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(discovery.Id))).ToLowerInvariant()] = discovery;
        return result;
    }

    internal static string Label(GameLocation loc)
    {
        if (loc is FarmHouse house)
            return FactNames.Home(house.OwnerId, Game1.player.UniqueMultiplayerID,
                Game1.MasterPlayer.UniqueMultiplayerID, Game1.player.IsMainPlayer);
        string? name = loc.GetData()?.DisplayName;
        return string.IsNullOrWhiteSpace(name) ? "tempat (nama belum diketahui)"
            : StardewValley.TokenizableStrings.TokenParser.ParseText(name);
    }
    GameLocation? Parent(GameLocation loc) => loc.parentLocationName.Value is { } name
        && this.worldLocations.TryGetValue(name,out var parent) ? parent : null;

    static string? FareLock(int price,string label)
    {
        var actor=Game1.player; var team=actor.team;
        int? money=team.useSeparateWallets.Value
            ? team.individualMoney.TryGetValue(actor.UniqueMultiplayerID,out var wallet) ? wallet.Value : null
            : team.money.Value;
        return money is null ? "unknown: personal wallet unavailable"
            : money < price ? label+": "+price+"g; money "+money : null;
    }

    bool Item(Item item, string kind, string date)
    {
        string qid;
        try { qid=item.itemId.Value is { Length: > 0 } id ? item.TypeDefinitionId+id : ""; }
        catch { return this.ledger.Observe(kind,this.UnknownSubject(item),null,date); }
        if (string.IsNullOrWhiteSpace(qid)) return this.ledger.Observe(kind,this.UnknownSubject(item),null,date);
        if (this.ledger.Seen.Contains(kind+":"+qid)) return false;
        string? description;
        try { var data=ItemRegistry.GetData(qid); description = data is null ? null : data.DisplayName + ": " + data.Description; }
        catch { description = null; }
        return this.ledger.Observe(kind, qid, description, date);
    }

    string UnknownSubject(object item) => "unknown:"+item.GetType().Name+":"+this.instanceEpoch+":"+this.unknownItems.Observe(item);

    Map? MapFor(GameLocation loc)
    {
        if (this.maps.TryGetValue(loc,out var map)) return map;
        if (ReferenceEquals(loc,Game1.currentLocation)) map=loc.map;
        else if (!string.IsNullOrWhiteSpace(loc.mapPath.Value))
        {
            try { map=this.helper.GameContent.Load<Map>(loc.mapPath.Value); }
            catch { map=null; }
        }
        return this.maps[loc]=map;
    }

    static xTile.Tiles.Tile? Tile(Map? map,int x,int y,string layerName)
    {
        var layer=map?.GetLayer(layerName);
        return layer is not null && x>=0 && y>=0 && x<layer.LayerWidth && y<layer.LayerHeight ? layer.Tiles[x,y] : null;
    }

    static string? Property(Map? map,int x,int y,string property,string layer)
    {
        var tile=Tile(map,x,y,layer);
        if (tile is null) return null;
        return tile.Properties.TryGetValue(property,out var value) || tile.TileIndexProperties.TryGetValue(property,out value)
            ? value?.ToString() : null;
    }

    static bool Passable(Map map,int x,int y)
    {
        var back=Tile(map,x,y,"Back"); var building=Tile(map,x,y,"Buildings");
        return back?.TileIndexProperties.ContainsKey("Passable")!=true
            && (building is null || building.TileIndexProperties.ContainsKey("Shadow") || building.TileIndexProperties.ContainsKey("Passable"));
    }

    string ContextId(GameLocation loc,HashSet<GameLocation>? visited=null)
    {
        visited??=new();
        if (!visited.Add(loc)) return "Default";
        var map=this.MapFor(loc);
        if (map?.Properties.TryGetValue("LocationContext",out var value)==true && Game1.locationContextData.ContainsKey(value.ToString()))
            return value.ToString();
        return this.Parent(loc) is { } parent ? this.ContextId(parent,visited) : "Default";
    }

    Season SeasonFor(GameLocation loc,HashSet<GameLocation>? visited=null)
    {
        visited??=new();
        if (!visited.Add(loc)) return Game1.season;
        var map=this.MapFor(loc);
        if (map?.Properties.TryGetValue("SeasonOverride",out var value)==true && Enum.TryParse<Season>(value.ToString(),true,out var season)) return season;
        if (Game1.locationContextData.TryGetValue(this.ContextId(loc),out var context) && context.SeasonOverride is { } forced) return forced;
        return this.Parent(loc) is { } parent ? this.SeasonFor(parent,visited) : Game1.season;
    }

    WorldDoorState DoorState(GameLocation loc,Farmer actor,string[] a)
    {
        if (ReferenceEquals(loc,Game1.currentLocation) && loc.map is not null) return NativeB1Offers.DoorState(loc,actor,a);
        string to=a.Length>=4 ? a[3] : "", context=this.ContextId(loc);
        int friendship=a.Length>=7 && actor.friendshipData.TryGetValue(a[6],out var f) ? f.Points : 0;
        bool rain=Game1.netWorldState.Value.LocationWeather.TryGetValue(context,out var weather) && weather.IsGreenRain;
        return new(context=="Default",GameLocation.AreStoresClosedForFestival(),actor.HasTownKey,loc is BeachNightMarket,
            Game1.shortDayNameFromDayOfSeason(Game1.dayOfMonth)=="Wed",to=="SeedShop" && Utility.HasAnyPlayerSeenEvent("191393"),
            actor.mailReceived.Contains("willyHours"),this.SeasonFor(loc)==Season.Winter,
            rain && Game1.year==1 && loc is not Beach && loc is not Forest && to!="AdventureGuild",friendship,
            Game1.MasterPlayer.mailReceived.Contains("ccDoorUnlock") || Game1.MasterPlayer.mailReceived.Contains("JojaMember"));
    }

    bool MotorDoor(GameLocation loc,Farmer actor,Point tile,string? action)
    {
        if (ReferenceEquals(loc,Game1.currentLocation)) return loc.map is not null && NativeB1Offers.ResolveDoor(loc,actor,tile) is not null;
        string[] a=ArgUtility.SplitBySpace(action??"");
        if (a.Length==1 && a[0]=="WarpCommunityCenter") return true;
        if (a.Length<4 || a[3]=="BoatTunnel" || !int.TryParse(a[1],out int x) || x<0 || !int.TryParse(a[2],out int y) || y<0) return false;
        return a[0]=="Warp" && a.Length==4 || a[0]=="LockedDoorWarp" && a.Length is >=6 and <=8;
    }

    void MinecartConnections(GameLocation loc,string networkId,List<WorldConnection> edges)
    {
        if (!DataLoader.Minecarts(Game1.content).TryGetValue(networkId,out var network) || network?.Destinations is null) return;
        string from=SceneIdentity.Token(loc.NameOrUniqueName);
        foreach (var target in network.Destinations)
        {
            if (string.IsNullOrWhiteSpace(target?.TargetLocation)) continue;
            string? gate=Game1.player.mount is not null ? "minecart cannot be used while mounted"
                : ConditionLock(network.UnlockCondition,loc) ?? ConditionLock(target.Condition,loc)
                ?? FareLock(target.Price,"minecart fare");
            edges.Add(new(from,SceneIdentity.Token(target.TargetLocation),"native:minecart",gate,false,
                Id:"minecart:"+from+":"+networkId+":"+target.Id));
        }
    }

    string? ConditionLock(string? condition,GameLocation loc)
    {
        if (string.IsNullOrWhiteSpace(condition)) return null;
        if (System.Text.RegularExpressions.Regex.IsMatch(condition,@"\bRANDOM(?:_[A-Z]+)?\b",
                System.Text.RegularExpressions.RegexOptions.IgnoreCase))
            return "unknown: chance-based game condition "+condition;
        if (!ReferenceEquals(loc,Game1.currentLocation) && System.Text.RegularExpressions.Regex.IsMatch(condition,
                @"\b(?:LOCATION[A-Z_]*|WEATHER[A-Z_]*|[A-Z_]*_HERE)\b",System.Text.RegularExpressions.RegexOptions.IgnoreCase))
            return "unknown: remote location query "+condition;
        // Query evaluation must not consume the game's RNG while observing.
        return GameStateQuery.CheckConditions(condition,Game1.currentLocation,
            Game1.player,random:new Random(0))
            ? null : "game condition: "+condition;
    }

    string? PortalLock(GameLocation loc, int x, int y, Farmer actor)
    {
        Map? map=this.MapFor(loc);
        if (!this.synced.Contains(loc) || loc.resourceClumps.Count == 0 || map?.Layers.Count is not > 0) return null;
        string signature=System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(map)+":"+this.Date+":"
            +string.Join("|",loc.resourceClumps.Select(c=>$"{c.parentSheetIndex.Value}:{c.Tile.X}:{c.Tile.Y}:{c.width.Value}:{c.height.Value}"))
            +":"+string.Join("|",actor.Items.OfType<Tool>().Select(t=>t.GetType().Name+":"+t.UpgradeLevel));
        var cacheKey=(loc,x,y);
        if (this.portalCache.TryGetValue(cacheKey,out var cached) && cached.Signature==signature) return cached.Reason;
        var layer = map.Layers[0];
        var start = (X: Math.Clamp(x, 0, layer.LayerWidth-1), Y: Math.Clamp(y, 0, layer.LayerHeight-1));
        const int radius = 8; // bounded local portal survey, not a game location or tile literal
        var open = new HashSet<(int, int)>(); var outside = new HashSet<(int, int)>();
        var blocked = new Dictionary<(int, int), string>();
        for (int sy = Math.Max(0, start.Y-radius); sy <= Math.Min(layer.LayerHeight-1, start.Y+radius); sy++)
            for (int sx = Math.Max(0, start.X-radius); sx <= Math.Min(layer.LayerWidth-1, start.X+radius); sx++)
            {
                if (!Passable(map,sx,sy)) continue;
                open.Add((sx, sy));
                if (Math.Max(Math.Abs(sx-start.X), Math.Abs(sy-start.Y)) == radius) outside.Add((sx, sy));
                foreach (var clump in loc.resourceClumps)
                    if (clump.getBoundingBox().Contains(sx*64+32, sy*64+32))
                    {
                        var need = WorldGateRules.ResourceTool(clump.parentSheetIndex.Value);
                        int carried = need?.Tool == "Axe" ? actor.Items.OfType<Axe>().Select(t => t.UpgradeLevel).DefaultIfEmpty(-1).Max()
                            : actor.Items.OfType<Pickaxe>().Select(t => t.UpgradeLevel).DefaultIfEmpty(-1).Max();
                        blocked[(sx, sy)] = need is { } requirement
                            ? $"resource clump {clump.parentSheetIndex.Value}: remove with {requirement.Tool} tier >= {requirement.Tier}; carried tier {carried}"
                            : "unknown: resource clump " + clump.parentSheetIndex.Value;
                    }
            }
        string? reason=WorldGateRules.PortalGate(start, open, outside, blocked);
        this.portalCache[cacheKey]=(signature,reason);
        return reason;
    }

    // These are native action semantics, never map tiles or a destination allowlist.
    static string? ActionDestination(string[] a) => a[0] switch {
        "Warp" or "LockedDoorWarp" or "WarpMensLocker" or "WarpWomensLocker" => a.Length >= 4 ? a[3] : null,
        "WarpCommunityCenter" => "CommunityCenter", "WarpGreenhouse" => "Greenhouse",
        "WarpBoatTunnel" => "BoatTunnel", "Warp_Sunroom_Door" => "Sunroom",
        "MagicWarp" => a.Length>=4 ? a[1] : null,
        "WizardHatch" => "WizardHouseBasement", "EnterSewer" => "Sewer", "MasteryRoom" => "MasteryCave",
        "ObeliskWarp" => a.Length>=4 ? a[1] : null, "BoatTicket" => "IslandSouth", "Theater_Entrance" => "MovieTheater",
        _ => null
    };

    string? DestinationLock(GameLocation from, string to, Farmer p)
    {
        if (from is Mountain && (to is "Mine" or "Railroad"))
        {
            string field=to=="Mine" ? "landslide" : "railroadAreaBlocked";
            var native=this.synced.Contains(from)
                ? typeof(Mountain).GetField(field,BindingFlags.NonPublic|BindingFlags.Instance)?.GetValue(from) as Netcode.NetBool : null;
            var days=typeof(StardewValley.Network.NetWorldState).GetField("daysPlayed",BindingFlags.NonPublic|BindingFlags.Instance)
                ?.GetValue(Game1.netWorldState.Value) as Netcode.NetInt;
            string? gate=WorldGateRules.CalendarGate(native?.Value,days?.Value,to=="Mine" ? 5 : 31,
                to=="Mine" && Game1.MasterPlayer.hasOrWillReceiveMail("landslideDone"));
            if (gate is not null) return field+": "+gate;
        }
        if (from is Forest && to=="Woods" && !this.synced.Contains(from))
            return "unknown: Secret Woods entrance log state; inspect Forest (remaining log needs Axe tier 2)";
        if (from is Forest && to == "WizardHouse" && !Forest.isWizardHouseUnlocked())
            return "wizard tower: personal wizardJunimoNote mail or host Joja/bundle progress";
        if (from is Mountain && to == "AdventureGuild" && !p.mailReceived.Contains("guildMember") && !p.hasQuest("16"))
            return "guildMember mail or initiation quest 16";
        if (from is Railroad railroad)
        {
            if (to=="Summit" && !Game1.MasterPlayer.mailReceived.Contains("Farm_Eternal"))
                return "host Farm_Eternal (perfection); summit boulder remains";
            if (to=="WitchWarpCave" && !Game1.MasterPlayer.mailReceived.Contains("witchStatueGone"))
                return !this.synced.Contains(from) ? "unknown: remote witch statue state; Dark Talisman progression"
                    : !railroad.witchStatueGone.Value ? "native witch statue still present; Dark Talisman progression" : null;
        }
        return null;
    }

    string? DoorLock(GameLocation loc, Farmer p, Point tile, string? action, string to)
    {
        string? destinationLock = DestinationLock(loc, to, p);
        if (destinationLock is not null) return destinationLock;
        string[] a = ArgUtility.SplitBySpace(action ?? "");
        if (a.Length == 0) return "unknown: door action unavailable";
        switch (a[0])
        {
            case "Warp": return WorldGateRules.Door(a,NativeWorldClock.Now,WorldDoorState.Empty);
            case "WarpCommunityCenter": return Game1.MasterPlayer.mailReceived.Contains("ccDoorUnlock")
                || Game1.MasterPlayer.mailReceived.Contains("JojaMember") ? null : "host mail ccDoorUnlock or JojaMember";
            case "WarpGreenhouse": return Game1.MasterPlayer.mailReceived.Contains("ccPantry") ? null : "host ccPantry (Pantry bundles/Joja greenhouse)";
            case "Warp_Sunroom_Door": return p.getFriendshipHeartLevelForNPC("Caroline") >= 2 ? null : "Caroline friendship >= 2 hearts";
            case "WizardHatch": return p.getFriendshipHeartLevelForNPC("Wizard") >= 4 ? null : "Wizard friendship >= 4 hearts";
            case "EnterSewer": return p.hasRustyKey || p.mailReceived.Contains("OpenedSewer") ? null : "wallet: Rusty Key";
            case "MasteryRoom": return p.farmingLevel.Value >= 10 && p.fishingLevel.Value >= 10 && p.foragingLevel.Value >= 10
                && p.miningLevel.Value >= 10 && p.combatLevel.Value >= 10 ? null : "personal base level 10 in all five skills";
            case "WarpBoatTunnel": return p.mailReceived.Contains("willyBackRoomInvitation") ? null : "personal mail willyBackRoomInvitation";
            case "WarpMensLocker": return p.IsMale ? null : "men's locker; actor gender gate";
            case "WarpWomensLocker": return !p.IsMale ? null : "women's locker; actor gender gate";
            case "ObeliskWarp": return null;
            case "MagicWarp": return a.Length<5 || p.mailReceived.Contains(a[4]) ? null : "personal mail "+a[4];
            case "BoatTicket": return !Game1.MasterPlayer.hasOrWillReceiveMail("willyBoatFixed")
                ? "host willyBoatFixed: hull, anchor and ticket-machine repair"
                : loc is BoatTunnel boat ? FareLock(boat.TicketPrice,"boat ticket") : "unknown: boat price";
            case "Theater_Entrance":
                if (!Game1.MasterPlayer.hasOrWillReceiveMail("ccMovieTheater")) return "host ccMovieTheater mail (theater restoration)";
                if (Game1.player.team.movieMutex.IsLocked()) return "movie already showing";
                if (Game1.isFestival()) return "festival; theater closed";
                if (NativeWorldClock.Now<900 || NativeWorldClock.Now>2100) return "theater hours 900-2100";
                if (p.lastSeenMovieWeek.Value>=Game1.Date.TotalWeeks) return "personal movie already seen this week";
                return p.Items.Any(i=>i is not null && i.itemId.Value=="809" && i.TypeDefinitionId=="(O)")
                    ? null : "inventory: Movie Ticket";
            case "LockedDoorWarp": return WorldGateRules.Door(a,NativeWorldClock.Now,this.DoorState(loc,p,a));
            default: return "unknown: action " + a[0];
        }
    }

    string[] RodFacts(Farmer p)
    {
        if (p.Items.Any(i => i is FishingRod)) return Array.Empty<string>();
        var events = this.helper.GameContent.Load<Dictionary<string, string>>("Data/Events/Beach");
        var row = events.FirstOrDefault(e => e.Value.Contains("awardFestivalPrize rod", StringComparison.Ordinal));
        if (row.Key is null) return new[] { "first fishing rod: unknown in installed event data" };
        var rods = (DataLoader.Shops(Game1.content).TryGetValue("FishShop", out var shop) ? shop?.Items : null)?
            .Where(item => ItemRegistry.GetData(item.ItemId)?.RawData is StardewValley.GameData.Tools.ToolData { ClassName: "FishingRod" })
            .Select(item => item.ItemId + (string.IsNullOrWhiteSpace(item.Condition) ? "" : " [" + item.Condition + "]")).ToArray();
        bool seen = p.eventsSeen.Contains(row.Key.Split('/')[0]);
        return new[] { "Game Data/Events/Beach: Willy gives Bamboo Pole in personal event " + row.Key
            + "; personal eventsSeen=" + seen
            + "; spring_2_1 read: host=" + Game1.MasterPlayer.mailReceived.Contains("spring_2_1")
            + ", self=" + p.mailReceived.Contains("spring_2_1") + " (*n means host OR self)"
            + ". Event reward/skip targets Game1.player (farmhand included); a full bag gets an item-grant menu. "
            // PM-1 fix11b #2: a16 saw the event, closed the full-bag grant menu and lost the pole; the event runs
            // once per player, so the remaining vanilla source is the FishShop stock.
            + (seen ? "The event does not repeat (eventsSeen); no rod in the bag now. " : "")
            + (rods is { Length: > 0 } ? "FishShop game shop data sells: " + string.Join(", ", rods)
                + " ([condition] = game condition; none = always); price/stock in its menu." : "FishShop rod stock: unknown.") };
    }

    // The graph already indexes this map's properties. Scene capture owns
    // publishing their current effective affordances.
    internal IEnumerable<(Point Tile, string Action, bool Touch)> ActionCells(GameLocation location) =>
        this.mapActions.TryGetValue(location, out var cached) && ReferenceEquals(cached.Map, location.map)
            ? cached.Actions : Array.Empty<(Point, string, bool)>();
}
