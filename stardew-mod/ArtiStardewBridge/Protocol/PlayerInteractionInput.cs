using System.Text.Json;

namespace ArtiStardewBridge.Protocol;

/// <summary>
/// One observed action-button target. WS3a (2026-10-07): <see cref="Kind"/> is mandatory on the
/// wire (chest | door | forage); a door also carries the expected destination location token.
/// The DLL and the runtime ship together, so there is no kind-less legacy form.
/// WS3b extension point: add "npc" to <see cref="WorldInteractionKinds.Supported"/> (8 keys, no expectation).
/// </summary>
public readonly record struct WorldInteractionTarget(string TargetToken, string SourceFactId,
    int TileX, int TileY, int ActorTileX, int ActorTileY, int Facing,
    string Kind = WorldInteractionKinds.Chest, string? ExpectedLocationId = null)
{
    public const string Primitive = "world_interact";
    static readonly string[] BaseKeys = { "target_token", "source_fact_id", "tile_x", "tile_y", "actor_tile_x", "actor_tile_y", "facing", "kind" };
    public const string ExpectedLocationKey = WorldInteractionKinds.ExpectedLocationKey;
    public static bool CanPress(WorldInteractionTarget wanted, WorldInteractionTarget? current,
        CommandInboxIdentity identity, bool enabled, bool snappyMenus, bool lease, bool mapped,
        int effectiveX, int effectiveY) => enabled && !snappyMenus && lease && mapped
        && !identity.IsMainPlayer && !identity.IsSplitScreen
        && wanted.IsValid && current==wanted
        && effectiveX==wanted.TileX && effectiveY==wanted.TileY;
    public bool IsValid => !string.IsNullOrWhiteSpace(TargetToken) && TargetToken.Length <= 128
        && !string.IsNullOrWhiteSpace(SourceFactId) && SourceFactId.Length <= 256
        && TileX is >= 0 and <= 4096 && TileY is >= 0 and <= 4096
        && ActorTileX is >= 0 and <= 4096 && ActorTileY is >= 0 and <= 4096
        && Facing is >= 0 and <= 3 && (GenericWorld.IsKind(Kind) || Kind == WorldExtras.HarvestCrop
            || Kind == EventControlAction.Stand // seated, the grab tile can be her own tile
            ? Math.Abs(TileX-ActorTileX)+Math.Abs(TileY-ActorTileY) <= 1
            : Kind == AnimalHouseRoute.Kind || EventControlAction.IsApproach(Kind) || (Kind == EventControlAction.EggRoute
            ? Math.Abs(TileX-ActorTileX)+Math.Abs(TileY-ActorTileY) <= EventControlAction.MaxRouteTiles + 1
            : Math.Abs(TileX-ActorTileX)+Math.Abs(TileY-ActorTileY)==1))
        && WorldInteractionKinds.IsSupported(Kind)
        && (WorldInteractionKinds.RequiresExpectedLocation(Kind)
            ? WorldInteractionKinds.IsLocationToken(ExpectedLocationId)
            : ExpectedLocationId is null);
    /// <summary>The single success outcome the mod may report for this kind.</summary>
    public string ExpectedOutcome => WorldInteractionKinds.ExpectedOutcome(Kind);
    public Dictionary<string, object> Args()
    {
        var args = new Dictionary<string, object> {
            ["target_token"]=TargetToken, ["source_fact_id"]=SourceFactId, ["tile_x"]=TileX, ["tile_y"]=TileY,
            ["actor_tile_x"]=ActorTileX, ["actor_tile_y"]=ActorTileY, ["facing"]=Facing, ["kind"]=Kind };
        if (ExpectedLocationId is { } expected) args[ExpectedLocationKey]=expected;
        return args;
    }
    public static bool TryParse(JsonElement args, out WorldInteractionTarget target)
    {
        target = default;
        if (args.ValueKind != JsonValueKind.Object
            || !args.TryGetProperty("kind", out JsonElement kindValue) || kindValue.ValueKind != JsonValueKind.String
            || kindValue.GetString() is not { } kind || !WorldInteractionKinds.IsSupported(kind)) return false;
        bool expectation = WorldInteractionKinds.RequiresExpectedLocation(kind);
        string[] keys = expectation ? BaseKeys.Append(ExpectedLocationKey).ToArray() : BaseKeys;
        if (args.EnumerateObject().Count()!=keys.Length
            || args.EnumerateObject().Select(p=>p.Name).Distinct().Count()!=keys.Length
            || args.EnumerateObject().Any(p=>!keys.Contains(p.Name))) return false;
        if (args.GetProperty("target_token").ValueKind!=JsonValueKind.String
            || args.GetProperty("source_fact_id").ValueKind!=JsonValueKind.String) return false;
        string? expected = null;
        if (expectation)
        {
            if (args.GetProperty(ExpectedLocationKey).ValueKind!=JsonValueKind.String) return false;
            expected = args.GetProperty(ExpectedLocationKey).GetString();
        }
        int[] n = new int[5];
        for (int i=0;i<5;i++)
            if (args.GetProperty(keys[i+2]).ValueKind!=JsonValueKind.Number
                || !args.GetProperty(keys[i+2]).TryGetInt32(out n[i])) return false;
        target = new(args.GetProperty(keys[0]).GetString()!,args.GetProperty(keys[1]).GetString()!,n[0],n[1],n[2],n[3],n[4],
            kind, expected);
        return target.IsValid;
    }
}

/// <summary>Finite receipt for one existing input_once. The existing native owner,
/// inbox, update observer and journal remain authoritative. No retry or game API here.</summary>
public sealed class WorldInteractionInput
{
    readonly CommandInboxIdentity pin;
    readonly long deadline;
    uint? lastTick, pressTick, suppressTick;
    uint tick;
    long gameTick;
    bool updateOpen, updateValid, gameSucceeded, suppressAttempted, suppressReturned, fault, started;
    int gameUpdates;
    string? receiver;
    long receiverGeneration;
    public string Button { get; }
    public string Fingerprint { get; }
    public bool PressAttempted { get; private set; }
    public bool CleanupConfirmed { get; private set; }
    public bool Busy => Verdict is null || (PressAttempted && !CleanupConfirmed);
    public WorldInteractionVerdict? Verdict { get; private set; }

    public WorldInteractionInput(CommandInboxIdentity pin, string button, string fingerprint, long deadline)
    { this.pin=pin; Button=button; Fingerprint=fingerprint; this.deadline=deadline; }

    bool SameActor(CommandInboxIdentity current) => current == this.pin with { ContextGeneration=current.ContextGeneration };

    public void BeginUpdate(uint tick, long gameTick, CommandInboxIdentity current, bool eligible)
    {
        if (this.updateOpen || (this.lastTick is uint last && (last==uint.MaxValue || tick!=last+1))) this.fault=true;
        this.tick=tick; this.gameTick=gameTick; this.gameUpdates=0; this.gameSucceeded=false;
        this.updateOpen=true; this.updateValid=eligible && this.SameActor(current);
        if (!this.updateValid) this.fault=true;
    }

    public void Tick(long now, bool admitted, Action press, Action suppress)
    {
        if (Verdict is not null && !Busy) return;
        if (!this.started)
        {
            this.started=true;
            if (this.fault || !admitted || now>=this.deadline) {
                CleanupConfirmed=true;
                Verdict=new("InteractionRejected",false,true,0,null,null);
                return;
            }
            PressAttempted=true; this.pressTick=this.tick;
            try { press(); } catch { this.fault=true; }
        }
        else if (PressAttempted && !this.suppressAttempted)
        {
            this.suppressAttempted=true; this.suppressTick=this.tick;
            try { suppress(); this.suppressReturned=true; } catch { this.fault=true; }
        }
        if (!admitted) this.fault=true;
    }

    public void ObserveGameUpdate(long gameTick, CommandInboxIdentity current, bool eligible, bool succeeded)
    {
        if (!this.updateOpen) { this.fault=true; return; }
        this.gameUpdates++;
        this.gameSucceeded=this.updateValid && eligible && succeeded && this.SameActor(current)
            && this.gameUpdates==1 && gameTick>this.gameTick;
        if (!this.gameSucceeded) this.fault=true;
    }

    public void Interrupt() => this.fault=true;

    // observedReceiver is from the actual menu and must identify the originally
    // pinned chest object. One generation advance is allowed; never receiver handoff.
    public void AfterUpdate(uint tick, long now, CommandInboxIdentity current, bool eligible,
        bool? pinnedButtonDown, string? observedReceiver, bool matchingChest)
    {
        if (!PressAttempted && Verdict is not null) return;
        bool valid=this.updateOpen && tick==this.tick && this.gameSucceeded
            && this.gameUpdates==1 && eligible && this.SameActor(current);
        this.updateOpen=false; this.lastTick=tick;
        if (!valid) this.fault=true;
        if (observedReceiver is not null)
        {
            if (!matchingChest || !PressAttempted || current.ContextGeneration!=this.pin.ContextGeneration+1
                || (this.receiver is not null && this.receiver!=observedReceiver)) this.fault=true;
            else { this.receiver=observedReceiver; this.receiverGeneration=current.ContextGeneration; }
        }
        else if (this.receiver is not null || current.ContextGeneration!=this.pin.ContextGeneration) this.fault=true;
        if (this.suppressReturned && this.suppressTick is uint suppressed && tick>suppressed && valid && pinnedButtonDown==false)
            CleanupConfirmed=true;
        bool expired=now>=this.deadline || (this.pressTick is uint pressed && tick-pressed>=8);
        if (CleanupConfirmed && (this.fault || this.receiver is not null || expired))
            Verdict=new(this.fault || this.receiver is null ? "InteractionUncertain" : "ContainerOpened",
                !this.fault && this.receiver is not null, true, PressAttempted?1:0,
                this.receiver,this.receiver is null ? null : this.receiverGeneration);
        else if (expired && Verdict is null)
            Verdict=new("InteractionUncertain",false,CleanupConfirmed,PressAttempted?1:0,this.receiver,
                this.receiver is null ? null : this.receiverGeneration);
    }
}

/// <summary>Opened = the kind's success outcome was observed (ContainerOpened / LocationChanged / ItemPickedUp).</summary>
public sealed record WorldInteractionVerdict(string Outcome, bool Opened, bool CleanupConfirmed,
    int PressCount, string? ReceiverToken, long? ResultContextGeneration,
    string? ResultLocationId = null, string? EffectDetail = null);

public readonly record struct ContainerTransferTarget(string ReceiverToken, string ContainerToken,
    string ReadbackToken, string SourceFactId, string TargetToken, int Slot,
    string QualifiedItemId, int Stack, int Quality, int OwnerCeiling, int? Quantity=null, string? Mode=null)
{
    public const string Primitive="container_transfer";
    // Primitives v2 WS-G2 modes (EconomyMenusEnabled; v1 = no mode = put the whole stack of wood/stone, unchanged):
    //   quick      = chest put of the whole stack with the vanilla shift-click modifier (oldKBState LeftShift, one block);
    //   take_out   = chest -> player: the whole stack of CONTAINER slot `slot` (ItemGrabMenu.cs:972-1003, Chest.grabItemFromChest);
    //   ship_one   = shipping bin: right click on a player slot ships ONE (ItemGrabMenu.cs:801-861, ShippingBin.cs:158-171, 267-270);
    //   ship_stack = shipping bin: left click ships the whole stack (ItemGrabMenu.cs:996-1006).
    public const string ModeQuick="quick", ModeTakeOut="take_out", ModeShipOne="ship_one", ModeShipStack="ship_stack";
    public static readonly string[] Modes={ModeQuick,ModeTakeOut,ModeShipOne,ModeShipStack};
    public int Amount => Mode==ModeShipOne ? 1 : Quantity ?? Stack;
    public bool IsShip => Mode is ModeShipOne or ModeShipStack;
    public bool IsTakeOut => Mode==ModeTakeOut;
    public bool IsValid => Mode is null ? IsV1Valid : IsModeValid;
    bool IsModeValid => Modes.Contains(Mode) && Quantity is null
        && Slot>=0 && Slot<(Mode==ModeTakeOut ? 72 : 48) && Stack is >0 and <=999 && Quality is 0 or 1 or 2 or 4
        && Amount<=OwnerCeiling && OwnerCeiling is >0 and <=999
        && !string.IsNullOrWhiteSpace(QualifiedItemId) && QualifiedItemId.Length<=128
        && !string.IsNullOrWhiteSpace(ReceiverToken) && ReceiverToken.Length<=128
        && !string.IsNullOrWhiteSpace(TargetToken) && TargetToken.Length<=128
        && !string.IsNullOrWhiteSpace(ContainerToken) && ContainerToken.Length<=128
        && !string.IsNullOrWhiteSpace(ReadbackToken) && ReadbackToken.Length<=128
        && !string.IsNullOrWhiteSpace(SourceFactId) && SourceFactId.Length<=512;
    bool IsV1Valid => new SelectItemTarget(ReceiverToken,TargetToken,Slot,QualifiedItemId,Stack,Quality).IsValid
        && QualifiedItemId is "(O)388" or "(O)390" && Quality==0 && Stack<=999 && Amount<=OwnerCeiling && OwnerCeiling is >0 and <=999
        && (Quantity is null || Quantity==1 && Stack>1)
        && !string.IsNullOrWhiteSpace(ContainerToken) && ContainerToken.Length<=128
        && !string.IsNullOrWhiteSpace(ReadbackToken) && ReadbackToken.Length<=128
        && !string.IsNullOrWhiteSpace(SourceFactId) && SourceFactId.Length<=512;
    public Dictionary<string,object> Args() {
        var args=new Dictionary<string,object> {
            ["receiver_token"]=ReceiverToken,["container_token"]=ContainerToken,["readback_token"]=ReadbackToken,
            ["source_fact_id"]=SourceFactId,["target_token"]=TargetToken,["slot"]=Slot,
            ["qualified_item_id"]=QualifiedItemId,["stack"]=Stack,["quality"]=Quality,["owner_ceiling"]=OwnerCeiling };
        if(Quantity is { } quantity)args["quantity"]=quantity;
        if(Mode is { } mode)args["mode"]=mode;
        return args;
    }
    public static bool TryParse(JsonElement args,out ContainerTransferTarget target)
    {
        target=default;
        string[] texts={"receiver_token","container_token","readback_token","source_fact_id","target_token","qualified_item_id"};
        string[] ints={"slot","stack","quality","owner_ceiling"};
        if(args.ValueKind!=JsonValueKind.Object)return false;
        bool single=args.TryGetProperty("quantity",out var quantity);
        bool moded=args.TryGetProperty("mode",out var modeValue);
        int count=10+(single?1:0)+(moded?1:0);
        if(args.EnumerateObject().Count()!=count
            || args.EnumerateObject().Select(p=>p.Name).Distinct().Count()!=count
            || args.EnumerateObject().Any(p=>!texts.Contains(p.Name)&&!ints.Contains(p.Name)&&p.Name!="quantity"&&p.Name!="mode")
            || single && (quantity.ValueKind!=JsonValueKind.Number || !quantity.TryGetInt32(out int q) || q!=1)
            || moded && (modeValue.ValueKind!=JsonValueKind.String || !Modes.Contains(modeValue.GetString())))return false;
        if(texts.Any(k=>args.GetProperty(k).ValueKind!=JsonValueKind.String))return false;
        var n=new int[4];
        for(int i=0;i<4;i++)if(args.GetProperty(ints[i]).ValueKind!=JsonValueKind.Number
            || !args.GetProperty(ints[i]).TryGetInt32(out n[i]))return false;
        target=new(args.GetProperty(texts[0]).GetString()!,args.GetProperty(texts[1]).GetString()!,
            args.GetProperty(texts[2]).GetString()!,args.GetProperty(texts[3]).GetString()!,
            args.GetProperty(texts[4]).GetString()!,n[0],args.GetProperty(texts[5]).GetString()!,n[1],n[2],n[3],single?1:null,
            moded?modeValue.GetString():null);
        return target.IsValid;
    }
}

// Immutable views for one existing inbox request. Not a state store or input backend.
public sealed record ContainerTransferReadback(string ReceiverToken,string ContainerToken,string SourceFactId,
    string ReadbackToken,long Generation,int ActorCount,int ContainerCount,bool CursorClear,bool SourceEmpty, int? SourceStack=null)
{
    public static ContainerTransferReadback? Read(JsonElement facts,ContainerTransferTarget wanted,long generation,
        out ContainerTransferTarget? current,out int capacity)
    {
        current=null;capacity=0;
        if(facts.GetProperty("available").ValueKind!=JsonValueKind.True)return null;
        var actor=facts.GetProperty("actor_slots").EnumerateArray().ToArray();
        var container=facts.GetProperty("container_slots").EnumerateArray().ToArray();
        if(actor.Length is <1 or >48 || container.Length is <1 or >72)return null;
        bool Match(JsonElement item)=>item.ValueKind==JsonValueKind.Object
            && item.GetProperty("qualified_item_id").GetString()==wanted.QualifiedItemId
            && item.GetProperty("quality").GetInt32()==wanted.Quality;
        int Count(JsonElement[] slots)=>slots.Sum(s=>Match(s.GetProperty("item"))?s.GetProperty("item").GetProperty("stack").GetInt32():0);
        // WS-G2 take_out: the source is the CONTAINER slot and the capacity is the actor's.
        bool takeOut=wanted.IsTakeOut;
        var source=(takeOut?container:actor).SingleOrDefault(s=>s.GetProperty("slot").GetInt32()==wanted.Slot);
        if(source.ValueKind==JsonValueKind.Undefined)return null;
        string Text(string k)=>facts.GetProperty(k).GetString()!;
        var item=source.GetProperty("item");
        if(Match(item)) {
            current=new(Text("receiver_token"),Text("container_token"),Text("readback_token"),Text("source_fact_id"),
                source.GetProperty("target_token").GetString()!,wanted.Slot,wanted.QualifiedItemId,
                item.GetProperty("stack").GetInt32(),wanted.Quality,wanted.OwnerCeiling,wanted.Quantity,wanted.Mode);
            if(item.TryGetProperty("max_stack",out var maximum) && maximum.TryGetInt32(out int max) && max is >0 and <=999) {
                // PM-1 fix10d: a locked backpack slot (slot >= actor_usable) is no room for a take_out.
                int usable=takeOut && facts.TryGetProperty("actor_usable",out var u) && u.TryGetInt32(out int n) ? n : int.MaxValue;
                foreach(var slot in takeOut?actor:container) {
                    if(slot.GetProperty("slot").GetInt32()>=usable)continue;
                    var dest=slot.GetProperty("item");
                    if(dest.ValueKind==JsonValueKind.Null)capacity+=max;
                    else if(Match(dest) && dest.TryGetProperty("max_stack",out var dm) && dm.GetInt32()==max)
                        capacity+=Math.Max(0,max-dest.GetProperty("stack").GetInt32());
                }
            }
        }
        return new(Text("receiver_token"),Text("container_token"),Text("source_fact_id"),Text("readback_token"),
            generation,Count(actor),Count(container),facts.GetProperty("cursor").ValueKind==JsonValueKind.Null,
            item.ValueKind==JsonValueKind.Null, Match(item)?item.GetProperty("stack").GetInt32():item.ValueKind==JsonValueKind.Null?0:null);
    }
}
public sealed record ContainerTransferVerdict(string Outcome,bool Paired,bool CleanupConfirmed,int ClickCount,
    bool ExpectedTransition,string? ReceiverToken,long? ResultContextGeneration,string? ReadbackToken,
    string? Detail=null,bool NoTransitionExpected=false);

public sealed class ContainerTransferInput
{
    bool duringClick,fault,consumed,returning,returnFault;
    int transitions,returnTransitions;
    string? originalReceiver,resultReceiver;
    public void ObserveReceiverTransition(string? from,string? to,bool sameChest,bool originalIdentity,bool succeeded)
    {
        if(returning) {   // PM-1 fix10d: the arm's own put-back replaces the chest menu once (Chest.grabItemFromInventory)
            returnTransitions++;
            if(returnTransitions!=1 || to is null || to==from || !sameChest || !originalIdentity || !succeeded)returnFault=true;
            return;
        }
        transitions++;
        if(!duringClick || transitions!=1 || from!=originalReceiver || to is null || to==from
            || !sameChest || !originalIdentity || !succeeded)fault=true;
        resultReceiver=to;
    }
    public ContainerTransferVerdict Execute(ContainerTransferTarget wanted,ContainerTransferTarget? current,
        ContainerTransferReadback before,bool eligible,bool snappyMenus,bool snappedComponentNull,int capacity,
        Action click,Action release,Func<ContainerTransferReadback?> read,bool singleItemReady=false,Action? returnCursor=null)
    {
        if(consumed) return new("TransferRejected",false,true,0,false,null,null,null);
        consumed=true;
        if(!eligible || snappyMenus || !snappedComponentNull || !wanted.IsValid || current!=wanted
            || wanted.Quantity is not null && !singleItemReady
            || capacity<wanted.Amount || !before.CursorClear || before.ReceiverToken!=wanted.ReceiverToken
            || before.ContainerToken!=wanted.ContainerToken || before.SourceFactId!=wanted.SourceFactId
            || before.ReadbackToken!=wanted.ReadbackToken
            || (wanted.IsTakeOut ? before.ContainerCount<wanted.Stack : before.ActorCount<wanted.Stack)
            || wanted.IsShip)
            return new("TransferRejected",false,true,0,false,null,null,null);
        originalReceiver=wanted.ReceiverToken;
        bool clean=false; ContainerTransferReadback? first=null,last=null; string? why=null;
        static string Short(Exception ex)=>ex.Message.Length<=80?ex.Message:ex.Message[..80];
        try {
            duringClick=true;
            try { click(); } finally { duringClick=false; }
            first=read();
        } catch(Exception ex) { fault=true; why="click:"+Short(ex); }
        finally {
            try { release();clean=true; } catch(Exception ex) { fault=true; why??="release:"+Short(ex); }
        }
        try { last=read(); } catch(Exception ex) { fault=true; why??="read:"+Short(ex); }
        bool transition=!fault && transitions==1 && first is not null && last==first
            && first.ReceiverToken==resultReceiver && first.Generation==before.Generation+1
            && first.ContainerToken==before.ContainerToken && first.SourceFactId==before.SourceFactId
            && first.ReadbackToken!=before.ReadbackToken;
        // take_out: Chest.clearNulls compacts the chest list after the grab (Chest.cs:857-865), so the source slot index
        // may hold the next item: the source term is the paired container count instead.
        bool source=first is not null && (wanted.IsTakeOut || (wanted.Quantity is null ? first.SourceEmpty
            : first.SourceStack==wanted.Stack-wanted.Amount && !first.SourceEmpty));
        int sign=wanted.IsTakeOut?-1:1;
        bool paired=transition && clean && first!.CursorClear && source
            && sign*(before.ActorCount-first.ActorCount)==wanted.Amount
            && sign*(first.ContainerCount-before.ContainerCount)==wanted.Amount;
        // PM-1 a7 #3 (N:2603, 9736, 19565): the live result named no failing term. Name the first one (no fix by guess).
        why??=paired?null:fault?"transition_fault:"+transitions:first is null?"first_read_unavailable"
            :last!=first?"read_unstable":first.ReceiverToken!=resultReceiver?"receiver_mismatch"
            :first.Generation!=before.Generation+1?$"generation:{before.Generation}->{first.Generation}"
            :!transition?"container_identity":!first.CursorClear?"cursor_held":!source?"source_slot":"counts_unpaired";
        // PM-1 fix10d (a13 d4:J2319, N:43478-43480): a take_out into a full backpack left the grabbed item on the menu's
        // cursor (ItemGrabMenu.receiveLeftClick: addItemToInventoryBool failed, heldItem kept). Base called that a clean
        // release, so the chest stayed open with nothing admissible (close and transfers refuse a cursor item) while
        // input read released. A cursor item after the click is native held evidence, never a confirmed cleanup; the
        // arm returns its own item with one put-back click (vanilla Chest.grabItemFromInventory, lossless) and
        // reports NoEffect only when both counts and a clear cursor read back exactly as before.
        bool cursorHeld=!paired && last is { CursorClear:false };
        if(cursorHeld && clean && returnCursor is not null) {
            ContainerTransferReadback? back=null;
            try { returning=true; try { returnCursor(); } finally { returning=false; } back=read(); }
            catch(Exception ex) { returnFault=true; why+=";return:"+Short(ex); }
            if(!returnFault && back is { CursorClear:true } && back.ActorCount==before.ActorCount
                && back.ContainerCount==before.ContainerCount && back.ContainerToken==before.ContainerToken
                && back.SourceFactId==before.SourceFactId)
                return new("TransferNoEffect",false,true,2,false,back.ReceiverToken,back.Generation,back.ReadbackToken,
                    Detail:"cursor_returned:"+why);
            if(back is not null) { last=back; cursorHeld=!back.CursorClear; }
            why+=returnFault?";return_unverified":";return_counts";
        }
        return new(paired?"TransferObserved":"TransferUncertain",paired,clean && !cursorHeld,1,transition,
            last?.ReceiverToken,last?.Generation,last?.ReadbackToken,Detail:why);
    }
}

/// <summary>PM-1 fix8: the pre-arm container_transfer terms, shared by both chest arms (v1 / WS-G2 mode) and the published
/// transfer affordances (a dry run of the same terms per slot). Null = admitted; ContainerTransferInput.Execute re-checks.</summary>
public static class ContainerTransferAdmission
{
    public static string? Refusal(JsonElement facts, ContainerTransferTarget target, long generation, bool ready, bool singleItemReady,
        out ContainerTransferReadback? before, out ContainerTransferTarget? current, out int capacity)
    {
        before = ContainerTransferReadback.Read(facts, target, generation, out current, out capacity);
        return !ready ? "transfer_not_ready" : !target.IsValid ? "target_invalid"
            : before is null || current != target ? "binding" : capacity < target.Amount ? "capacity"
            : target.Quantity is not null && !singleItemReady ? "single_item_unavailable" : !before.CursorClear ? "cursor_item"
            : (target.IsTakeOut ? before.ContainerCount : before.ActorCount) < target.Stack ? "count" : target.IsShip ? "bin_only" : null;
    }

    /// <summary>Every whole-stack put (v1 wood/stone, else WS-G2 quick), single put and take_out the arm admits now.</summary>
    public static IEnumerable<Dictionary<string, object>> Publish(JsonElement facts, long generation, bool ready, bool modesReady,
        bool singleItemReady, Func<int, string?> storeRefusal)
    {
        if (facts.ValueKind != JsonValueKind.Object || !facts.TryGetProperty("available", out var a) || a.ValueKind != JsonValueKind.True)
            yield break;
        string T(string k) => facts.GetProperty(k).GetString()!;
        foreach (var (side, takeOut) in new[] { ("actor_slots", false), ("container_slots", true) })
            foreach (var row in facts.GetProperty(side).EnumerateArray())
            {
                var item = row.GetProperty("item");
                if (item.ValueKind != JsonValueKind.Object) continue;
                string qid = item.GetProperty("qualified_item_id").GetString()!;
                int slot = row.GetProperty("slot").GetInt32(), stack = item.GetProperty("stack").GetInt32(), quality = item.GetProperty("quality").GetInt32();
                bool v1 = !takeOut && qid is "(O)388" or "(O)390" && quality == 0;
                var modes = takeOut ? new[] { ("take_out", (int?)null) } : v1 ? new[] { ("put", (int?)null), ("put_one", (int?)1) }
                    : new[] { ("quick", (int?)null) };
                foreach (var (mode, quantity) in modes)
                {
                    if ((mode is "quick" or "take_out" && !modesReady) || (mode == "quick" && storeRefusal(slot) is not null)) continue;
                    var target = new ContainerTransferTarget(T("receiver_token"), T("container_token"), T("readback_token"), T("source_fact_id"),
                        row.GetProperty("target_token").GetString()!, slot, qid, stack, quality, stack, quantity,
                        mode is "quick" or "take_out" ? mode : null);
                    if (Refusal(facts, target, generation, ready, singleItemReady, out _, out _, out _) is null)
                        yield return new() { ["verb"] = "transfer", ["slot"] = slot, ["mode"] = mode,
                            ["from"] = takeOut ? "container" : "actor", ["to"] = takeOut ? "actor" : "container", ["amount"] = target.Amount };
                }
            }
    }
}
