using System.Text.Json;
using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    /// <summary>Result of the WS3a kind resolution: a target, a miss, or "continue with the chest rule".</summary>
    private readonly record struct InteractKindCapture(WorldInteractionTarget? Target, BgActionTarget? Action, string? Miss, bool ChestPath)
    {
        public static InteractKindCapture Chest => new(null, null, null, true);
        public static InteractKindCapture Missed(string miss) => new(null, null, miss, false);
    }

    string InteractToken(string kind, string instance, string source, Farmer player, string? expected,
        AnimalProductFingerprint? product = null)
    {
        CommandInboxIdentity identity = this.CurrentCommandIdentity();
        object?[] pins = { kind, instance, source, identity, (int)player.Tile.X, (int)player.Tile.Y, player.FacingDirection, expected };
        if (product is not null)
            pins = pins.Append(product).ToArray();
        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(pins)))).ToLowerInvariant();
    }

    static int InventoryCount(Farmer player, string qid) =>
        qid.Length == 0 ? 0 : player.Items.Where(i => i is not null && i.QualifiedItemId == qid).Sum(i => i!.Stack);

    static readonly GiftTargetInstances travelRequestInstances = new();
    static string? NativeTravelRequestEpoch() => Game1.locationRequest is { } request
        ? travelRequestInstances.Single(request) : null;

    /// <summary>Read-only effect facts for a pinned door / forage target (runtime settle readback).</summary>
    static BgActionEffectFacts SampleInteractEffect(Func<bool> present, string qid)
    {
        Farmer? player = Game1.player;
        GameLocation? location = player?.currentLocation;
        bool transitioning = Game1.isWarping || Game1.fadeToBlack || Game1.globalFade || Game1.fadeToBlackAlpha > 0.001
            || Game1.locationRequest is not null || Game1.currentLocation is null || location is null;
        bool presentNow;
        try { presentNow = present(); } catch { presentNow = false; }
        return new BgActionEffectFacts(
            Game1.currentLocation is null ? "" : CurrentLocationId(player),
            false,
            transitioning,
            Context.IsWorldReady && Context.IsPlayerFree && Context.CanPlayerMove,
            Game1.activeClickableMenu is not null || Game1.dialogueUp,
            Game1.activeClickableMenu?.GetType().Name,
            presentNow,
            player is null ? 0 : InventoryCount(player, qid),
            Game1.eventUp || Game1.CurrentEvent is not null,
            PendingLocationToken: Game1.locationRequest?.Name is { Length: > 0 } pending ? SceneIdentity.Token(pending) : null,
            FadeAlpha: Game1.fadeToBlackAlpha, RequestEpoch: NativeTravelRequestEpoch());
    }

    static string ExtrasSource(string kind, GameLocation location, Vector2 tile, string? name)
    {
        string raw = location.NameOrUniqueName ?? location.Name ?? "_";
        string source = "xt:" + kind + ":" + SceneIdentity.Token(raw) + ":" + (int)tile.X + "," + (int)tile.Y
            + (name is null ? "" : ":" + SceneIdentity.Token(name));
        return source.Length <= 256 ? source : source[..256];
    }

    /// <summary>FIXB16 review: the extras target the last capture refused (reset at every capture).</summary>
    (string Kind, string Source, int X, int Y)? extrasMissTarget;

    /// <summary>One captured extras target: guard verdict first, then the token / pinned closures (re-capture pre-press).</summary>
    InteractKindCapture ExtrasCapture(string kind, Farmer player, GameLocation location, Vector2 tile, ExtrasGuardFacts facts,
        object instanceObject, string? name, string? expected, Func<BgExtraFacts>? readback, Func<object?, bool>? receiver = null,
        AnimalProductFingerprint? product = null, Func<bool>? instancePin = null)
    {
        if (WorldExtras.Miss(kind, facts) is { } miss)
        {
            // FIXB16 review MAJOR: the refused target (kind + source + tile) for ui.context "extras_refusal".
            this.extrasMissTarget = (kind, ExtrasSource(kind, location, tile, name), (int)tile.X, (int)tile.Y);
            return InteractKindCapture.Missed(miss);
        }
        string source = ExtrasSource(kind, location, tile, name);
        string instance = this.worldTargetInstanceTokenTracker.Observe(instanceObject)!;
        if (kind is WorldExtras.AnimalPet or WorldExtras.AnimalMilk or WorldExtras.AnimalShear or WorldExtras.HayPlace)
            instance += ":" + this.animalTargetInstances.Single(player) + ":" + this.animalTargetInstances.Single(location)
                + ":" + this.animalTargetInstances.Single(Game1.getFarm())
                + ":" + (location.ParentBuilding is { } parent ? this.animalTargetInstances.Single(parent) : "_");
        string token = this.InteractToken(kind, instance, source, player, expected, product);
        var target = new WorldInteractionTarget(token, source, (int)tile.X, (int)tile.Y, (int)player.Tile.X, (int)player.Tile.Y,
            player.FacingDirection, kind, expected);
        if (!target.IsValid)
            return InteractKindCapture.Missed("target_invalid");
        GameLocation pinnedLocation = location;
        Vector2 pinnedTile = tile;
        Func<bool>? nativeBinding = kind is WorldExtras.AnimalPet or WorldExtras.AnimalMilk or WorldExtras.AnimalShear or WorldExtras.HayPlace
            ? PinAnimalLocation(player, location) : null;
        var captureIdentity = this.CurrentCommandIdentity();
        bool nativeReadFault = false;
        // Pre-press re-check = the full extras capture again (every guard re-evaluated), same kind and source.
        // #309: instancePin (B3 crab-pot bait / collect) also requires the SAME native object instance at the tile, so a
        // replacement pot with an identical kind/source/tile is refused before the first input (not only at readback).
        bool StillPresent() => !nativeReadFault && (nativeBinding?.Invoke() ?? true) && (instancePin?.Invoke() ?? true)
            && Game1.player is Farmer p && ReferenceEquals(p.currentLocation, pinnedLocation)
            && (WorldExtras.UsesX(kind) ? ExtrasXTile(p, p.GetGrabTile()) : ExtrasResolveTile(p, kind)) == pinnedTile
            && this.CaptureExtrasKind(p, ExtrasResolveTile(p, kind)) is { Target: { } now }
            && now.Kind == kind && string.Equals(now.SourceFactId, source, StringComparison.Ordinal)
            && (kind is not (WorldExtras.AnimalShop or WorldExtras.Blacksmith or WorldExtras.CommunityNote or WorldExtras.Kitchen)
                || ReferenceEquals(p, player) && this.CurrentCommandIdentity() == captureIdentity && now.TargetToken == target.TargetToken)
            && (kind is not (WorldExtras.AnimalPet or WorldExtras.AnimalMilk or WorldExtras.AnimalShear or WorldExtras.HayPlace)
                || AnimalRoutine.SameTarget(target.TargetToken, now.TargetToken));
        BgActionKind actionKind = WorldInteractionKinds.ToActionKind(kind);
        BgActionEffectFacts Sample()
        {
            // Never read a retired house or a replacement actor's inventory, even when names match.
            bool bound;
            try { bound = !nativeReadFault && (nativeBinding?.Invoke() ?? true); } catch { bound = false; }
            if (!bound)
            {
                nativeReadFault = true;
                return SampleInteractEffect(() => false, "") with { Extra = null };
            }
            BgExtraFacts? extra = null;
            try { extra = readback?.Invoke(); } catch { extra = null; nativeReadFault |= nativeBinding is not null; }
            bool expectedMenu = false;
            try { expectedMenu = receiver?.Invoke(Game1.activeClickableMenu) == true; } catch { expectedMenu = false; }
            return SampleInteractEffect(() => true, "") with { Extra = extra, ExpectedReceiver = expectedMenu };
        }
        var action = new BgActionTarget(actionKind, null, expected, "", StillPresent, Sample)
        {
            ExtraKind = kind,
            UsePadX = WorldExtras.UsesX(kind),
            ToolUseAfterPress = WorldExtras.ToolUseAfterPress(kind),
            PoseMayChangeAfterPress = WorldExtras.PoseMayChange(kind),
            ExpectedReceiver = receiver,
        };
        return new InteractKindCapture(target, action, null, false);
    }

}
