using ArtiStardewBridge.Protocol;
using Microsoft.Xna.Framework;
using StardewModdingAPI;
using StardewValley;
using StardewValley.Tools;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    static bool PierreRouteOpen() => GoalRouteRule.PierreOpen(Game1.timeOfDay,
        Game1.shortDayNameFromDayOfSeason(Game1.dayOfMonth) == "Wed", GameLocation.AreStoresClosedForFestival());

    // Read-only, bounded fields in the existing actor/session-bound ui.context; no remote-location snapshot.
    void EnrichGoalRouteContext(Dictionary<string, object> snapshot)
    {
        if (!Context.IsWorldReady || Game1.player is not Farmer p || p.currentLocation is not GameLocation loc
                || !this.config.WorldExtrasEnabled || !this.config.GamepadInputEnabled)
            return;
        if (!this.config.EconomyMenusEnabled && !this.config.ReflexFishingEnabled)
            return;
        Dictionary<string, object> facts = new(StringComparer.Ordinal)
        {
            ["location"] = SceneIdentity.Token(loc.NameOrUniqueName),
            ["actor_tile"] = new[] { p.TilePoint.X, p.TilePoint.Y },
            ["date"] = new object[] { Game1.year, Game1.currentSeason, Game1.dayOfMonth },
            ["time_of_day"] = Game1.timeOfDay,
            ["shop_open"] = this.config.EconomyMenusEnabled && PierreRouteOpen(),
            ["rod_owned"] = this.config.ReflexFishingEnabled && p.Items.Any(i => i is FishingRod),
        };
        // Live7: today's native weather of the actor's location context (indoors reads the same context, so the
        // FarmHouse/Cabin report the valley's rain); is_raining is that context's flag, outdoors the location's own.
        var weather = loc.GetWeather(); // StardewValley.Network.LocationWeather
        facts["weather"] = GoalRouteRule.WeatherToken(weather.IsRaining, weather.IsLightning, weather.IsSnowing,
            weather.IsDebrisWeather, weather.IsGreenRain);
        facts["is_raining"] = weather.IsRaining;
        facts["outdoors"] = loc.IsOutdoors;
        if (GoalRouteRule.ForecastToken(NativeGoalRouteWeather.TomorrowId(loc)) is { } tomorrow)
            facts["weather_tomorrow"] = tomorrow;
        Point? landmark = loc.NameOrUniqueName switch { "SeedShop" => new Point(4, 20), "Mountain" => new Point(43, 17), _ => null };
        if (landmark is { } t && NavCellAt(loc, t.X, t.Y) == '.'
                && loc.isTilePassable(new Vector2(t.X, t.Y))
                && !loc.IsTileBlockedBy(new Vector2(t.X, t.Y), CollisionMask.All, CollisionMask.All))
            facts["landmark"] = new Dictionary<string, object> { ["tile"] = new[] { t.X, t.Y } };
        snapshot["goal_route"] = facts;
    }
}
