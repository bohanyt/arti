using System.Text.Json;
using ArtiStardewBridge.Observation;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Characters;
using StardewValley.Tools;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    readonly GiftTargetInstances genericWorldInstances = new();
    // Generic buttons share the existing owner, retained pad hold, stop and release proof.
    // They never choose the intended effect or invoke a feature-specific game method.
    bool GenericWorldReady => (this.config.GenericWorldInputEnabled || this.OrdinaryInteractSurface()) && this.WorldInteractionBackgroundReady
        && this.PadPathConfigured // ADR 244: no focus term
        && (Context.IsPlayerFree && Context.CanPlayerMove || NativeEventControl.Token() is not null)
        && Game1.activeClickableMenu is null
        && (!(Game1.eventUp || Game1.CurrentEvent is not null) || NativeEventControl.Token() is not null) && Game1.currentMinigame is null
        && Game1.chatBox?.isActive() != true && Game1.player?.UsingTool != true && Game1.player?.CursorSlotItem is null;

    static bool IsOpenableContainer(StardewValley.Object? item) => item is StardewValley.Objects.Chest chest
        && OpenableContainer.Eligible(true, chest.giftbox.Value, chest.playerChest.Value);

    static int? ContainerLid(StardewValley.Objects.Chest chest) => GenericFields(chest.GetType())
        .FirstOrDefault(f => f.Name == "currentLidFrame")?.GetValue(chest) as int?;

    bool OrdinaryInteractSurface() => Game1.player?.currentLocation is { } location
        && GenericWorld.OrdinarySurface(location.objects.Values.Any(IsOpenableContainer),
            this.ShopCounterEnabled && this.vendorCounterSample is { Refusal: null } counter && ReferenceEquals(counter.Location, location),
            this.ExtrasEnabled, location.doesTileHaveProperty((int)Game1.player.GetGrabTile().X,
                (int)Game1.player.GetGrabTile().Y, "Action", "Buildings"));

    void EnrichOpenableContainers(Dictionary<string, object> snapshot)
    {
        snapshot.Remove("openable_containers");
        if (!Context.IsWorldReady || Game1.player?.currentLocation is not { } location) return;
        snapshot["openable_containers"] = location.objects.Pairs.Where(p => IsOpenableContainer(p.Value)).Select(p => {
            var chest = (StardewValley.Objects.Chest)p.Value;
            var fact = OpenableContainer.Fact(SceneIdentity.ObjectFactId(location.NameOrUniqueName, (int)p.Key.X, (int)p.Key.Y, chest.QualifiedItemId),
                (int)p.Key.X, (int)p.Key.Y, chest.giftbox.Value, chest.giftboxIsStarterGift.Value,
                chest.giftboxIsStarterGift.Value && location is StardewValley.Locations.FarmHouse home && !home.IsOwnedByCurrentPlayer);
            fact["state"] = chest.giftbox.Value ? "unopened_gift" : "reward_lid_frame:" + ContainerLid(chest);
            fact["stands"] = ExtrasStandTiles(location, GenericWorld.Action, (int)p.Key.X, (int)p.Key.Y).Stands;
            return fact;
        }).ToArray();
    }

    (WorldInteractionTarget? Target, StardewValley.Objects.Chest? Chest, BgActionTarget? Action)
        CaptureGenericWorld(string kind)
    {
        if (!GenericWorld.IsKind(kind) || !this.GenericWorldReady || Game1.player is not Farmer player
            || player.currentLocation is not GameLocation location)
            return (null, null, null);
        Vector2 tile = kind == GenericWorld.Use && BackgroundToolTileProbe.ToolTile(player) is { } tool
            ? new Vector2(tool.X, tool.Y) : player.GetGrabTile();
        if (!location.isTileOnMap(tile) || Math.Abs(tile.X - player.Tile.X) + Math.Abs(tile.Y - player.Tile.Y) > 1)
            return (null, null, null);
        Item? held = player.CurrentItem;
        int slot = player.CurrentToolIndex, day = Game1.Date.TotalDays;
        var identity = this.CurrentCommandIdentity();
        string? eventPin = NativeEventControl.Token();
        string source = "generic:" + SceneIdentity.Token(location.NameOrUniqueName) + ":" + (int)tile.X + "," + (int)tile.Y;
        string admission = this.GenericWorldAdmission(player, location, tile);
        string token = this.InteractToken(kind, admission, source, player, null);
        var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y,
            (int)player.Tile.X, (int)player.Tile.Y, player.FacingDirection, kind);
        if (!target.IsValid) return (null, null, null);
        bool Bound() => Context.IsWorldReady && ReferenceEquals(Game1.player, player)
            && Game1.Date.TotalDays == day && this.CurrentCommandIdentity().SessionId == identity.SessionId;
        bool Present() => Bound() && ReferenceEquals(player.currentLocation, location)
            && player.CurrentToolIndex == slot && ReferenceEquals(player.CurrentItem, held)
            && this.CaptureGenericWorld(kind).Target == target;
        BgActionEffectFacts Sample() => SampleInteractEffect(Bound, "") with
        {
            Generic = Bound() ? this.SampleGenericWorld(player, location, tile, identity.SessionId + ":" + player.UniqueMultiplayerID + ":" + day) : null,
        };
        return (target, null, new BgActionTarget(BgActionKind.Generic, null, null, "", Present, Sample)
        {
            UsePadX = kind == GenericWorld.Use, ToolUseAfterPress = true, PoseMayChangeAfterPress = true,
            // The menu is an observed receiver for generic UI, never a scripted answer.
            ExpectedReceiver = menu => menu is not null,
            EventScopeValid = eventPin is null ? null : consumed => Bound() && (consumed || NativeEventControl.Token() == eventPin),
        });
    }

    string GenericWorldAdmission(Farmer player, GameLocation location, Vector2 tile)
    {
        string Instance(object? value) => value is null ? "_" : this.genericWorldInstances.Single(value);
        var rect = TileRect(tile);
        location.objects.TryGetValue(tile, out var obj);
        location.terrainFeatures.TryGetValue(tile, out var terrain);
        Item? held = player.CurrentItem;
        object?[] pins = {
            Instance(player), Instance(location), player.CurrentToolIndex, Instance(held), held?.QualifiedItemId,
            held?.Stack, held?.Quality, Instance(obj), Instance(terrain),
            NativeEventControl.Token(),
            location.characters.Where(c => c.GetBoundingBox().Intersects(rect)).Select(c => Instance(c)).OrderBy(x => x).ToArray(),
            location.animals.Values.Where(a => a.GetBoundingBox().Intersects(rect)).Select(a => Instance(a)).OrderBy(x => x).ToArray(),
        };
        if (this.config.UiPadMenusEnabled)
            pins = pins.Append(location.doesTileHaveProperty((int)tile.X, (int)tile.Y, "Action", "Buildings")).ToArray();
        return JsonSerializer.Serialize(pins);
    }

    GenericWorldFrame SampleGenericWorld(Farmer player, GameLocation location, Vector2 center, string binding)
    {
        var fields = new SortedDictionary<string, string>(StringComparer.Ordinal);
        void Put(string path, object? value) => fields[path] = JsonSerializer.Serialize(value);
        int cx = (int)center.X, cy = (int)center.Y;
        for (int y = cy - GenericWorld.Radius; y <= cy + GenericWorld.Radius; y++)
            for (int x = cx - GenericWorld.Radius; x <= cx + GenericWorld.Radius; x++)
            {
                Vector2 tile = new(x, y);
                if (location.objects.TryGetValue(tile, out var obj))
                {
                    Put("object:" + x + "," + y, SceneSnapshotCapture.CaptureObject(location.NameOrUniqueName, tile, obj).Dto);
                    if (IsOpenableContainer(obj) && obj is StardewValley.Objects.Chest chest)
                        Put("container:" + x + "," + y, new { gift = chest.giftbox.Value, lid_frame = ContainerLid(chest) });
                }
                if (location.terrainFeatures.TryGetValue(tile, out var terrain))
                    Put("terrain:" + x + "," + y, SceneSnapshotCapture.CaptureTerrain(location.NameOrUniqueName, tile, terrain).Dto);
            }
        for (int slot = 0; slot < player.Items.Count; slot++)
        {
            Item? item = player.Items[slot];
            Put("inventory:" + slot, item is null ? null : new { qid = item.QualifiedItemId, stack = item.Stack,
                quality = item.Quality, water = item is WateringCan can ? (int?)can.WaterLeft : null });
        }
        Rectangle window = new((cx - GenericWorld.Radius) * 64, (cy - GenericWorld.Radius) * 64,
            (GenericWorld.Radius * 2 + 1) * 64, (GenericWorld.Radius * 2 + 1) * 64);
        foreach (NPC npc in location.characters.Where(c => c.GetBoundingBox().Intersects(window)))
        {
            player.friendshipData.TryGetValue(npc.Name, out Friendship? friendship);
            Put("character:" + npc.Name, new { npc.Name, petted = npc is Pet pet
                ? pet.lastPetDay.TryGetValue(player.UniqueMultiplayerID, out int d) && d == Game1.Date.TotalDays : (bool?)null,
                friendship = friendship?.Points, gifts_today = friendship?.GiftsToday, gifts_week = friendship?.GiftsThisWeek });
        }
        foreach (var animal in location.animals.Values.Where(a => a.GetBoundingBox().Intersects(window)))
            Put("animal:" + animal.myID.Value, new { petted = animal.wasPet.Value, produce = animal.currentProduce.Value,
                quality = animal.produceQuality.Value, friendship = animal.friendshipTowardFarmer.Value });
        Put("hay_stock", location.GetRootLocation().piecesOfHay.Value);
        foreach (var quest in this.ReadQuestFacts(player, QuestTargetLocation)) Put("quest:" + quest.Id, quest.Wire());
        Put("location", CurrentLocationId(player));
        Put("menu", Game1.activeClickableMenu is { } menu
            ? new { type = menu.GetType().Name, receiver = this.ReceiverTokenOf(CurrentInteractiveMenu(menu)) } : null);
        Put("money", player.Money);
        return new GenericWorldFrame(binding, SceneIdentity.Token(location.NameOrUniqueName) + ":" + cx + "," + cy,
            Game1.ticks, fields, fields.Count <= GenericWorld.MaxFields);
    }

    void EnrichGenericWorldContext(Dictionary<string, object> snapshot)
    {
        this.EnrichOpenableContainers(snapshot);
        snapshot.Remove("world_buttons");
        if (!this.GenericWorldReady) return;
        try
        {
            var targets = new[] { GenericWorld.Action, GenericWorld.Use }.Select(k => this.CaptureGenericWorld(k).Target)
                .Where(t => t is not null).Select(t => t!.Value.Args()).ToArray();
            snapshot["world_buttons"] = new Dictionary<string, object> { ["targets"] = targets,
                ["meaning"] = "ACTION / USE with the current held item. Observe the delta; no expected effect is implied." };
        }
        catch { snapshot.Remove("world_buttons"); }
    }
}
