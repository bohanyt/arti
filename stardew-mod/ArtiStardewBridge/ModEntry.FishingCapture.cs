using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Tools;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    /// <summary>Read-only fishing facts at ARTI's current pose for a cast released at <paramref name="power"/>.</summary>
    private (bool RodHeld, bool FacesWater, bool LandingFishable, int LandX, int LandY, int Level) SampleFishingFacts(float power)
    {
        try
        {
            Farmer? p = Context.IsWorldReady ? Game1.player : null;
            GameLocation? loc = p?.currentLocation;
            if (p is null || loc is null)
                return (false, false, false, -1, -1, 0);
            bool rod = p.CurrentTool is FishingRod;
            Point standing = p.StandingPixel;
            int facing = p.FacingDirection;
            (int dx, int dy) = FishCastGeometry.FacingDelta(facing);
            int tx = standing.X / 64, ty = standing.Y / 64;
            bool faces = loc.isWaterTile(tx + dx, ty + dy);
            (int lx, int ly) = FishCastGeometry.LandingTile(standing.X, standing.Y, facing, power, p.FishingLevel);
            // Review minor 3: the cast flies at target .. target + 2 sweep steps; every tile of that window must be fishable.
            bool fishable = FishCastGeometry.WindowFishable(standing.X, standing.Y, facing, power, p.FishingLevel,
                (x, y) => loc.isTileOnMap(x, y) && loc.canFishHere() && loc.isTileFishable(x, y));
            return (rod, faces, fishable, lx, ly, p.FishingLevel);
        }
        catch
        {
            return (false, false, false, -1, -1, 0);
        }
    }

    /// <summary>Pure admission facts (pad mode / cursor terms filled in by the caller).</summary>
    private FishAdmissionFacts SampleFishAdmission(float power)
    {
        var f = this.SampleFishingFacts(power);
        Farmer? p = Context.IsWorldReady ? Game1.player : null;
        return new FishAdmissionFacts(
            FlagOn: this.config.ReflexFishingEnabled,
            PadPathConfigured: this.PadPathConfigured,
            WindowActive: Game1.game1?.IsActive == true,
            WorldReady: Context.IsWorldReady && p?.currentLocation is not null,
            MenuOpen: Game1.activeClickableMenu is not null || Game1.dialogueUp,
            EventUp: Game1.eventUp || Game1.CurrentEvent is not null || Game1.currentMinigame is not null || Game1.farmEvent is not null,
            PlayerFree: Context.IsPlayerFree && Context.CanPlayerMove,
            RodHeld: f.RodHeld,
            UsingTool: p?.UsingTool == true,
            RuntimeBusy: this.backgroundMove?.Busy == true || this.backgroundMove?.MenuDebtActive == true,
            Stamina: p?.Stamina ?? 0f,
            FreeSlots: p?.freeSpotsInInventory() ?? 0,
            FacesWater: f.FacesWater,
            LandingFishable: f.LandingFishable,
            PadModeRefusal: null,
            CursorHiddenRefusal: null);
    }

}
