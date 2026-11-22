using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Menus;
using StardewValley.Characters;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    private sealed record EventObjectId(string Value);
    private readonly System.Runtime.CompilerServices.ConditionalWeakTable<object, EventObjectId> eventObjects = new();
    private int nextEventObjectId;
    private Dictionary<string, object>? lastEventAction;
    private long lastEventActionTick;
    private EventEggRun? eggRun;
    private sealed record EventExitPin(LocationRequest? Request, string? Name, bool Structure, GameLocation? Location)
    {
        public static EventExitPin Capture(Event ev) => new(ev.exitLocation, ev.exitLocation?.Name,
            ev.exitLocation?.IsStructure == true, ev.exitLocation?.Location);
        public bool Valid(Event ev) => ReferenceEquals(ev.exitLocation, this.Request) && (this.Request is null
            || this.Request.Name == this.Name && this.Request.IsStructure == this.Structure
                && ReferenceEquals(this.Request.Location, this.Location));
        public bool ReturnsTo(GameLocation location) => this.Request is null
            || ReferenceEquals(this.Location, location) && this.Name == location.NameOrUniqueName;
    }
    private sealed class EventEggRun
    {
        public MenuStepCommandRequest Request;
        public Event Event = null!;
        public GameLocation Location = null!;
        public Farmer Player = null!;
        public Prop Prop = null!;
        public WorldInteractionTarget? ApproachTarget;
        public EventExitPin? ExitPin;
        public int InputLimit = EventControlAction.MaxRoutePresses;
        public string Pin = "";
        public Point Tile, Stand, BeforeTile, Waypoint;
        public IReadOnlyList<(int X, int Y)> Path = null!;
        public int PathIndex, Presses, StartScore, Facing, ValidationIndex, FaultPanics;
        public List<string> CleanupFaults = new();
        public long Started, ExecutionDeadline;
        public bool Pending, Acting, Stopping, Cleanup = true;
        public bool Automatic, Replan;
        public Vector2 Before;
        public string Direction = "";
        public int Ticks;
        public int SegmentEnd = -1;
        public List<Dictionary<string, object>> Trace = new();
    }

    private bool EventActionsReady => this.UiG3On && this.PadPathConfigured && this.config.WorldInteractKindsEnabled
        && this.config.WorldInteractionInputEnabled && this.config.BackgroundToolUiEnabled
        && this.config.MenuStepCommandInboxEnabled && this.config.WorldPressEnabled
        && this.WorldInteractionBackgroundReady && NativeEventControl.Token() is not null
        && Game1.player is { UsingTool: false, ActiveObject: null } p && !p.IsSitting() && !p.isRidingHorse();

    // Session readiness is potential capability, never an executable target.
    // Do not flood the map in the per-frame session publisher. Publication and
    // arm recompute reachability; the final native capture still authorizes A.
    private bool EventApproachReady => this.EventActionsReady && Game1.CurrentEvent is { } ev
        && (!ev.eventSwitched || this.config.UiPadMenusEnabled);

    private (WorldInteractionTarget? Target, BgActionTarget? Action) CaptureEventAction(Point tile, bool route = false, bool inspectOnly = false)
    {
        if (!this.EventActionsReady || Game1.CurrentEvent is not Event ev || Game1.player is not Farmer player
            || player.currentLocation is not GameLocation loc || NativeEventControl.Token() is not { } eventPin)
            return (null, null);
        string? kind = null, shopId = null, rawAction = null;
        string descriptor = "";
        object targetObject = ev;
        NPC? npc = null;
        Prop? egg = null;
        MapSeat? seat = null;
        Vector2? seatPosition = null;
        Vector2 seatStart = player.Position;
        var rect = new Rectangle(tile.X * 64, tile.Y * 64, 64, 64);
        if (!ev.isFestival && !this.config.UiPadMenusEnabled)
        {
            if (!EventControlAction.TargetTile(ev.playerControlSequenceID, tile.X, tile.Y)
                || ev.playerControlTargetTile != tile) return (null, null);
            kind = EventControlAction.Target;
        }
        else
        {
            // Event.checkAction checks objects and tile actions BEFORE actors, eggs and seats.
            // Never let an unreviewed earlier interceptor receive A.
            GameLocation? temporary;
            NPC? hostActor;
            try {
                temporary = this.Helper.Reflection.GetField<GameLocation>(ev, "temporaryLocation").GetValue();
                hostActor = this.Helper.Reflection.GetField<NPC>(ev, "festivalHost").GetValue();
            }
            catch { return (null, null); }
            if (loc.objects.ContainsKey(tile.ToVector2()) || temporary?.objects.ContainsKey(tile.ToVector2()) == true
                || ev.id == "festival_winter8" && loc.getTileIndexAt(tile.X, tile.Y, "Buildings", "fest") is 1009 or 1010 or 1012 or 1013
                || loc.farmers.Any(f => !ReferenceEquals(f, player)
                    && f.GetBoundingBox().Intersects(rect))) return (null, null);
            string? action = loc.doesTileHaveProperty(tile.X, tile.Y, "Action", "Buildings");
            if (this.config.UiPadMenusEnabled && !ev.isFestival && ev.playerControlTargetTile == tile
                && (tile.X > 0 || tile.Y > 0))
            {
                kind = EventControlAction.Interact;
            }
            else if (action is not null)
            {
                string[] parts = ArgUtility.SplitBySpace(action);
                rawAction = action;
                bool isShop = parts.Length == 2 && parts[0] is ("Shop" or "OpenShop")
                    && !string.IsNullOrWhiteSpace(parts[1]);
                if (this.config.UiPadMenusEnabled)
                {
                    kind = isShop ? EventControlAction.Shop : EventControlAction.Interact;
                    // "shop" is the game's alias; its receiver identity is learned on opening.
                    shopId = isShop && parts[1] != "shop" ? parts[1] : null;
                    descriptor = UiKeyTargetToken.Hash(rawAction);
                    targetObject = loc;
                }
                else
                {
                    if (ev.eventSwitched || !isShop) return (null, null);
                    shopId = parts[1] == "shop" ? ev.id switch {
                    "festival_spring13" => "Festival_EggFestival_Pierre",
                    "festival_spring24" => "Festival_FlowerDance_Pierre",
                    "festival_summer11" => "Festival_Luau_Pierre",
                    "festival_summer28" => "Festival_DanceOfTheMoonlightJellies_Pierre",
                    "festival_winter8" => "Festival_FestivalOfIce_TravelingMerchant",
                    "festival_winter25" => "Festival_FeastOfTheWinterStar_Pierre", _ => null } : parts[1];
                if (shopId is null) return (null, null);
                kind = EventControlAction.Shop;
                    descriptor = UiKeyTargetToken.Hash(System.Text.Json.JsonSerializer.Serialize(new { rawAction, shopId }));
                    targetObject = loc;
                }
            }
            else
            {
                NPC[] actors = ev.actors.Where(a => a.GetBoundingBox().Intersects(rect)
                    || a.TilePoint.X == tile.X && (a.TilePoint.Y == tile.Y || a.TilePoint.Y == tile.Y + 1)).ToArray();
                if (actors.Length != 0)
                {
                    if (actors.Length != 1 || actors[0] is Child || actors[0].IsInvisible
                        || (actors[0].TemporaryDialogue ?? (actors[0].IsVillager
                            ? Game1.npcDialogues?.GetValueOrDefault(actors[0].Name) : null)) is not { Count: > 0 }) return (null, null);
                    if (!this.config.UiPadMenusEnabled && (ev.eventSwitched
                        || ev.id is "festival_spring24" or "festival_winter25"
                        || ReferenceEquals(actors[0], hostActor) || actors[0].Name == "Lewis")) return (null, null);
                    npc = actors[0]; targetObject = npc; kind = EventControlAction.Npc;
                }
                else if (ev.id == "festival_spring13" && ev.eventSwitched && ev.playerControlSequenceID == "eggHunt"
                    && ev.festivalTimer > 0)
                {
                    Prop[] props = ev.festivalProps.Where(p => p.isColliding(rect)).ToArray();
                    if (props.Length != 1) return (null, null);
                    egg = props[0]; targetObject = egg; kind = route ? EventControlAction.EggRoute : EventControlAction.Egg;
                }
                else if (ev.isFestival && !ev.eventSwitched)
                {
                    var seats = loc.mapSeats.Where(s => s.OccupiesTile(tile.X, tile.Y)).ToArray();
                    if (seats.Length != 1 || seats[0].IsBlocked(loc)) return (null, null);
                    targetObject = seats[0]; kind = EventControlAction.Seat;
                    seat = seats[0];
                    var rawSeats = seat.GetSeatPositions(ignore_offsets: true);
                    var endSeats = seat.GetSeatPositions();
                    if (rawSeats.Count == 0 || rawSeats.Count > 64 || endSeats.Count != rawSeats.Count) return (null, null);
                    int[] indices = rawSeats.Select((v,i) => (v,i)).Where(v => v.v == tile.ToVector2()).Select(v => v.i).ToArray();
                    if (indices.Length != 1) return (null, null);
                    int seatIndex = indices[0];
                    if (!inspectOnly && (seat.sittingFarmers.Values.Contains(seatIndex)
                        || loc.IsTemporary && loc.farmers.Any(f => !ReferenceEquals(f, player) && f.IsSitting()
                            && Math.Abs(f.mapChairSitPosition.Value.X - rawSeats[seatIndex].X) < 1e-5
                            && Math.Abs(f.mapChairSitPosition.Value.Y - rawSeats[seatIndex].Y) < 1e-5))) return (null, null);
                    descriptor = UiKeyTargetToken.Hash(System.Text.Json.JsonSerializer.Serialize(new {
                        seatIndex, raw = rawSeats.Select(v => new[] { v.X, v.Y }),
                        end = endSeats.Select(v => new[] { v.X, v.Y }) }));
                    // Metadata may name a bench; executable A must select this exact vacant seat.
                    if (!inspectOnly && !route)
                    {
                        seatPosition = PredictFestivalSeat(seat, player, loc, tile);
                        if (seatPosition is null) return (null, null);
                    }
                }
            }
        }
        if (kind is null) return (null, null);
        if (route && kind is not (EventControlAction.EggRoute)) kind += "_route";
        string targetId = this.eventObjects.GetValue(targetObject, _ => new EventObjectId("ei" + ++this.nextEventObjectId)).Value;
        string source = "event:" + kind + ":" + targetId + ":" + tile.X + "," + tile.Y
            + (descriptor.Length == 0 ? "" : ":" + descriptor);
        // startEvent installs a default same-location exit request even for plain
        // `end` (GameLocation.cs:15729-15731). Bind that request, not nullness.
        EventExitPin exitPin = EventExitPin.Capture(ev);
        LocationRequest? exit = exitPin.Request;
        string token = UiKeyTargetToken.Hash(System.Text.Json.JsonSerializer.Serialize(new {
            eventPin, targetId, source, identity = this.CurrentCommandIdentity(),
            exit = exit is null ? null : new { instance = this.eventObjects.GetValue(exit,
                _ => new EventObjectId("ei" + ++this.nextEventObjectId)).Value, exitName = exitPin.Name, exitStructure = exitPin.Structure,
                location = exitPin.Location is null ? null : this.eventObjects.GetValue(exitPin.Location,
                    _ => new EventObjectId("ei" + ++this.nextEventObjectId)).Value },
            actor = new[] { player.TilePoint.X, player.TilePoint.Y }, player.FacingDirection,
            seatStart = kind == EventControlAction.Seat ? new[] { seatStart.X, seatStart.Y } : null,
            seatEnd = seatPosition is { } end ? new[] { end.X * 64, end.Y * 64 } : null }));
        var target = new WorldInteractionTarget(token, source, tile.X, tile.Y, player.TilePoint.X, player.TilePoint.Y,
            player.FacingDirection, kind);
        if (inspectOnly) return (target, null); // metadata only; never an executable wire target
        if (!target.IsValid) return (null, null);
        if (EventControlAction.IsApproach(kind)) return (target, null); // execution recaptures the exact final native pose
        int command = ev.CurrentCommand;
        string? sequence = ev.playerControlSequenceID;
        string raw = NativeEventControl.EffectiveRaw(ev)!;
        string[] script = ev.eventCommands;
        bool festival = ev.isFestival, main = ev.eventSwitched, timer = ev.festivalTimer > 0;
        string eventId = ev.id;
        CommandInboxIdentity identityPin = this.CurrentCommandIdentity();
        bool ActorBinding()
        {
            CommandInboxIdentity current = this.CurrentCommandIdentity();
            return current == identityPin with { ContextGeneration = current.ContextGeneration }
                && current.ContextGeneration >= identityPin.ContextGeneration
                && current.ContextGeneration - identityPin.ContextGeneration <= 1;
        }
        IClickableMenu? observedReceiver = null;
        EventActionFacts? observedEffect = null;
        bool NativeFinishedEnd()
        {
            // Plain end calls exitEvent: finished=true, fadeToBlack/eventOver=true,
            // CurrentCommand += 2 (Event.cs:4744-4775). This is release-only scope,
            // available solely after the exact second-target H effect was observed.
            if (kind != EventControlAction.Target || sequence != "haleyBeach2" || observedEffect is null
                || command + 1 >= script.Length || script[command + 1] != "end"
                || ev.CurrentCommand != command + 3 || ev.playerControlSequence || ev.playerControlSequenceID is not null
                || !exitPin.Valid(ev) || !exitPin.ReturnsTo(loc)) return false;
            try { return this.Helper.Reflection.GetField<bool>(ev, "eventFinished").GetValue(); }
            catch { return false; }
        }
        bool NativeEnd() => NativeFinishedEnd() && Game1.eventOver && Game1.fadeToBlack;
        bool SeatGeometryValid()
        {
            if (seat is null || !loc.mapSeats.Contains(seat) || seat.IsBlocked(loc)) return false;
            var rawSeats = seat.GetSeatPositions(ignore_offsets: true);
            var endSeats = seat.GetSeatPositions();
            if (rawSeats.Count == 0 || rawSeats.Count > 64 || endSeats.Count != rawSeats.Count) return false;
            int[] indices = rawSeats.Select((v,i) => (v,i)).Where(v => v.v == tile.ToVector2()).Select(v => v.i).ToArray();
            return indices.Length == 1 && descriptor == UiKeyTargetToken.Hash(System.Text.Json.JsonSerializer.Serialize(new {
                seatIndex = indices[0], raw = rawSeats.Select(v => new[] { v.X, v.Y }), end = endSeats.Select(v => new[] { v.X, v.Y }) }));
        }
        bool Identity() => ActorBinding() && Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
            && ReferenceEquals(Game1.player, player) && ReferenceEquals(Game1.currentLocation, loc)
            && ReferenceEquals(player.currentLocation, loc) && ReferenceEquals(Game1.CurrentEvent, ev)
            && ReferenceEquals(loc.currentEvent, ev) && ReferenceEquals(ev.eventCommands, script)
            && exitPin.Valid(ev) && (sequence != "haleyBeach2" || ev.playerControlTargetTile == tile)
            && script[Math.Min(command, script.Length - 1)] == raw
            && (rawAction is null || loc.doesTileHaveProperty(tile.X, tile.Y, "Action", "Buildings") == rawAction)
            && (seat is null || SeatGeometryValid())
            && Game1.eventUp && ev.id == eventId && ev.isFestival == festival && ev.eventSwitched == main
            && (ev.festivalTimer > 0) == timer && Game1.currentMinigame is null && Game1.farmEvent is null
            && !Game1.paused && !Game1.HostPaused && !Game1.globalFade && (!Game1.fadeToBlack || NativeEnd())
            && !Game1.freezeControls && !Game1.panMode && Game1.locationRequest is null;
        bool EndedAfterEffect() => kind == EventControlAction.Target && sequence == "haleyBeach2"
            && observedEffect is not null && ActorBinding() && Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
            && ReferenceEquals(Game1.player, player) && ReferenceEquals(Game1.currentLocation, loc)
            && ReferenceEquals(player.currentLocation, loc) && !Game1.eventUp && Game1.CurrentEvent is null && loc.currentEvent is null
            && ReferenceEquals(ev.eventCommands, script) && script[Math.Min(command, script.Length - 1)] == raw
            && exitPin.Valid(ev) && ev.playerControlTargetTile == tile
            && ev.id == eventId && ev.isFestival == festival && ev.eventSwitched == main
            && (ev.festivalTimer > 0) == timer && !ev.playerControlSequence && ev.playerControlSequenceID is null
            && !Game1.paused && !Game1.HostPaused && !Game1.freezeControls && !Game1.panMode
            && Game1.currentMinigame is null && Game1.activeClickableMenu is null && !Game1.dialogueUp
            && !Game1.globalFade && (!Game1.fadeToBlack || NativeFinishedEnd())
            && !Game1.eventOver && Game1.locationRequest is null;
        bool Receiver(object? menu)
        {
            bool expected = kind == EventControlAction.Npc && menu is DialogueBox box
                && ReferenceEquals(box.characterDialogue?.speaker, npc)
                || kind == EventControlAction.Shop && menu is ShopMenu shop
                    && (shopId is null || shop.ShopId == shopId || shop.ShopId == ev.id + "_" + shopId)
                || kind == EventControlAction.Interact && menu is IClickableMenu;
            if (expected && observedReceiver is null) observedReceiver = (IClickableMenu)menu!;
            return expected && ReferenceEquals(menu, observedReceiver);
        }
        EventActionFacts Read() => new(kind, targetId, Identity(), ev.playerControlSequenceID, ev.playerControlSequence,
            ev.CurrentCommand, ev.playerControlTargetTile.X, ev.playerControlTargetTile.Y, ev.festivalTimer > 0,
            player.festivalScore, egg is null || ev.festivalProps.Any(p => ReferenceEquals(p, egg)),
            Receiver(Game1.activeClickableMenu), Game1.activeClickableMenu?.GetType().Name, player.IsSitting());
        EventActionFacts baseline = Read();
        bool Scope(bool after)
        {
            if (!Identity()) return after && EndedAfterEffect();
            if (after && NativeEnd()) return Game1.activeClickableMenu is null && !Game1.dialogueUp;
            if (after && kind == EventControlAction.Interact)
                return EventControlAction.Effect(baseline, Read());
            if (after && kind == EventControlAction.Target)
                return EventControlAction.Effect(baseline, Read())
                    || sequence == "haleyBeach2" && observedEffect is not null
                        && !ev.playerControlSequence && ev.playerControlSequenceID is null
                        && ev.CurrentCommand >= command + 1 && ev.CurrentCommand <= script.Length
                        && Game1.activeClickableMenu is null && !Game1.dialogueUp;
            bool strict = ev.CurrentCommand == command && ev.playerControlSequenceID == sequence
                && ev.playerControlSequence && NativeEventControl.EffectiveRaw(ev) == raw;
            return strict && (after ? Game1.activeClickableMenu is null || Receiver(Game1.activeClickableMenu)
                : NativeEventControl.Token() == eventPin
                    && (kind is not (EventControlAction.Egg or EventControlAction.EggRoute) || player.festivalScore == baseline.Score));
        }
        bool Present() => Scope(false) && player.ActiveObject is null && !player.UsingTool
            && (route || player.GetGrabTile() == tile.ToVector2())
            && this.CaptureEventAction(tile, route).Target == target;
        FestivalSeatMotion? seatMotion = seatPosition is { } at
            ? new(seatStart.X, seatStart.Y, at.X * 64, at.Y * 64) : null;
        bool SeatBinding() => seat is not null && seatPosition is { } at && Scope(true)
            && loc.mapSeats.Contains(seat) && !seat.IsBlocked(loc)
            && ReferenceEquals(player.sittingFurniture, seat) && seat.IsSittingHere(player)
            && seat.GetSittingPosition(player) == at
            && seat.GetSittingPosition(player, ignore_offsets: true) == tile.ToVector2()
            && player.mapChairSitPosition.Value == tile.ToVector2();
        BgActionEffectFacts Sample()
        {
            EventActionFacts now = Read();
            if (EventControlAction.Effect(baseline, now)) observedEffect ??= now;
            // A target sequence can advance again while the verified release settles. Keep the exact
            // first native effect, but never keep it across instance/location/phase drift.
            EventActionFacts effect = kind == EventControlAction.Target && sequence == "haleyBeach2"
                && Scope(true) && observedEffect is not null
                ? observedEffect : now with { ScopeValid = false };
            if (kind != EventControlAction.Target || sequence != "haleyBeach2" || observedEffect is null)
                effect = now with { ScopeValid = Scope(false) || Scope(true) };
            if (kind == EventControlAction.Seat && now.Sitting)
                effect = effect with { ScopeValid = SeatBinding() && seatMotion?.Settled == true };
            return SampleInteractEffect(() => egg is null || ev.festivalProps.Contains(egg), "")
                with { Event = effect, ExpectedReceiver = Receiver(Game1.activeClickableMenu) };
        }
        var pinned = new BgActionTarget(WorldInteractionKinds.ToActionKind(kind), null, null, source, Present, Sample)
        { EventScopeValid = Scope, ExpectedReceiver = Receiver,
            SeatMotion = seatMotion,
            SampleSeatMotion = seatMotion is null ? null : () => new FestivalSeatMotionFacts(
                SeatBinding(), player.IsSitting(), player.CanMove, player.freezePause,
                player.movementDirections.Count, player.Position.X, player.Position.Y),
            PoseMayChangeAfterPress = kind == EventControlAction.Seat || kind == EventControlAction.Target && sequence == "haleyBeach2" };
        return (target, pinned);
    }

    // Read-only mirror of MapSeat.AddSittingFarmer / CheckSeatOccupancyIfTemporaryMap
    // (MapSeat.cs:317-369,410-473). No AddSittingFarmer call or native field writes.
    private static Vector2? PredictFestivalSeat(MapSeat seat, Farmer player, GameLocation loc, Point tile, Vector2? plannedStanding = null)
    {
        var positions = seat.GetSeatPositions();
        var raw = seat.GetSeatPositions(ignore_offsets: true);
        if (positions.Count == 0 || positions.Count > 64 || raw.Count != positions.Count) return null;
        int selected = -1;
        float distance = 96;
        Vector2 standing = plannedStanding ?? player.getStandingPosition();
        for (int i = 0; i < positions.Count; i++)
        {
            if (seat.sittingFarmers.Values.Contains(i) || loc.IsTemporary && loc.farmers.Count > 1
                && loc.farmers.Any(f => !ReferenceEquals(f, player) && f.IsSitting()
                    && Math.Abs(f.mapChairSitPosition.Value.X - raw[i].X) < 1e-5
                    && Math.Abs(f.mapChairSitPosition.Value.Y - raw[i].Y) < 1e-5)) continue;
            Vector2 delta = (positions[i] + new Vector2(.5f, .5f)) * 64 - standing;
            float d = delta.Length();
            if (d < distance) { distance = d; selected = i; }
        }
        if (selected < 0 || raw[selected] != tile.ToVector2()) return null;
        Vector2 end = positions[selected] * 64;
        Vector2 start = player.Position + standing - player.getStandingPosition();
        return new FestivalSeatMotion(start.X, start.Y, end.X, end.Y).GeometryValid
            ? positions[selected] : null;
    }

    private void EnrichEventActionContext(Dictionary<string, object> snapshot)
    {
        if (this.lastEventAction is not null && Game1.ticks - this.lastEventActionTick <= 180)
            snapshot["event_action_receipt"] = this.lastEventAction;
        if (!this.EventActionsReady || Game1.CurrentEvent is not Event ev) return;
        List<Dictionary<string, object>> targets = new();
        Point grab = Game1.player.GetGrabTile().ToPoint();
        var faced = this.CaptureEventAction(grab);
        if (faced.Target is { } target) targets.Add(target.Args());
        if (ev.id == "festival_spring13" && ev.playerControlSequenceID == "eggHunt" && ev.festivalTimer > 0)
        {
            foreach (var candidate in this.EventEggCandidates())
            {
                var capture = this.CaptureEventAction(candidate.Tile, route: true);
                if (capture.Target is { } eggTarget) targets.Add(eggTarget.Args());
            }
        }
        var routes = this.EventApproachCandidates();
        foreach (var candidate in routes) targets.Add(candidate.Target.Args());
        string? hostName = null;
        try
        {
            NPC? host = this.Helper.Reflection.GetField<NPC>(ev, "festivalHost").GetValue();
            if (host is not null)
                hostName = string.IsNullOrWhiteSpace(host.displayName) ? host.Name : host.displayName;
        }
        catch { }
        snapshot["event_actions"] = new Dictionary<string, object> {
            ["event_token"] = NativeEventControl.Token()!, ["sequence"] = ev.playerControlSequenceID,
            ["target_tile"] = new[] { ev.playerControlTargetTile.X, ev.playerControlTargetTile.Y },
            ["festival_id"] = ev.id, ["main_event_running"] = ev.eventSwitched,
            ["timer_positive"] = ev.festivalTimer > 0, ["timer_ms"] = ev.festivalTimer,
            ["festival_score"] = Game1.player.festivalScore, ["targets"] = targets,
            ["route_inputs"] = routes.ToDictionary(c => c.Target.SourceFactId,
                c => EventControlAction.RouteBudget(c.Path, (c.Target.ActorTileX,c.Target.ActorTileY))),
            ["route_labels"] = routes.ToDictionary(c => c.Target.SourceFactId, c => c.Target.Kind switch {
                EventControlAction.TargetRoute => "Activate Haley event target",
                EventControlAction.SeatRoute => "Sit on vacant festival seat",
                EventControlAction.ShopRoute => "Open festival shop",
                _ => "Talk to " + (ev.actors.FirstOrDefault(a => a.GetBoundingBox().Intersects(
                    new Rectangle(c.Tile.X*64,c.Tile.Y*64,64,64)) || a.TilePoint.X == c.Tile.X
                    && (a.TilePoint.Y == c.Tile.Y || a.TilePoint.Y == c.Tile.Y + 1))?.Name ?? "festival NPC") }),
        };
        if (this.config.UiPadMenusEnabled)
        {
            var actions = (Dictionary<string, object>)snapshot["event_actions"];
            var people = new Dictionary<string, string>(StringComparer.Ordinal);
            // ADR 240: who she can see here, nearest first, before and apart from the sliced route plan.
            // A tile is the bounding-box centre (the same read as TilePoint, without its setters; ADR 25).
            var seenOrder = new List<(int D, int Y, int X, int I)>();
            var seenRows = new List<object[]>();
            foreach (NPC actor in ev.actors.Take(128))
            {
                if (actor.IsInvisible || string.IsNullOrWhiteSpace(actor.displayName)) continue;
                string id = this.eventObjects.GetValue(actor, _ => new EventObjectId("ei" + ++this.nextEventObjectId)).Value;
                people[id] = UiPadTable.Trim(actor.displayName, 80);
                Point centre = actor.GetBoundingBox().Center;
                int ax = centre.X / 64, ay = centre.Y / 64;
                seenOrder.Add((Math.Abs(ax - grab.X) + Math.Abs(ay - grab.Y), ay, ax, seenRows.Count));
                seenRows.Add(new object[] { id, ax, ay, false });
            }
            actions["people"] = people;
            seenOrder.Sort();
            var seenPeople = new List<object[]>();
            foreach (var key in seenOrder)
            {
                if (seenPeople.Count >= EventControlAction.MaxEventSeen) break;
                object[] row = seenRows[key.I];
                // `talk`: the game's own checks take a talk with this actor at its tile or the tile above it
                // now (the same capture a route binds); the route and A are still admitted natively.
                bool talk = false;
                for (int dy = 0; dy <= 1 && !talk; dy++)
                    talk = this.CaptureEventAction(new Point(key.X, key.Y - dy), route: true, inspectOnly: true).Target is { } heard
                        && heard.Kind == EventControlAction.NpcRoute && heard.SourceFactId.Split(':') is { Length: >= 4 } part
                        && part[2] == (string)row[0];
                row[3] = talk;
                seenPeople.Add(row);
            }
            // What answers a press: this event map's Action cells (one sliced scan per map, kept across plans),
            // each rechecked now and described by the capture a route would bind (its kind and exact source).
            EventApproachPlan? scan = this.eventApproachPlan is { CellsDone: true } visibleScan && ReferenceEquals(visibleScan.Event, ev)
                ? visibleScan : this.eventApproachRefresh is { CellsDone: true } refreshScan && ReferenceEquals(refreshScan.Event, ev) ? refreshScan : null;
            var cellOrder = new List<(int D, int Y, int X, int I)>();
            if (scan?.Cells is { } cells && ReferenceEquals(scan.Location, Game1.currentLocation))
                for (int i = 0; i < cells.Count; i++)
                    if (Game1.currentLocation.doesTileHaveProperty(cells[i].X, cells[i].Y, "Action", "Buildings") is not null)
                        cellOrder.Add((Math.Abs(cells[i].X - grab.X) + Math.Abs(cells[i].Y - grab.Y), cells[i].Y, cells[i].X, i));
            cellOrder.Sort();
            var seenThings = new List<object[]>();
            int tried = 0;
            foreach (var key in cellOrder)
            {
                if (seenThings.Count >= EventControlAction.MaxEventSeen || tried++ >= 2 * EventControlAction.MaxEventSeen) break;
                if (this.CaptureEventAction(new Point(key.X, key.Y), route: true, inspectOnly: true).Target is { } pressed
                    && pressed.Kind is EventControlAction.ShopRoute or EventControlAction.InteractRoute)
                    seenThings.Add(new object[] { pressed.SourceFactId, pressed.Kind, key.X, key.Y });
            }
            // Routes are ready when the plan for her current pose is complete and every route it holds is published now.
            bool ready = this.eventApproachPlan is { Done: true } readyPlan && readyPlan.ValidatedTick >= 0
                && Game1.ticks - readyPlan.ValidatedTick <= 1 && routes.Count == readyPlan.Published.Count
                && this.EventApproachPlanValid(readyPlan);
            actions["seen"] = new Dictionary<string, object> {
                ["people"] = seenPeople, ["people_total"] = seenRows.Count,
                ["things"] = seenThings, ["things_total"] = cellOrder.Count, ["things_known"] = scan is not null,
                ["routes_ready"] = ready };
        }
        if (!string.IsNullOrWhiteSpace(hostName))
            ((Dictionary<string, object>)snapshot["event_actions"])["festival_host"] = hostName;
        if (snapshot.TryGetValue("capabilities", out var raw) && raw is Dictionary<string, object> caps && targets.Count > 0)
            caps[WorldInteractionTarget.Primitive] = new Dictionary<string, object> { ["available"] = true };
    }

    private object[] EventNearbyTargets()
    {
        if (!this.EventActionsReady || Game1.CurrentEvent is not Event { isFestival: true, eventSwitched: false } ev) return Array.Empty<object>();
        Point at = Game1.player.TilePoint;
        var found = new List<(int Distance, Dictionary<string, object> Row)>();
        for (int y = at.Y - 12; y <= at.Y + 12; y++)
            for (int x = at.X - 12; x <= at.X + 12; x++)
            {
                if (!Game1.currentLocation.isTileOnMap(new Vector2(x, y))) continue;
                var capture = this.CaptureEventAction(new Point(x, y), inspectOnly: true);
                if (capture.Target is not { } target) continue;
                string label = target.Kind;
                if (target.Kind == EventControlAction.Npc)
                {
                    var rect = new Rectangle(x * 64, y * 64, 64, 64);
                    label = ev.actors.First(a => a.GetBoundingBox().Intersects(rect)
                        || a.TilePoint.X == x && (a.TilePoint.Y == y || a.TilePoint.Y == y + 1)).Name;
                    if (label.Length > 60) label = label[..60];
                }
                found.Add((Math.Abs(x - at.X) + Math.Abs(y - at.Y), new Dictionary<string, object> {
                    ["kind"] = target.Kind, ["label"] = label, ["tile"] = new[] { x, y }, ["ref"] = target.SourceFactId }));
            }
        // A nearby bench must not crowd out every NPC/shop needed for a multi-part goal.
        return found.GroupBy(f => (string)f.Row["kind"])
            .SelectMany(g => g.OrderBy(f => f.Distance).ThenBy(f => (string)f.Row["ref"], StringComparer.Ordinal).Take(2))
            .OrderBy(f => f.Distance).ThenBy(f => (string)f.Row["ref"], StringComparer.Ordinal)
            .Take(EventControlAction.MaxOffers).Select(f => (object)f.Row).ToArray();
    }

    private bool EventRouteCell(GameLocation loc, Event ev, Farmer player, int x, int y)
    {
        return ReferenceEquals(loc.currentEvent, ev) && NativeRouteCell.IsWalkable(loc, player, x, y)
            // A walkable touch-action/warp is still not an inert route leg.
            && loc.doesTileHaveProperty(x, y, "TouchAction", "Back") is null
            && !loc.warps.Any(w => w is not null && w.X == x && w.Y == y);
    }

    // Character.GetGrabTile (:1169-1179) supports each side of the egg. Execution
    // still requires the actual grab tile, not merely an adjacent tile centre.
    private static IEnumerable<(Point Stand, int Facing)> EventEggPoses(Point tile)
    {
        yield return (new(tile.X, tile.Y + 1), 0);
        yield return (new(tile.X - 1, tile.Y), 1);
        yield return (new(tile.X, tile.Y - 1), 2);
        yield return (new(tile.X + 1, tile.Y), 3);
    }

    private sealed record EventEggCandidate(Point Tile, Point Stand, int Facing, IReadOnlyList<(int X, int Y)> Path, Prop Prop);
    private sealed record EventApproachCandidate(WorldInteractionTarget Target, Point Tile, Point Stand, int Facing,
        IReadOnlyList<(int X, int Y)> Path);
    // One shared slice per actual UpdateTicking, including discovery, frontier,
    // pose selection and path verification. Publication/admission never drain it.
    private const int EventApproachWorkPerTick = 64;
    private int eventApproachWorkLastTick, eventApproachNativeChecksLastTick;
    private long eventApproachPlanningTick = long.MinValue;
    private EventApproachPlan? eventApproachPlan;
    private EventApproachPlan? eventApproachRefresh;
    private sealed class EventApproachPlan
    {
        public Event Event = null!;
        public GameLocation Location = null!;
        public Farmer Player = null!;
        public Point Actor;
        public int Facing, Width, Height;
        public string Pin = "";
        public CommandInboxIdentity Identity;
        public bool LeaseHeld;
        public string LeaseOwner = "";
        public long LeaseGeneration, LeaseFence;
        public IEnumerator<int> Work = null!;
        public bool Done;
        public long ValidatedTick = long.MinValue, CompletedTick;
        public List<EventApproachCandidate> Offers = new(), Published = new();
        // ADR 240: the event map's Action cells do not depend on her pose. One sliced scan per map; a plan
        // rebuilt after she moves or a lease changes inherits it, and perception reads it before any route.
        public List<Point>? Cells;
        public bool CellsDone, CellsMain;
        /// <summary>Review R5 S7: when the map scan ran. An inherited scan older than EventControlAction.CellScanMaxAgeMs
        /// is not inherited, so the next refresh rescans and an Action cell the event added in place is seen.</summary>
        public long CellsScannedMs;

        public static EventApproachCandidate? ReadSelected(ModEntry mod, WorldInteractionTarget target)
        {
            // Route authority binds the published destination and current player pose.
            // The faced tile is captured only once the route reaches its final stand.
            if (mod.eventApproachPlan is not { Done: true } plan || !mod.EventApproachPlanValid(plan)
                || Game1.ticks < plan.ValidatedTick || Game1.ticks - plan.ValidatedTick > 1
                || mod.eventApproachWorkLastTick >= EventApproachWorkPerTick) return null;
            foreach (var published in plan.Published)
            {
                if (published.Target != target) continue;
                mod.eventApproachWorkLastTick++;
                mod.eventApproachNativeChecksLastTick++;
                return mod.CaptureEventAction(published.Tile, route: true).Target == target ? published : null;
            }
            return null;
        }

        public static bool Matches(ModEntry mod, WorldInteractionTarget target) => EventControlAction.IsApproach(target.Kind)
            ? ReadSelected(mod, target) is not null
            : mod.EventEggCandidates().Any(c => c.Tile.X == target.TileX && c.Tile.Y == target.TileY
                && mod.CaptureEventAction(c.Tile, route: true).Target == target);
    }
    private bool EventApproachPlanValid(EventApproachPlan p) => this.EventApproachReady
        && !this.actionOwner.StopLatched && !this.actionOwner.ReleaseRequested
        && !this.actionOwner.RecordFaulted && !this.actionOwner.IsReleasing
        && ReferenceEquals(Game1.CurrentEvent,p.Event) && ReferenceEquals(Game1.currentLocation,p.Location)
        && ReferenceEquals(Game1.player,p.Player) && p.Player.TilePoint == p.Actor && p.Player.FacingDirection == p.Facing
        && p.Location.Map.Layers[0].LayerWidth == p.Width && p.Location.Map.Layers[0].LayerHeight == p.Height
        && NativeEventControl.Token() == p.Pin && this.CurrentCommandIdentity() == p.Identity
        // Reachability is actor/session authority, not input ownership. An idle
        // offer survives clean ACQUIRE; admission and every consumer still need
        // the request's live owner. Release/STOP/reacquire must retire old plans.
        && this.actionOwner.FencedGeneration == p.LeaseFence
        && (p.LeaseHeld
            ? this.ReflexLeaseHeld && this.actionOwner.OwnerId == p.LeaseOwner
                && this.actionOwner.Generation == p.LeaseGeneration
            : !this.actionOwner.IsArmed || this.ReflexLeaseHeld)
        && p.Event.actors.Count <= 128 && p.Location.mapSeats.Count <= 128 && p.Location.farmers.Count <= 16
        && p.Event.festivalProps.Count <= 128;

    private List<EventApproachCandidate> EventApproachCandidates()
    {
        // Only completed plans escape. A withdrawn target never survives publication
        // or admission, even if its frontier was computed on earlier ticks.
        if (this.eventApproachPlan is not { Done: true } p || !this.EventApproachPlanValid(p)) return new();
        // UpdateTicking plans against tick N; Game1._update increments to N+1
        // before UpdateTicked captures/admits commands (Game1.cs:3914).
        // Re-read each bounded target after that update, never publish an old receiver.
        if (p.ValidatedTick < 0 || Game1.ticks < p.ValidatedTick || Game1.ticks - p.ValidatedTick > 1) return new();
        if (this.eventApproachWorkLastTick + p.Published.Count >= EventApproachWorkPerTick) return new();
        var current = new List<EventApproachCandidate>();
        foreach (var candidate in p.Published)
        {
            this.eventApproachWorkLastTick++; this.eventApproachNativeChecksLastTick++;
            if (this.CaptureEventAction(candidate.Tile, route: true).Target == candidate.Target) current.Add(candidate);
        }
        return current;
    }
    private void TickEventApproachPlanning()
    {
        if (this.eventApproachPlanningTick == Game1.ticks) return;
        this.eventApproachPlanningTick = Game1.ticks;
        this.eventApproachWorkLastTick = this.eventApproachNativeChecksLastTick = 0;
        if (this.eggRun is not null) return; // admission validation owns the shared slice
        EventApproachPlan? cellSource = this.eventApproachPlan; // ADR 240: a finished cell scan outlives its plan
        if (this.eventApproachPlan is { } old && !this.EventApproachPlanValid(old))
        {
            old.Work.Dispose(); this.eventApproachPlan = null;
            this.eventApproachRefresh?.Work.Dispose(); this.eventApproachRefresh = null;
        }
        if (!this.EventApproachReady || Game1.CurrentEvent is not { } ev
            || this.actionOwner.StopLatched || this.actionOwner.ReleaseRequested) return;
        if (this.eventApproachPlan is null || this.eventApproachPlan.Done
            && Game1.ticks - this.eventApproachPlan.CompletedTick > 180 && this.eventApproachRefresh is null)
        {
            var loc = Game1.currentLocation;
            var layer = loc.Map.Layers[0];
            if (layer.LayerWidth <= 0 || layer.LayerHeight <= 0
                || (long)layer.LayerWidth * layer.LayerHeight > FestivalMapSearch.MaxCells
                || ev.actors.Count > 128 || loc.mapSeats.Count > 128 || loc.farmers.Count > 16 || ev.festivalProps.Count > 128) return;
            var p = new EventApproachPlan { Event=ev, Location=loc, Player=Game1.player,
                Actor=Game1.player.TilePoint, Facing=Game1.player.FacingDirection,
                Width=layer.LayerWidth, Height=layer.LayerHeight, Pin=NativeEventControl.Token()!,
                Identity=this.CurrentCommandIdentity(), LeaseHeld=this.ReflexLeaseHeld,
                LeaseOwner=this.actionOwner.OwnerId, LeaseGeneration=this.actionOwner.Generation,
                LeaseFence=this.actionOwner.FencedGeneration };
            if (cellSource is { CellsDone: true } scanned && ReferenceEquals(scanned.Event, ev) && ReferenceEquals(scanned.Location, loc)
                && scanned.Width == p.Width && scanned.Height == p.Height && scanned.CellsMain == ev.eventSwitched
                && Environment.TickCount64 - scanned.CellsScannedMs <= EventControlAction.CellScanMaxAgeMs) // review R5 S7
            { p.Cells = scanned.Cells; p.CellsDone = true; p.CellsMain = scanned.CellsMain; p.CellsScannedMs = scanned.CellsScannedMs; }
            p.Work = this.BuildEventApproaches(p).GetEnumerator();
            if (this.eventApproachPlan is null) this.eventApproachPlan = p;
            else this.eventApproachRefresh = p;
        }
        // Refresh reachability in the same bounded slice while re-reading every
        // exact published receiver each tick. Time alone must not withdraw an
        // unchanged offer halfway through a durable reservation. Old paths grant
        // no input: admission validates the full path before any route leg, and
        // each consumer still checks its current swept cells.
        var visible = this.eventApproachPlan;
        // Leave space for validation plus the bounded UI/inspection/admission
        // reads in the same tick. A refresh must not spend the entire slice
        // before a command can recapture its exact receiver. Repeated readers
        // still share the hard64 cap and can never buy extra frontier work.
        // ADR 240: with up to sixteen published routes, keep room for one publication pass and one reader pass
        // (2 per offer) plus admission; 6 per offer would leave a refresh no slice at all.
        int reserve = this.config.UiPadMenusEnabled ? Math.Min(EventApproachWorkPerTick - 8, 2 * visible.Offers.Count + 8)
            : 6 * visible.Offers.Count;
        var plan = this.eventApproachRefresh ?? visible;
        while (!plan.Done && this.eventApproachWorkLastTick < EventApproachWorkPerTick - reserve)
        {
            this.eventApproachWorkLastTick++;
            if (!plan.Work.MoveNext()) { plan.Done = true; plan.CompletedTick = Game1.ticks; plan.Work.Dispose(); }
        }
        if (this.eventApproachRefresh is { Done: true } refreshed)
        {
            this.eventApproachPlan = visible = refreshed;
            this.eventApproachRefresh = null;
        }
        plan = visible;
        if (plan.Done && this.eventApproachWorkLastTick + 2 * plan.Offers.Count <= EventApproachWorkPerTick)
        {
            plan.Published = new();
            foreach(var candidate in plan.Offers)
            {
                this.eventApproachWorkLastTick++; this.eventApproachNativeChecksLastTick++;
                if (this.CaptureEventAction(candidate.Tile,route:true).Target == candidate.Target) plan.Published.Add(candidate);
            }
            plan.ValidatedTick = Game1.ticks;
        }
    }
    private IEnumerable<int> BuildEventApproaches(EventApproachPlan p)
    {
        var ev=p.Event; var loc=p.Location; var player=p.Player;
        var tiles = new SortedSet<Point>(Comparer<Point>.Create((a,b) => a.Y != b.Y ? a.Y.CompareTo(b.Y) : a.X.CompareTo(b.X)));
        if (!ev.isFestival) tiles.Add(ev.playerControlTargetTile);
        if (ev.isFestival || this.config.UiPadMenusEnabled)
        {
            // Indexed reads allow native collections to change between slices.
            for (int i=0;i<ev.actors.Count && i<128;i++)
            { var actor=ev.actors[i]; tiles.Add(actor.TilePoint); tiles.Add(new(actor.TilePoint.X,actor.TilePoint.Y-1)); this.eventApproachNativeChecksLastTick++; yield return 0; }
            for (int i=0;i<loc.mapSeats.Count && i<128;i++)
            {
                var positions=loc.mapSeats[i].GetSeatPositions(ignore_offsets:true);
                this.eventApproachNativeChecksLastTick++; yield return 0;
                if (positions.Count>64) yield break;
                foreach (var at in positions) { tiles.Add(at.ToPoint()); yield return 0; }
            }
            if (p.CellsDone && p.Cells is { } known)
            {
                // ADR 240: an inherited scan of this same event map; captures below still recheck each cell.
                foreach (Point cell in known) { tiles.Add(cell); this.eventApproachNativeChecksLastTick++; yield return 0; }
            }
            else
            {
                var found = new List<Point>();
                for (int y=0;y<p.Height;y++)
                    for (int x=0;x<p.Width;x++)
                    {
                        this.eventApproachNativeChecksLastTick++;
                        if (loc.doesTileHaveProperty(x,y,"Action","Buildings") is not null) { tiles.Add(new(x,y)); found.Add(new(x,y)); }
                        yield return 0;
                    }
                p.Cells = found; p.CellsMain = ev.eventSwitched; p.CellsDone = true; p.CellsScannedMs = Environment.TickCount64;
            }
        }
        var search = new FestivalMapSearch(p.Width,p.Height,(p.Actor.X,p.Actor.Y));
        bool Clear(int x,int y) { this.eventApproachNativeChecksLastTick++; return this.EventRouteCell(loc,ev,player,x,y); }
        while (!search.Done) { search.Tick(Clear,(_,_)=>false,1); yield return 0; }
        foreach (Point tile in tiles)
        {
            var target=this.CaptureEventAction(tile,route:true).Target;
            this.eventApproachNativeChecksLastTick++; yield return 0;
            if (target is not { } offered || !EventControlAction.IsApproach(offered.Kind)) continue;
            EventApproachCandidate? best=null;
            foreach (var pose in EventEggPoses(tile))
            {
                bool allowed=Clear(pose.Stand.X,pose.Stand.Y);
                if (allowed && offered.Kind==EventControlAction.SeatRoute)
                    allowed=loc.mapSeats.Any(s=>s.OccupiesTile(tile.X,tile.Y)
                        && PredictFestivalSeat(s,player,loc,tile,new Vector2(pose.Stand.X*64+32,pose.Stand.Y*64+32)) is not null);
                yield return 0;
                if (!allowed || !search.TryParent((pose.Stand.X,pose.Stand.Y),out _)) continue;
                var path=new List<(int X,int Y)>(); var at=(pose.Stand.X,pose.Stand.Y);
                bool valid=true;
                while (at!=(p.Actor.X,p.Actor.Y))
                {
                    if (!Clear(at.Item1,at.Item2) || !search.TryParent(at,out var parent)) { valid=false; break; }
                    path.Add(at); at=parent; yield return 0;
                }
                if (!valid) continue;
                // Reverse itself is sliced; never copy/reverse a map-sized path in one tick.
                for(int left=0,right=path.Count-1;left<right;left++,right--)
                { (path[left],path[right])=(path[right],path[left]); yield return 0; }
                if (best is null || path.Count<best.Path.Count) best=new(offered,tile,pose.Stand,pose.Facing,path);
            }
            this.eventApproachNativeChecksLastTick++;
            if (best is null || this.CaptureEventAction(tile,route:true).Target!=offered) { yield return 0; continue; }
            p.Offers.Add(best);
            p.Offers=p.Offers.GroupBy(c=>c.Target.Kind).SelectMany(g=>g.OrderBy(c=>c.Path.Count)
                .ThenBy(c=>c.Target.SourceFactId,StringComparer.Ordinal)
                .DistinctBy(c => this.config.UiPadMenusEnabled && g.Key == EventControlAction.NpcRoute
                    ? c.Target.SourceFactId.Split(':')[2] : c.Target.SourceFactId)
                .Take(!this.config.UiPadMenusEnabled ? 2 : g.Key == EventControlAction.NpcRoute
                    ? EventControlAction.MaxEventPeopleRoutes : EventControlAction.MaxEventKindRoutes))
                .OrderBy(c=>c.Path.Count).ThenBy(c=>c.Target.SourceFactId,StringComparer.Ordinal)
                .Take(this.config.UiPadMenusEnabled ? EventControlAction.MaxEventRoutes : EventControlAction.MaxOffers).ToList();
            yield return 0;
        }
    }

    private void CheckEventRouteBeforeRuntime()
    {
        if (this.eggRun is not { Acting:false, Pending:true, Stopping:false } run) return;
        this.AggregateEventRouteCleanup(run);
        if (!run.Cleanup || !this.EventEggPinValid(run) || !this.ReflexLeaseHeld || NowUnixMs() >= run.ExecutionDeadline)
        { run.Stopping=true; this.backgroundMove?.RequestRelease("EVENT_ROUTE_WITHDRAWN"); }
    }
    private List<EventEggCandidate> EventEggCandidates()
    {
        var result = new List<EventEggCandidate>();
        if (Game1.CurrentEvent is not Event ev || Game1.player is not Farmer player) return result;
        GameLocation loc = player.currentLocation;
        // Native egg props are exactly one tile (Event.cs:11610). Fail closed for other sizes.
        foreach (Prop prop in ev.festivalProps.Take(128))
        {
            Rectangle box = this.Helper.Reflection.GetField<Rectangle>(prop, "boundingRect").GetValue();
            if (box.Width != 64 || box.Height != 64 || box.X % 64 != 0 || box.Y % 64 != 0) continue;
            Point target = new(box.X / 64, box.Y / 64);
            if (Math.Abs(target.X - player.TilePoint.X) + Math.Abs(target.Y - player.TilePoint.Y) > EventControlAction.MaxRouteTiles + 1)
                continue;
            EventEggCandidate? best = null;
            foreach (var pose in EventEggPoses(target))
            {
                if (!this.EventRouteCell(loc, ev, player, pose.Stand.X, pose.Stand.Y)) continue;
                var path = EventGoalRoute.Plan((player.TilePoint.X, player.TilePoint.Y), (pose.Stand.X, pose.Stand.Y),
                    (x, y) => this.EventRouteCell(loc, ev, player, x, y));
                if (path is not null && (best is null || path.Count < best.Path.Count))
                    best = new(target, pose.Stand, pose.Facing, path, prop);
            }
            if (best is not null) result.Add(best);
        }
        return result.OrderBy(p => p.Path.Count).ThenBy(p => p.Tile.Y).ThenBy(p => p.Tile.X)
            .Take(EventControlAction.MaxOffers).ToList();
    }

    private bool EventEggPinValid(EventEggRun run) => this.EventActionsReady
        && ReferenceEquals(Game1.CurrentEvent, run.Event) && ReferenceEquals(Game1.currentLocation, run.Location)
        && ReferenceEquals(Game1.player, run.Player) && NativeEventControl.Token() == run.Pin
        && run.ExitPin?.Valid(run.Event) != false
        && (run.ApproachTarget is { } approach
            ? this.CaptureEventAction(run.Tile, route: true).Target is { } current
                && current.SourceFactId == approach.SourceFactId && current.Kind == approach.Kind
            : run.Player.festivalScore == run.StartScore
        && run.Event.festivalProps.Any(p => ReferenceEquals(p, run.Prop))
        && run.Event.festivalProps.Count(p => p.isColliding(new Rectangle(run.Tile.X * 64, run.Tile.Y * 64, 64, 64))) == 1);

    private void ArmEventEggRoute(MenuStepCommandRequest req)
    {
        if (EventControlAction.IsApproach(req.InteractionTarget?.Kind))
        {
            var approach = EventApproachPlan.ReadSelected(this, req.InteractionTarget!.Value);
            if (approach is null || this.eggRun is not null || this.backgroundMove?.Busy != false
                || NowUnixMs() >= req.DeadlineUnixMs || NowUnixMs() >= (req.ExecutionDeadlineUnixMs ?? req.DeadlineUnixMs)
                || !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(req, this.CurrentCommandIdentity(), Context.IsWorldReady,
                    true, true, out _) || !this.ReflexLeaseHeld)
            { this.WriteCommandResult(this.commandInbox.CompleteRejected(req, CommandRejectReason.BindingMismatch,
                this.CurrentCommandIdentity(), NowUnixMs(), "EVENT_APPROACH_CHANGED")); return; }
            this.eggRun = new EventEggRun { Request = req, Event = Game1.CurrentEvent, Location = Game1.currentLocation,
                Player = Game1.player, ApproachTarget = approach.Target, Pin = NativeEventControl.Token()!,
                ExitPin = EventExitPin.Capture(Game1.CurrentEvent),
                Tile = approach.Tile, Stand = approach.Stand, Facing = approach.Facing, Path = approach.Path,
                InputLimit = EventControlAction.RouteBudget(approach.Path, (Game1.player.TilePoint.X,Game1.player.TilePoint.Y)), Started = Game1.ticks,
                FaultPanics = this.backgroundMove?.FestivalFaultPanics ?? 0,
                ExecutionDeadline = Math.Min(req.ExecutionDeadlineUnixMs ?? req.DeadlineUnixMs, NowUnixMs() + EventControlAction.ApproachMs) };
            var armed = this.eggRun;
            this.backgroundMove.EventRouteAuthority = step => step.RequestId == armed.Request.RequestId
                && ReferenceEquals(this.eggRun,armed) && !armed.Stopping && this.EventEggPinValid(armed) && this.ReflexLeaseHeld
                && this.EventSegmentValid(armed);
            return;
        }
        var target = req.InteractionTarget;
        var candidate = this.EventEggCandidates().FirstOrDefault(c => target is { } t && c.Tile.X == t.TileX && c.Tile.Y == t.TileY);
        if (this.eggRun is not null || this.backgroundMove?.Busy != false || !this.EventActionsReady || candidate is null
            || NowUnixMs() >= req.DeadlineUnixMs || NowUnixMs() >= (req.ExecutionDeadlineUnixMs ?? req.DeadlineUnixMs)
            || req.InteractionTarget != this.CaptureEventAction(candidate.Tile, route: true).Target
            || !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(req, this.CurrentCommandIdentity(), Context.IsWorldReady,
                true, true, out _) || this.actionOwner.ReleaseRequested || this.actionOwner.StopLatched)
        {
            this.WriteCommandResult(this.commandInbox.CompleteRejected(req, CommandRejectReason.BindingMismatch,
                this.CurrentCommandIdentity(), NowUnixMs(), "EVENT_EGG_ROUTE_CHANGED")); return;
        }
        this.eggRun = new EventEggRun { Request = req, Event = Game1.CurrentEvent, Location = Game1.currentLocation,
            Player = Game1.player, Prop = candidate.Prop, Pin = NativeEventControl.Token()!, Tile = candidate.Tile,
            Stand = candidate.Stand, Facing = candidate.Facing, Path = candidate.Path, StartScore = Game1.player.festivalScore,
            Started = Game1.ticks, FaultPanics = this.backgroundMove?.FestivalFaultPanics ?? 0, ExecutionDeadline = Math.Min(req.ExecutionDeadlineUnixMs ?? req.DeadlineUnixMs,
                NowUnixMs() + Math.Min(20_000, Game1.CurrentEvent.festivalTimer - 3000)) };
    }

    // After the runtime begins its pad ledger tick: all route presses use the existing sole writer.
    private void TickEventEggRoute()
    {
        this.TickEventApproachPlanning();
        if (this.eggRun is not { } run) return;
        this.AggregateEventRouteCleanup(run);
        if (run.Automatic && !run.Acting && !run.Event.festivalProps.Contains(run.Prop)) run.Replan = true;
        // A stolen prop cancels the next leg, not a verified step already in flight.
        if (run.Replan && run.Pending && this.festivalRun is { Ending: false } f
            && this.FestivalPhaseValid(f) && this.FestivalLeaseValid(f)) return;
        if (!run.Acting && (!run.Cleanup || !this.EventEggPinValid(run) || Game1.ticks - run.Started >= (run.Automatic || run.ApproachTarget is not null ? 7200 : EventControlAction.MaxRouteTicks)
            || run.ApproachTarget is null && run.Event.festivalTimer <= (run.Automatic ? 0 : 3000)
            || run.Presses >= (run.Automatic ? 512 : run.InputLimit) || NowUnixMs() >= run.ExecutionDeadline
            || !this.ReflexLeaseHeld))
        {
            if (run.Pending && !run.Stopping) { run.Stopping = true; this.backgroundMove?.RequestRelease("EVENT_EGG_ROUTE_STOP"); }
            else if (run.Pending) return;
            else this.FinishEventEggRoute("InteractionUncertain", false);
            return;
        }
        if (run.Pending || this.backgroundMove?.Busy != false) return;
        // Admission consumes a completed offer, then validates its whole cached path
        // over bounded ticks before the first input. The public one-second deadline
        // is an admission limit; the pinned execution deadline covers this phase.
        if (run.ApproachTarget is not null && run.ValidationIndex < run.Path.Count)
        {
            int budget = EventApproachWorkPerTick - this.eventApproachWorkLastTick;
            while (budget-- > 0 && run.ValidationIndex < run.Path.Count)
            {
                var cell = run.Path[run.ValidationIndex++];
                this.eventApproachWorkLastTick++; this.eventApproachNativeChecksLastTick++;
                if (!this.EventRouteCell(run.Location, run.Event, run.Player, cell.X, cell.Y))
                { this.FinishEventEggRoute("InteractionUncertain", false); return; }
            }
            return;
        }
        if (run.ApproachTarget is not null && run.PathIndex < run.Path.Count && run.SegmentEnd < run.PathIndex)
        {
            var start = run.PathIndex == 0 ? (run.ApproachTarget.Value.ActorTileX,run.ApproachTarget.Value.ActorTileY)
                : run.Path[run.PathIndex-1];
            run.SegmentEnd = EventControlAction.SegmentEnd(run.Path,run.PathIndex,start);
        }
        int waypointIndex = run.ApproachTarget is not null ? run.SegmentEnd : run.PathIndex;
        Point waypoint = run.PathIndex < run.Path.Count
            ? new Point(run.Path[waypointIndex].X, run.Path[waypointIndex].Y) : run.Stand;
        if (!this.EventRouteCell(run.Location, run.Event, run.Player, waypoint.X, waypoint.Y))
        { run.Replan = run.Automatic && !run.Acting; this.FinishEventEggRoute("InteractionUncertain", false); return; }
        // Character.Tile/TilePoint use the bounding-box centre (Character.cs:346-388),
        // not raw Position. Farmer's box has a sprite-height offset (Farmer.cs:5933-5940).
        Rectangle box = run.Player.GetBoundingBox();
        Vector2 delta = new(waypoint.X * 64 + 32 - (box.X + box.Width / 2),
            waypoint.Y * 64 + 32 - (box.Y + box.Height / 2));
        if (Math.Abs(delta.X) <= 6 && Math.Abs(delta.Y) <= 6 && run.Player.TilePoint == waypoint)
        {
            if (run.PathIndex < run.Path.Count) { run.PathIndex = waypointIndex + 1; return; }
            // Face/flush only via short stick steps, never position/facing writes.
            if (run.Player.FacingDirection == run.Facing && run.Player.GetGrabTile() == run.Tile.ToVector2())
            {
                var capture = this.CaptureEventAction(run.Tile);
                if (!this.FinalEventRouteTarget(run, capture.Target) || capture.Action is null)
                { this.FinishEventEggRoute("InteractionUncertain", false); return; }
                var inner = run.Request with { InteractionTarget = capture.Target, Ticks = 1, DeadlineUnixMs = run.ExecutionDeadline };
                var refused = this.backgroundMove.TryArmHoldTarget(inner, BgHoldKind.Action, capture.Action);
                run.Acting = true; run.Pending = refused is null; run.Presses++;
                if (refused is not null) this.RouteEventEggTerminal(refused);
                return;
            }
            var (dx, dy) = FishCastGeometry.FacingDelta(run.Facing);
            delta = new Vector2(dx * 16, dy * 16);
        }
        // At the final stand, keep the perpendicular axis centred while nudging
        // toward the native grab boundary (bounded; collision is failure).
        if (run.PathIndex >= run.Path.Count && (run.Facing % 2 == 0 ? Math.Abs(delta.X) : Math.Abs(delta.Y)) <= 6
            && run.Player.TilePoint == run.Stand)
        {
            if (run.Player.FacingDirection == run.Facing && run.Player.GetGrabTile() == run.Tile.ToVector2())
            { run.PathIndex = run.Path.Count; delta = Vector2.Zero; }
            else
            {
                var (dx, dy) = FishCastGeometry.FacingDelta(run.Facing);
                delta = new Vector2(dx * 16, dy * 16);
            }
        }
        if (delta == Vector2.Zero) // re-enter action arm with a centre-independent final pose
        {
            var capture = this.CaptureEventAction(run.Tile);
            if (capture.Action is null || !this.FinalEventRouteTarget(run, capture.Target)) { this.FinishEventEggRoute("InteractionUncertain", false); return; }
            var refused = this.backgroundMove.TryArmHoldTarget(run.Request with { InteractionTarget = capture.Target, Ticks = 1,
                DeadlineUnixMs = run.ExecutionDeadline },
                BgHoldKind.Action, capture.Action);
            run.Acting = true; run.Pending = refused is null; run.Presses++;
            if (refused is not null) this.RouteEventEggTerminal(refused);
            return;
        }
        string direction = Math.Abs(delta.X) > 6 ? delta.X > 0 ? "right" : "left" : delta.Y > 0 ? "down" : "up";
        int ticks = Math.Clamp((int)((Math.Abs(delta.X) > 6 ? Math.Abs(delta.X) : Math.Abs(delta.Y)) / 6), 1,
            run.ApproachTarget is not null ? EventControlAction.SegmentTicks : EventControlMove.MaxTicks);
        run.Before = run.Player.Position; run.BeforeTile = run.Player.TilePoint; run.Waypoint = waypoint;
        run.Direction = direction; run.Ticks = ticks;
        var step = run.Request with { Primitive = MenuStepCommandInbox.PrimitiveMoveCardinal, InteractionTarget = null,
            Direction = direction, Ticks = ticks, EventToken = run.Pin, DeadlineUnixMs = run.ExecutionDeadline };
        var rejection = this.backgroundMove.TryArm(step);
        run.Pending = rejection is null; run.Presses++;
        if (rejection is not null) this.RouteEventEggTerminal(rejection);
    }

    private bool RouteEventEggTerminal(BackgroundMoveTerminal? terminal)
    {
        if (terminal is null || this.eggRun is not { } run || terminal.Request.RequestId != run.Request.RequestId) return false;
        run.Pending = false;
        this.AggregateEventRouteCleanup(run);
        bool clean = terminal.Hold?.Outcome.CleanupConfirmed
            ?? (terminal.Kind is BackgroundTerminalKind.Completed or BackgroundTerminalKind.NotCompleted
                || terminal.Label.StartsWith("NOT_COMPLETED:RELEASED_EARLY", StringComparison.Ordinal)
                || terminal.ConsumedH == 0 && terminal.Label.StartsWith("RELEASED_BEFORE_LATCH:", StringComparison.Ordinal));
        clean |= run.Automatic && this.backgroundMove?.FestivalReleaseConfirmed == true;
        run.Cleanup &= clean && this.backgroundMove?.PressOverrideOutstanding != true;
        Vector2 delta = run.Player.Position - run.Before;
        bool ok = run.Acting ? run.ApproachTarget is { } approach
            ? terminal.Hold?.Outcome is { Opened: true, CleanupConfirmed: true } effect
                && effect.Outcome == WorldInteractionKinds.ExpectedOutcome(approach.Kind)
                && this.backgroundMove?.LastEventBefore is { } before && this.backgroundMove.LastEventAfter is { } after
                && EventControlAction.Effect(before,after)
            : terminal.Hold?.Outcome is { Opened: true, CleanupConfirmed: true, Outcome: "FestivalEggCollected" }
                && this.backgroundMove?.LastEventBefore?.Score == run.StartScore
                && this.backgroundMove?.LastEventAfter?.Score == run.StartScore + 1
                && run.Player.festivalScore == run.StartScore + 1 && !run.Event.festivalProps.Contains(run.Prop)
                && ReferenceEquals(Game1.CurrentEvent, run.Event) && NativeEventControl.Token() == run.Pin
            : EventControlAction.RouteStep(terminal.Kind == BackgroundTerminalKind.Completed, run.Cleanup,
                (this.EventEggPinValid(run) || run.Automatic && !run.Event.festivalProps.Contains(run.Prop)
                    && this.festivalRun is { } festival && this.FestivalPhaseValid(festival) && this.FestivalLeaseValid(festival)
                    && run.Player.festivalScore == run.StartScore)
                && this.EventSegmentEndpoint(run),
                run.Direction, run.Ticks, delta.X, delta.Y, segment: run.ApproachTarget is not null);
        run.Trace.Add(new Dictionary<string, object> { ["run_id"] = terminal.RunId, ["kind"] = run.Acting ? "A" : run.Direction,
            ["verified"] = ok, ["cleanup"] = clean, ["label"] = terminal.Label });
        if (!run.Acting)
        {
            run.Trace[^1]["before_pixel"] = new[] { run.Before.X, run.Before.Y };
            run.Trace[^1]["after_pixel"] = new[] { run.Player.Position.X, run.Player.Position.Y };
            run.Trace[^1]["waypoint"] = new[] { run.Waypoint.X, run.Waypoint.Y };
            run.Trace[^1]["ticks"] = run.Ticks;
        }
        ok &= !run.Stopping;
        if (run.Acting || !ok) this.FinishEventEggRoute(ok && run.Cleanup
            ? run.Automatic ? "FestivalEggCollected" : WorldInteractionKinds.ExpectedOutcome(run.Request.InteractionTarget?.Kind)
            : "InteractionUncertain", ok && run.Cleanup);
        return true;
    }

    private bool EventSegmentValid(EventEggRun run)
    {
        // At most four exact native cells, rechecked at the sole writer's frame
        // gate as well as arm. No turn, touch-action, warp or stale actor bypass.
        if (run.ApproachTarget is null || run.Acting) return true;
        if (run.Pending)
        {
            bool Allowed(Point tile) => tile == run.BeforeTile || run.PathIndex >= run.Path.Count && tile == run.Stand
                || Enumerable.Range(run.PathIndex, Math.Max(0, run.SegmentEnd - run.PathIndex + 1))
                    .Any(i => tile.X == run.Path[i].X && tile.Y == run.Path[i].Y);
            if (!Allowed(run.Player.TilePoint)) return false;
            if (this.backgroundMove?.EventMoveInputPending == true)
            {
                // Farmer.cs:8063-8086: playerControl/event movement ignores buffs,
                // and is <= speed * .066 * elapsed-ms (strafe=2, diagonal lowers it).
                // Read fields only; getMovementSpeed itself writes movementMultiplier.
                // setRunning can change the prior speed to five before movement.
                float bound = Math.Max(2f, Math.Max(5, run.Player.speed) * .066f * Game1.currentGameTime.ElapsedGameTime.Milliseconds);
                if (!float.IsFinite(bound) || bound > 16) return false;
                int advance = (int)Math.Ceiling(bound);
                var (dx,dy) = run.Direction switch { "up" => (0,-1), "right" => (1,0), "down" => (0,1), "left" => (-1,0), _ => (0,0) };
                var box = run.Player.GetBoundingBox();
                var next = new Point((int)Math.Floor((box.X + box.Width/2 + dx*advance) / 64f),
                    (int)Math.Floor((box.Y + box.Height/2 + dy*advance) / 64f));
                if ((dx,dy) == (0,0) || !Allowed(next)) return false;
                // Farmer.MovePositionImpl checks the proposed full rectangle for
                // warps before collision (Farmer.cs:8585-8599). Its exact corner
                // sampling uses Right/Bottom, not Right-1/Bottom-1 (Location:2118).
                // Check both endpoints of the <=16 px sweep, including cells
                // touched by the box outside its centre's route corridor.
                foreach (int offset in new[]{0,advance})
                foreach (int px in new[]{box.Left + dx*offset,box.Right + dx*offset})
                foreach (int py in new[]{box.Top + dy*offset,box.Bottom + dy*offset})
                {
                    int x=(int)Math.Floor(px/64d),y=(int)Math.Floor(py/64d);
                    if (!run.Location.isTileOnMap(new Vector2(x,y))
                        || run.Location.doesTileHaveProperty(x,y,"TouchAction","Back") is not null
                        || run.Location.warps.Any(w=>w is not null && w.X==x && w.Y==y)) return false;
                }
            }
        }
        if (run.PathIndex >= run.Path.Count) return true;
        if (run.SegmentEnd < run.PathIndex || run.SegmentEnd >= run.Path.Count) return false;
        for (int i=run.PathIndex;i<=run.SegmentEnd;i++)
            if (!this.EventRouteCell(run.Location,run.Event,run.Player,run.Path[i].X,run.Path[i].Y)) return false;
        return true;
    }

    private bool EventSegmentEndpoint(EventEggRun run)
    {
        var tile=run.Player.TilePoint;
        if (tile==run.BeforeTile || tile==run.Waypoint) return true;
        if (run.ApproachTarget is null || run.PathIndex>=run.Path.Count) return false;
        for (int i=run.PathIndex;i<=run.SegmentEnd;i++)
            if (tile.X==run.Path[i].X && tile.Y==run.Path[i].Y) return true;
        return false;
    }

    private void AggregateEventRouteCleanup(EventEggRun run)
    {
        void Fault(string reason) { run.Cleanup = false; if (!run.CleanupFaults.Contains(reason)) run.CleanupFaults.Add(reason); }
        if (this.padAdapter?.CleanupFault is { } fault) Fault("adapter_cleanup_fault:" + fault);
        if (this.backgroundMove?.FestivalContainmentFault is { } containment) Fault("containment:" + containment);
        if (this.backgroundMove?.FestivalFaultPanics != run.FaultPanics)
            Fault("pad_panic:" + (this.backgroundMove?.FestivalLastFaultPanicReason ?? "unknown"));
        if (this.padAdapter?.RestoreWritePending == true) Fault("pad_restore_pending");
    }

    private void FinishEventEggRoute(string outcome, bool success)
    {
        if (this.eggRun is not { } run) return;
        this.AggregateEventRouteCleanup(run);
        run.Cleanup &= this.backgroundMove?.Busy == false && this.backgroundMove.PressOverrideOutstanding == false
            && this.padAdapter?.RestoreWritePending != true;
        this.eggRun = null;
        if (run.ApproachTarget is not null && this.backgroundMove is not null) this.backgroundMove.EventRouteAuthority = null;
        if (run.Automatic)
        {
            // A bounded end summary may already have reported cleanup debt. A late
            // terminal cannot certify it or fall into the unrelated manual inbox path.
            if (this.festivalRun is not { } festival) return;
            festival.Cleanup &= run.Cleanup && this.backgroundMove?.Busy != true && this.backgroundMove?.PressOverrideOutstanding != true;
            if (success && festival.Cleanup) festival.Verified++;
            festival.Effects.Add(new() { ["kind"] = "egg", ["verified"] = success && festival.Cleanup,
                ["score_before"] = run.StartScore, ["score_after"] = run.Player.festivalScore,
                ["prop_removed"] = !run.Event.festivalProps.Contains(run.Prop), ["tile"] = new[] { run.Tile.X, run.Tile.Y },
                ["cleanup"] = run.Cleanup, ["steps"] = run.Trace.Count,
                ["all_steps_verified"] = run.Trace.All(t => (bool)t["verified"] && (bool)t["cleanup"]) });
            this.Monitor.Log("FESTIVAL_REFLEX egg " + System.Text.Json.JsonSerializer.Serialize(festival.Effects[^1]), LogLevel.Info);
            if (run.Replan && festival.Cleanup && !run.Acting && this.FestivalPhaseValid(festival) && this.FestivalLeaseValid(festival)) festival.Replans++;
            else if (!success) this.EndFestival("egg_unverified");
            return;
        }
        success &= run.Cleanup && this.backgroundMove?.Busy != true && this.backgroundMove?.PressOverrideOutstanding != true;
        this.RecordEventAction(run.Request, outcome, success, run.Trace);
        this.WriteCommandResult(this.commandInbox.CompleteWorldInteraction(new WorldInteractionVerdict(outcome, success,
            run.Cleanup && this.backgroundMove?.Busy != true && this.backgroundMove?.PressOverrideOutstanding != true,
            run.Presses, null, null, EffectDetail: success ? run.Request.InteractionTarget?.SourceFactId + "|verified_route"
                : string.Join("|", run.CleanupFaults)),
            this.CurrentCommandIdentity(), NowUnixMs()));
    }

    private void RecordEventAction(MenuStepCommandRequest req, string outcome, bool success, object? trace = null)
    {
        this.lastEventActionTick = Game1.ticks;
        this.lastEventAction = new Dictionary<string, object> { ["target_token"] = req.InteractionTarget!.Value.TargetToken,
            ["request_id"] = req.RequestId,
            ["source_fact_id"] = req.InteractionTarget.Value.SourceFactId, ["outcome"] = outcome,
            ["before"] = EventEffectFields(this.backgroundMove?.LastEventBefore),
            ["after"] = EventEffectFields(this.backgroundMove?.LastEventAfter),
            ["verified"] = success, ["captured_tick"] = Game1.ticks, ["trace"] = trace ?? Array.Empty<object>() };
    }

    private static Dictionary<string, object?> EventEffectFields(EventActionFacts? f) => f is null ? new() : new() {
        ["kind"] = f.Kind, ["target_id"] = f.TargetId, ["scope_valid"] = f.ScopeValid, ["sequence"] = f.Sequence,
        ["control"] = f.Control, ["command"] = f.Command, ["target_tile"] = new[] { f.TargetX, f.TargetY },
        ["timer_positive"] = f.TimerPositive, ["score"] = f.Score, ["present"] = f.Present,
        ["expected_menu"] = f.ExpectedMenu, ["menu_type"] = f.MenuType, ["sitting"] = f.Sitting };

    private bool FinalEventRouteTarget(EventEggRun run, WorldInteractionTarget? target) => target is { } t
        && (run.ApproachTarget is not { } offered || t.Kind == EventControlAction.NativeKind(offered.Kind)
            && t.SourceFactId == offered.SourceFactId.Replace(":" + offered.Kind + ":", ":" + t.Kind + ":", StringComparison.Ordinal));

    private void AbortEventEggForSave()
    {
        if (this.eggRun is not { } run) return;
        this.backgroundMove?.RequestRelease("EVENT_ROUTE_SAVE");
        run.ExecutionDeadline = 0;
        run.Acting = false;
        run.Cleanup = false; // normal terminal readback must establish release; save abort never reports success
    }
}
