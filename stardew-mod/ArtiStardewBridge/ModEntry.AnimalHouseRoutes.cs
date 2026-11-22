using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Buildings;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    private HouseTravel? houseTravel;
    private bool houseNativeFenceReady;
    private Farmer? houseNativeReleasePlayer;
    private bool HouseNativeReleaseSettled
    {
        get
        {
            if(this.houseNativeReleasePlayer is not { } player)return true;
            if(ReferenceEquals(Game1.player,player) && player.movementDirections.Count!=0)return false;
            this.houseNativeReleasePlayer=null;
            return true;
        }
    }
    private (CommandInboxIdentity Identity, Farmer Player, GameLocation Location, object Map,
        Vector2 Position, int Facing, int Year, string Season, int Day)? houseBudgetPose;
    private float houseBudgetSpeed;

    private float HouseBudgetSpeed(Farmer p)
    {
        // getMovementSpeed includes this frame's elapsed milliseconds. Hold the
        // publication's sizing estimate at an unchanged identity/pose, so a
        // frame-duration change alone cannot withdraw its authority token.
        var location=p.currentLocation!; // HouseTravelReady already requires this receiver.
        (CommandInboxIdentity,Farmer,GameLocation,object,Vector2,int,int,string,int) pose =
            (this.CurrentCommandIdentity(),p,location,location.Map,p.Position,
                p.FacingDirection,Game1.year,Game1.currentSeason,Game1.dayOfMonth);
        if(this.houseBudgetPose != pose)
        {
            this.houseBudgetPose=pose;
            this.houseBudgetSpeed=p.getMovementSpeed();
        }
        return this.houseBudgetSpeed;
    }
    private sealed record HouseDoor(Building Building, AnimalHouse House, GameLocation Farm,
        GameLocation From, GameLocation To, Point Door, Warp? Exit, WorldInteractionTarget Target, int Budget);
    private sealed class HouseTravel
    {
        public HouseDoor Door = null!;
        public Farmer Player = null!;
        public CommandInboxIdentity Identity;
        public MenuStepCommandRequest Request;
        public IEnumerator<int>? Search;
        public List<(int X, int Y)>? Path;
        public Point Stand, Waypoint, BeforeTile;
        public Vector2 Before;
        public int Index, Presses, Ticks, FaultPanics, Facing;
        public bool Planned, Pending, Acting, Stopping, Nudging, FootprintStop, Cleanup = true;
        public string Direction = "";
        public float Speed = 8; // short first calibration; never extrapolate beyond a checked straight corridor
        public long Deadline, Started;
        public int Year, Day, Width, Height;
        public string Season = "";
        public object Map = null!;
        public Vector2 Origin;
        public List<Point> Corridor = new();
        public List<Dictionary<string, object>> Trace = new();
        public List<string> Faults = new();
    }

    private bool HouseTravelReady => this.houseNativeFenceReady && this.HouseNativeReleaseSettled && this.ExtrasEnabled && this.config.WorldInteractKindsEnabled
        && this.config.WorldInteractionInputEnabled && this.config.MoveThroughEnabled
        && this.BackgroundToolUiNow && this.backgroundMove is not null // ADR 244: no focus term
        && Context.IsWorldReady && !Context.IsMainPlayer && !Context.IsSplitScreen
        && Context.IsPlayerFree && Context.CanPlayerMove && Game1.activeClickableMenu is null
        && Game1.keyboardDispatcher?.Subscriber is null && !Game1.eventUp && Game1.CurrentEvent is null
        && !Game1.isFestival() && Game1.player is { UsingTool: false } p && p.GetType()==typeof(Farmer)
        && !p.IsSitting() && !p.isRidingHorse() && p.mount is null
        && p.currentLocation?.currentEvent is null;

    private List<HouseDoor> HouseDoors()
    {
        var result = new List<HouseDoor>();
        if (!this.HouseTravelReady || Game1.player is not Farmer p || p.currentLocation is not { } from) return result;
        var farm = Game1.getFarm();
        if (farm.buildings.Count > 128) return result;
        foreach (var b in farm.buildings)
        {
            if (b.GetIndoors() is not AnimalHouse house || b.humanDoor.Value.X < 0 || b.humanDoor.Value.Y < 0
                || b.daysOfConstructionLeft.Value > 0 || b.isMoving || p.team.demolishLock.IsLocked()) continue;
            bool exit = ReferenceEquals(from, house);
            if (!exit && !ReferenceEquals(from, farm)) continue;
            Point door = new(b.tileX.Value + b.humanDoor.Value.X, b.tileY.Value + b.humanDoor.Value.Y);
            // Building.updateInteriorWarps rewrites these to the human door's Farm landing (Building.cs:1801-1815).
            var exits = house.warps.Where(w => !w.npcOnly.Value && w.TargetName == farm.NameOrUniqueName
                && w.TargetX == door.X && w.TargetY == door.Y + 1).Take(3).ToList();
            if (exit && exits.Count != 1) continue;
            Warp? warp = exit ? exits[0] : null;
            Point tile = warp is null ? door : new(warp.X, warp.Y);
            string source = warp is null ? SceneIdentity.BuildingDoorFactId(farm.NameOrUniqueName, door.X, door.Y, house.NameOrUniqueName)
                : SceneIdentity.WarpFactId(house.NameOrUniqueName, warp.X, warp.Y, warp.TargetName, warp.TargetX, warp.TargetY);
            string expected = SceneIdentity.Token(exit ? farm.NameOrUniqueName : house.NameOrUniqueName);
            int budget = AnimalHouseRoute.Budget(Math.Abs(tile.X-p.TilePoint.X)+Math.Abs(tile.Y-p.TilePoint.Y), exit, this.HouseBudgetSpeed(p));
            string token = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(
                System.Text.Json.JsonSerializer.Serialize(new object[] { AnimalHouseRoute.Kind, this.CurrentCommandIdentity(),
                    this.animalTargetInstances.Single(b), this.animalTargetInstances.Single(house), house.NameOrUniqueName,
                    source, door.X, door.Y, p.TilePoint.X, p.TilePoint.Y, p.FacingDirection, budget,
                    Game1.year, Game1.currentSeason, Game1.dayOfMonth })))).ToLowerInvariant();
            var target = new WorldInteractionTarget(token, source, tile.X, tile.Y, p.TilePoint.X, p.TilePoint.Y,
                p.FacingDirection, AnimalHouseRoute.Kind, expected);
            if (target.IsValid) result.Add(new(b,house,farm,from,exit ? farm : house,door,warp,target,budget));
            if (result.Count == AnimalRoutine.HousesMax) break;
        }
        return result;
    }

    private void EnrichHouseTravel(Dictionary<string, object> routine)
        => routine["routes"] = this.HouseDoors().Select(d => new Dictionary<string, object> {
            ["target"] = d.Target.Args(), ["input_limit"] = d.Budget }).ToArray();

    private bool HouseDoorPinned(HouseDoor d) => ReferenceEquals(Game1.getFarm(),d.Farm) && d.Farm.buildings.Contains(d.Building)
        && ReferenceEquals(d.Building.GetIndoors(), d.House) && d.Building.daysOfConstructionLeft.Value <= 0
        && !d.Building.isMoving && !Game1.player.team.demolishLock.IsLocked()
        && d.Door == new Point(d.Building.tileX.Value + d.Building.humanDoor.Value.X,
            d.Building.tileY.Value + d.Building.humanDoor.Value.Y)
        && SceneIdentity.Token(d.To.NameOrUniqueName) == d.Target.ExpectedLocationId
        && (d.Exit is null || d.House.warps.Contains(d.Exit) && !d.Exit.npcOnly.Value
            && d.Exit.X == d.Target.TileX && d.Exit.Y == d.Target.TileY
            && d.Exit.TargetName == d.Farm.NameOrUniqueName && d.Exit.TargetX == d.Door.X && d.Exit.TargetY == d.Door.Y+1);

    private bool HouseTravelPin(HouseTravel r) => this.ReflexLeaseHeld && !this.actionOwner.ReleaseRequested
        && ReferenceEquals(Game1.player,r.Player) && this.HouseDoorPinned(r.Door)
        && this.actionOwner.OwnerId == r.Request.OwnerId && this.actionOwner.Generation == r.Request.Generation
        && (this.CurrentCommandIdentity() with { LocationId=r.Identity.LocationId, ContextGeneration=r.Identity.ContextGeneration }) == r.Identity
        && !Game1.eventUp && Game1.CurrentEvent is null && !Game1.isFestival()
        && Game1.year==r.Year && Game1.currentSeason==r.Season && Game1.dayOfMonth==r.Day && Game1.timeOfDay<WorldExtras.HardStopTime
        && ReferenceEquals(r.Door.From.Map,r.Map) && r.Door.From.Map.Layers[0].LayerWidth==r.Width && r.Door.From.Map.Layers[0].LayerHeight==r.Height
        && (r.Acting || this.CurrentCommandIdentity().ContextGeneration==r.Identity.ContextGeneration)
        && (r.Presses>0 || r.Player.Position==r.Origin)
        && (ReferenceEquals(r.Player.currentLocation,r.Door.From)
            || r.Acting && ReferenceEquals(r.Player.currentLocation,r.Door.To));

    private bool HouseConsumerSafe(HouseTravel r, MenuStepCommandRequest step)
    {
        if (!this.HouseTravelPin(r))
        {
            this.AggregateHouseCleanup(r);
            return false;
        }
        // A has no walking consumer. Its exact faced door is independently pinned
        // by CaptureInteractKind. Once the intended transition starts only N/R run.
        if (r.Acting && (r.Door.Exit is null || Game1.isWarping
            || ReferenceEquals(r.Player.currentLocation,r.Door.To))) return true;
        if(this.backgroundMove?.NativeRouteMovementConsumer==false) return true;
        int direction=step.Direction switch { "up"=>0,"right"=>1,"down"=>2,"left"=>3,_=>-1 };
        float speed=r.Player.getMovementSpeed();
        if(direction<0 || !float.IsFinite(speed) || speed<0 || speed>16) return false;
        if(r.Nudging && direction!=r.Facing)return false;
        // Farmer.nextPosition uses CEILING speed, and native warp checks include
        // Right/Bottom (not Right-1/Bottom-1). Fence the whole swept footprint,
        // not the actor centre or the previous released hold's speed estimate.
        var before=r.Player.GetBoundingBox(); var next=r.Player.nextPosition(direction);
        bool safe=this.HouseFootprintSafe(r,before,next);
        if(!safe)r.FootprintStop=true;
        return safe;
    }

    private bool HouseFootprintSafe(HouseTravel r, Rectangle before, Rectangle next)
    {
        int left=Math.Min(before.X,next.X),right=Math.Max(before.X+before.Width,next.X+next.Width);
        int top=Math.Min(before.Y,next.Y),bottom=Math.Max(before.Y+before.Height,next.Y+next.Height);
        for(int y=(int)Math.Floor(top/64d);y<=(int)Math.Floor(bottom/64d);y++)
            for(int x=(int)Math.Floor(left/64d);x<=(int)Math.Floor(right/64d);x++)
            {
                bool intendedExit=r.Acting && r.Door.Exit is { } w && w.X==x && w.Y==y;
                if(intendedExit)
                {
                    if(r.Door.From.warps.Any(other=>!ReferenceEquals(other,r.Door.Exit) && other.X==x && other.Y==y)
                        || r.Door.From.doors.ContainsKey(new Point(x,y))
                        || r.Door.From.doesTileHaveProperty(x,y,"TouchAction","Back") is not null) return false;
                }
                else if(!r.Corridor.Contains(new Point(x,y)) || !this.HouseCorridorCell(r,x,y))
                    return false;
            }
        return true;
    }

    private bool HouseNativeMovementSafe(Farmer player, int direction, float dx, float dy)
    {
        // Never intercept another farmer or an ordinary movement request.
        if(this.houseTravel is not { } r)
            return !ReferenceEquals(player,this.houseNativeReleasePlayer) || this.HouseNativeReleaseSettled;
        if(!ReferenceEquals(player,r.Player)) return true;
        bool safe=false;
        try
        {
            if(!r.Stopping && r.Pending && this.HouseTravelPin(r) && NowUnixMs()<r.Deadline
                && this.backgroundMove?.NativeRouteHasH(r.Request.RequestId)==true
                && this.HouseTravelReady && direction>=0 && direction<=3
                && (!r.Acting || r.Door.Exit is not null)
                && r.Direction==new[] {"up","right","down","left"}[direction]
                && float.IsFinite(dx) && float.IsFinite(dy) && Math.Abs(dx)+Math.Abs(dy)<=16)
            {
                float speed=player.getMovementSpeed();
                if(!float.IsFinite(speed) || speed<0 || speed>16)throw new InvalidOperationException("house_native_speed");
                var before=player.GetBoundingBox(); var next=player.nextPosition(direction);
                var actual=before;
                // Enclose the float displacement even for negative coordinates.
                actual.X+=(int)(dx<0 ? Math.Floor(dx):Math.Ceiling(dx));
                actual.Y+=(int)(dy<0 ? Math.Floor(dy):Math.Ceiling(dy));
                safe=this.HouseFootprintSafe(r,before,next) && this.HouseFootprintSafe(r,before,actual);
                // Native full/half collision remains authoritative. When BOTH
                // collide, do not permit native lateral sliding outside the
                // straight corridor (Farmer.cs:8617-8654).
                if(safe && !(r.Acting && r.Door.Exit is not null)
                    && r.Door.From.isCollidingPosition(next,Game1.viewport,true,0,false,player,false,skipCollisionEffects:true)
                    && r.Door.From.isCollidingPosition(player.nextPositionHalf(direction),Game1.viewport,true,0,false,player,false,skipCollisionEffects:true))
                {
                    // At the pinned human door the previous full/half step can
                    // already have established the faced A receiver. Stop this
                    // nudge without native slide, then verify the released pose
                    // before the coordinator can arm A. This grants no movement.
                    if(r.Nudging && r.Door.Exit is null && player.FacingDirection==r.Facing
                        && player.GetGrabTile()==new Vector2(r.Door.Target.TileX,r.Door.Target.TileY))
                    { this.backgroundMove?.RequestRelease("ANIMAL_HOUSE_NATIVE_DOOR_EDGE"); return false; }
                    safe=false;
                }
            }
        }
        catch { /* A native read failure withholds this movement. */ }
        if(!safe)
        {
            r.FootprintStop=true; r.Stopping=true;
            this.backgroundMove?.RequestRelease("ANIMAL_HOUSE_NATIVE_MOVEMENT_WITHDRAWN");
        }
        return safe;
    }

    private bool HouseCorridorCell(HouseTravel r,int x,int y)
        => this.HouseCell(r.Door.From,x,y)
            // The pinned Building human door is an A receiver (Building.doAction),
            // not a walking warp. A short facing nudge may project its body into
            // that solid collision edge while native full/half collision still
            // decides actual movement. Never extend this to a native door/warp/
            // touch cell, another building, path search or an ordinary walk leg.
            || r.Nudging && r.Door.Exit is null && x==r.Door.Target.TileX && y==r.Door.Target.TileY
                && this.HouseDoorPinned(r.Door) && !r.Door.From.warps.Any(w=>w.X==x && w.Y==y)
                && !r.Door.From.doors.ContainsKey(new Point(x,y))
                && r.Door.From.doesTileHaveProperty(x,y,"TouchAction","Back") is null;

    private bool HouseCell(GameLocation loc, int x, int y)
        // Only reviewed native house/Farm receiver types. Unknown modded
        // collision overrides cannot gain authority through legacy fallback.
        => (loc.GetType() == typeof(GameLocation) || loc.GetType() == typeof(Farm) || loc.GetType() == typeof(AnimalHouse))
        && loc.isTileOnMap(new Vector2(x,y)) && NavCellAt(loc,x,y) == '.'
        && !loc.warps.Any(w=>w.X==x && w.Y==y)
        && !loc.doors.ContainsKey(new Point(x,y))
        && loc.doesTileHaveProperty(x,y,"TouchAction","Back") is null;

    private void ArmHouseTravel(MenuStepCommandRequest req)
    {
        var door = this.HouseDoors().FirstOrDefault(d => d.Target == req.InteractionTarget);
        if (door is null || this.houseTravel is not null || this.backgroundMove?.Busy != false || !this.ReflexLeaseHeld
            || NowUnixMs() >= req.DeadlineUnixMs || NowUnixMs() >= (req.ExecutionDeadlineUnixMs ?? req.DeadlineUnixMs)
            || !MenuStepCommandInbox.TryValidateInputOnceCurrentBinding(req,this.CurrentCommandIdentity(),Context.IsWorldReady,true,true,out _))
        { this.WriteCommandResult(this.commandInbox.CompleteRejected(req,CommandRejectReason.BindingMismatch,
            this.CurrentCommandIdentity(),NowUnixMs(),"ANIMAL_HOUSE_ROUTE_CHANGED")); return; }
        var r = new HouseTravel { Door=door, Player=Game1.player, Identity=this.CurrentCommandIdentity(), Request=req,
            Started=Game1.ticks, Deadline=Math.Min(req.ExecutionDeadlineUnixMs ?? req.DeadlineUnixMs,NowUnixMs()+AnimalHouseRoute.ExecutionMs),
            FaultPanics=this.backgroundMove.FestivalFaultPanics,Year=Game1.year,Season=Game1.currentSeason,Day=Game1.dayOfMonth,
            Map=door.From.Map,Width=door.From.Map.Layers[0].LayerWidth,Height=door.From.Map.Layers[0].LayerHeight,Origin=Game1.player.Position,
            Speed=float.IsFinite(Game1.player.getMovementSpeed()) ? Math.Clamp(Game1.player.getMovementSpeed(),2,12) : 8 };
        Point tile = new(door.Target.TileX,door.Target.TileY);
        var poses = EventEggPoses(tile).ToArray();
        var layer = door.From.Map.Layers[0];
        r.Search = AnimalHouseRoute.Plan(layer.LayerWidth,layer.LayerHeight,(r.Player.TilePoint.X,r.Player.TilePoint.Y),
            poses.Select(s=>(s.Stand.X,s.Stand.Y)).ToArray(), (x,y)=>this.HouseCell(door.From,x,y),
            (path,stand)=> { r.Path=path; r.Stand=new(stand.X,stand.Y); r.Planned=true;
                if(path is not null) r.Facing=poses.First(s=>s.Stand==r.Stand).Facing; }).GetEnumerator();
        this.houseTravel=r;
        this.backgroundMove.NativeRouteAuthority = step => ReferenceEquals(this.houseTravel,r) && step.RequestId==req.RequestId
            && (!this.backgroundMove.NativeRouteMovementConsumer || !r.Stopping && this.HouseConsumerSafe(r,step));
    }

    private void CheckHouseTravelBeforeRuntime()
    {
        if (this.houseTravel is not { Pending:true, Stopping:false } r) return;
        this.AggregateHouseCleanup(r);
        if (!r.Cleanup || !this.HouseTravelPin(r) || NowUnixMs()>=r.Deadline)
        { r.Stopping=true; this.backgroundMove?.RequestRelease("ANIMAL_HOUSE_ROUTE_WITHDRAWN"); }
    }

    private void TickHouseTravel()
    {
        if (this.houseTravel is not { } r) return;
        this.AggregateHouseCleanup(r);
        if (!r.Cleanup || !this.HouseTravelPin(r) || r.Stopping || NowUnixMs()>=r.Deadline
            || Game1.ticks-r.Started>=AnimalHouseRoute.MaxTicks || !r.Pending && r.Presses>=r.Door.Budget)
        {
            if (r.Pending) { r.Stopping=true; this.backgroundMove?.RequestRelease("ANIMAL_HOUSE_ROUTE_STOP"); }
            else this.FinishHouseTravel(false);
            return;
        }
        if (r.Pending || this.backgroundMove?.Busy != false) return;
        if (!r.Planned)
        {
            for(int work=0;work<FestivalMapSearch.CellsPerTick;work++)
                if(!r.Search!.MoveNext()) { r.Search.Dispose(); break; }
            return;
        }
        if(r.Path is null) { r.Faults.Add("unreachable_house:"+r.Door.Target.ExpectedLocationId); this.FinishHouseTravel(false); return; }
        var box=r.Player.GetBoundingBox();
        Vector2 centre=new(box.X+box.Width/2,box.Y+box.Height/2);
        // A verified straight hold may reach several path cells. Retire exactly
        // its observed endpoint, never an inferred intermediate arrival.
        if(r.Index<r.Path.Count)
            for(int i=r.Index;i<r.Path.Count;i++)
            {
                var at=r.Path[i];
                if(r.Player.TilePoint==new Point(at.X,at.Y))
                {
                    // A capped hold can stop inside a checked cell before its
                    // centre. Keep that observed cell as the centring goal;
                    // never walk back to a retired prefix of the corridor.
                    r.Index=i;
                    bool straight=i+1<r.Path.Count && i>0
                        && r.Path[i+1].X-at.X==at.X-r.Path[i-1].X
                        && r.Path[i+1].Y-at.Y==at.Y-r.Path[i-1].Y;
                    if(straight || Math.Abs(centre.X-(at.X*64+32))<=8 && Math.Abs(centre.Y-(at.Y*64+32))<=8)r.Index++;
                    break;
                }
            }
        Point targetTile=new(r.Door.Target.TileX,r.Door.Target.TileY);
        bool atStand=r.Index==r.Path.Count && r.Player.TilePoint==r.Stand;
        Vector2 delta;
        r.Corridor=new();
        r.Nudging=false;
        r.Corridor.Add(r.Player.TilePoint);
        // A released leg can leave the bounding box straddling its last cell.
        // Pin that already-occupied, safe fringe before choosing the next leg;
        // it grants no new forward cells beyond this leg's checked path.
        for(int y=(int)Math.Floor(box.Y/64d);y<=(int)Math.Floor((box.Y+box.Height)/64d);y++)
            for(int x=(int)Math.Floor(box.X/64d);x<=(int)Math.Floor((box.X+box.Width)/64d);x++)
                r.Corridor.Add(new Point(x,y));
        if(atStand && (r.Facing%2==0 ? Math.Abs(centre.X-(r.Stand.X*64+32)) : Math.Abs(centre.Y-(r.Stand.Y*64+32)))<=8)
        {
            if(r.Door.Exit is not null)
            {
                var capture=this.CaptureMoveThroughTarget();
                if(capture.Target is not { } through || capture.Expectation is null || through.SourceFactId!=r.Door.Target.SourceFactId
                    || through.ExpectedLocationId!=r.Door.Target.ExpectedLocationId) { this.FinishHouseTravel(false); return; }
                var inner=r.Request with { Primitive=MoveThroughTarget.Primitive, InteractionTarget=null,
                    ThroughTarget=through with { Ticks=22 }, Direction=through.Direction,Ticks=22,DeadlineUnixMs=r.Deadline };
                r.Acting=true; r.Presses++; r.Before=r.Player.Position; r.BeforeTile=r.Player.TilePoint;
                r.Waypoint=targetTile; r.Ticks=22; r.Direction=through.Direction; r.FootprintStop=false;
                var refused=this.backgroundMove.TryArmThrough(inner,capture.Expectation);
                r.Pending=refused is null; if(refused is not null)this.RouteHouseTravelTerminal(refused); return;
            }
            if(r.Player.FacingDirection==r.Facing && r.Player.GetGrabTile()==targetTile.ToVector2())
            {
                var capture=this.CaptureInteractKind(r.Player,targetTile.ToVector2());
                if(capture.Target is not { Kind:WorldInteractionKinds.Door } faced || capture.Action is null
                    || faced.SourceFactId!=r.Door.Target.SourceFactId || faced.ExpectedLocationId!=r.Door.Target.ExpectedLocationId)
                { this.FinishHouseTravel(false); return; }
                r.Acting=true; r.Presses++; r.Before=r.Player.Position; r.BeforeTile=r.Player.TilePoint;
                r.Waypoint=targetTile; r.Ticks=1; r.FootprintStop=false;
                var refused=this.backgroundMove.TryArmHoldTarget(r.Request with { InteractionTarget=faced,Ticks=1,DeadlineUnixMs=r.Deadline },BgHoldKind.Action,capture.Action);
                r.Pending=refused is null; if(refused is not null)this.RouteHouseTravelTerminal(refused); return;
            }
            var (dx,dy)=FishCastGeometry.FacingDelta(r.Facing); delta=new(dx*8,dy*8);
            r.Waypoint=r.Stand; r.Corridor.Add(r.Stand);
            r.Nudging=true;
            if(r.Door.Exit is null)r.Corridor.Add(targetTile);
        }
        else
        {
            int end=r.Index;
            if(end<r.Path.Count)
            {
                var first=r.Path[end]; var prior=r.Player.TilePoint;
                int dx=first.X-prior.X,dy=first.Y-prior.Y;
                // Check a full H-frame corridor, including the partial endpoint.
                // Only the first leg calibrates over one cell. Intermediate cells
                // need observed traversal, not an extra centring publication.
                int cells=r.Trace.Count==0 ? 1 : (int)Math.Ceiling(r.Speed*60/64)+1;
                while(end+1<r.Path.Count && end-r.Index+1<cells && r.Path[end+1].X-r.Path[end].X==dx && r.Path[end+1].Y-r.Path[end].Y==dy) end++;
                for(int i=r.Index;i<=end;i++)r.Corridor.Add(new(r.Path[i].X,r.Path[i].Y));
                r.Waypoint=new(r.Path[end].X,r.Path[end].Y);
            }
            else { r.Waypoint=r.Stand; r.Corridor.Add(r.Stand); }
            delta=new(r.Waypoint.X*64+32-centre.X,r.Waypoint.Y*64+32-centre.Y);
        }
        if(r.Corridor.Any(p=>!this.HouseCorridorCell(r,p.X,p.Y))) { this.FinishHouseTravel(false); return; }
        r.Direction=(r.Nudging ? r.Facing%2==1 : Math.Abs(delta.X)>8) ? delta.X>0 ? "right":"left" : delta.Y>0 ? "down":"up";
        float distance=Math.Abs(delta.X)>8 ? Math.Abs(delta.X) : Math.Abs(delta.Y);
        r.Ticks=Math.Clamp((int)Math.Round(distance/r.Speed),1,60); r.Before=r.Player.Position; r.BeforeTile=r.Player.TilePoint;
        r.FootprintStop=false;
        // Size the hold within the footprint too. This avoids deliberately
        // arming an extra nudge past a door/turn and needing an uncertain release.
        // The native-consumer fence remains authoritative if speed later changes.
        float nativeSpeed=r.Player.getMovementSpeed();
        if(!float.IsFinite(nativeSpeed) || nativeSpeed<=0 || nativeSpeed>16) { this.FinishHouseTravel(false); return; }
        for(;r.Ticks>0;r.Ticks--)
        {
            var projected=box;
            int reach=(int)Math.Ceiling((r.Ticks-1)*nativeSpeed)+(int)Math.Ceiling(nativeSpeed);
            switch(r.Direction) { case "up":projected.Y-=reach;break;case "right":projected.X+=reach;break;
                case "down":projected.Y+=reach;break;case "left":projected.X-=reach;break; }
            if(this.HouseFootprintSafe(r,box,projected))break;
        }
        if(r.Ticks==0) { this.FinishHouseTravel(false); return; }
        var step=r.Request with { Primitive=MenuStepCommandInbox.PrimitiveMoveCardinal,InteractionTarget=null,
            Direction=r.Direction,Ticks=r.Ticks,DeadlineUnixMs=r.Deadline };
        r.Presses++; var rejection=this.backgroundMove.TryArm(step); r.Pending=rejection is null;
        if(rejection is not null)this.RouteHouseTravelTerminal(rejection);
    }

    private bool RouteHouseTravelTerminal(BackgroundMoveTerminal? terminal)
    {
        if(terminal is null || this.houseTravel is not { } r || terminal.Request.RequestId!=r.Request.RequestId)return false;
        r.Pending=false; this.AggregateHouseCleanup(r);
        bool clean=terminal.Hold?.Outcome.CleanupConfirmed
            ?? (terminal.Kind is BackgroundTerminalKind.Completed or BackgroundTerminalKind.NotCompleted
                || terminal.Label.StartsWith("NOT_COMPLETED:RELEASED_EARLY",StringComparison.Ordinal)
                || terminal.ConsumedH==0 && terminal.Label.StartsWith("RELEASED_BEFORE_LATCH:",StringComparison.Ordinal));
        r.Cleanup &= clean && this.backgroundMove?.PressOverrideOutstanding!=true;
        Vector2 delta=r.Player.Position-r.Before;
        bool success=r.Acting
            ? (r.Door.Exit is null ? terminal.Hold?.Outcome is { Opened:true,CleanupConfirmed:true,Outcome:"LocationChanged" }
                : terminal.Kind==BackgroundTerminalKind.Completed && terminal.Label==BgMoveThrough.Warped)
                && ReferenceEquals(r.Player.currentLocation,r.Door.To) && this.HouseTravelPin(r)
                && this.ReflexLeaseHeld && !Game1.isWarping && Context.IsPlayerFree && Context.CanPlayerMove
                && Game1.activeClickableMenu is null
            : terminal.Kind==BackgroundTerminalKind.Completed && AnimalHouseRoute.Step(r.Cleanup,this.HouseTravelPin(r),r.Direction,r.Ticks,delta.X,delta.Y)
                && (r.Player.TilePoint==r.BeforeTile || r.Corridor.Contains(r.Player.TilePoint));
        r.Trace.Add(new() { ["kind"]=r.Acting ? r.Door.Exit is null ? "A":"exit":r.Direction,["verified"]=success,
            ["cleanup"]=clean,["ticks"]=r.Ticks,["consumed_h"]=terminal.ConsumedH,["footprint_stop"]=r.FootprintStop,["label"]=terminal.Label,
            ["before_pixel"]=new[] {r.Before.X,r.Before.Y},["after_pixel"]=new[] {r.Player.Position.X,r.Player.Position.Y},
            ["waypoint"]=new[] {r.Waypoint.X,r.Waypoint.Y},["path_index"]=r.Index });
        if(!r.Acting && success && terminal.ConsumedH>0)
            r.Speed=Math.Clamp((Math.Abs(delta.X)+Math.Abs(delta.Y))/terminal.ConsumedH,2,12);
        if(r.Acting || !success || r.Stopping)this.FinishHouseTravel(success && !r.Stopping);
        return true;
    }

    private void AggregateHouseCleanup(HouseTravel r)
    {
        void Fault(string why) { r.Cleanup=false; if(!r.Faults.Contains(why))r.Faults.Add(why); }
        if(!ReferenceEquals(r.Player.currentLocation,r.Door.From)
            && !(r.Acting && ReferenceEquals(r.Player.currentLocation,r.Door.To)))Fault("unplanned_location_change");
        if(this.padAdapter?.CleanupFault is { } fault)Fault("adapter_cleanup_fault:"+fault);
        if(this.backgroundMove?.FestivalContainmentFault is { } containment)Fault("containment:"+containment);
        if(this.backgroundMove?.FestivalFaultPanics!=r.FaultPanics)Fault("pad_panic:"+this.backgroundMove?.FestivalLastFaultPanicReason);
        if(this.padAdapter?.RestoreWritePending==true)Fault("pad_restore_pending");
    }
    private void FinishHouseTravel(bool success)
    {
        if(this.houseTravel is not { } r)return;
        this.AggregateHouseCleanup(r); r.Search?.Dispose();
        // Skipping an input callback does not erase movementDirections. Keep a
        // veto after a terminal until native neutral input actually drains it.
        // No direct Halt/direction write and no automatic retry or new owner.
        if(r.Player.movementDirections.Count!=0) { this.houseNativeReleasePlayer=r.Player; r.Cleanup=false; }
        r.Cleanup &= this.backgroundMove?.Busy==false && this.backgroundMove?.PressOverrideOutstanding!=true;
        success &= r.Cleanup && r.Trace.Count>0 && r.Trace.All(t=>(bool)t["verified"] && (bool)t["cleanup"]);
        bool movementOnly = !success && !r.Pending && r.Faults.Count==0
            && AnimalHouseRoute.MovementOnly(r.Cleanup, this.HouseTravelPin(r),
                r.Acting, r.Stopping, r.Presses,
                r.Trace.Select(t=>((string)t["kind"], (bool)t["verified"], (bool)t["cleanup"])).ToArray());
        this.Monitor.Log("ANIMAL_HOUSE_ROUTE " + System.Text.Json.JsonSerializer.Serialize(new {
            request_id=r.Request.RequestId,from=r.Door.From.NameOrUniqueName,to=r.Door.To.NameOrUniqueName,
            source_fact_id=r.Door.Target.SourceFactId,budget=r.Door.Budget,inputs=r.Presses,
            verified=success,cleanup=r.Cleanup,faults=r.Faults,trace=r.Trace }),
            success ? LogLevel.Info : LogLevel.Warn);
        this.houseTravel=null; if(this.backgroundMove is not null)this.backgroundMove.NativeRouteAuthority=null;
        this.WriteCommandResult(this.commandInbox.CompleteWorldInteraction(new WorldInteractionVerdict(
            success ? "LocationChanged":movementOnly ? "InteractionNoEffect":r.Presses==0 && r.Cleanup ? "InteractionRejected":"InteractionUncertain",success,r.Cleanup,r.Presses,null,null,
            ResultLocationId:success ? r.Door.Target.ExpectedLocationId:null,
            EffectDetail:success ? r.Door.Target.SourceFactId+"|verified_house_route":movementOnly ? "house_route_movement_only":string.Join("|",r.Faults)),
            this.CurrentCommandIdentity(),NowUnixMs()));
    }
    private void ResetHouseTravelForTitle()
    {
        this.houseBudgetPose=null;
        this.houseNativeReleasePlayer=null;
        this.houseTravel?.Search?.Dispose();this.houseTravel=null;
        if(this.backgroundMove is not null)this.backgroundMove.NativeRouteAuthority=null;
    }
    private void AbortHouseTravelForSave()
    { if(this.houseTravel is { } r) { r.Stopping=true; r.Cleanup=false; r.Deadline=0; this.backgroundMove?.RequestRelease("ANIMAL_HOUSE_ROUTE_SAVE"); } }
}
