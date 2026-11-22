using StardewValley;

namespace ArtiStardewBridge;

internal static class NativeGoalRouteWeather
{
    internal static string TomorrowId(GameLocation location)
    {
        // Other contexts keep their own forecast, as in TV's island weather channel.
        if (!location.InValleyContext())
            return location.GetWeather().WeatherForTomorrow;

        // Match TV.getWeatherForecast (1.6.15): raw weather omits date overrides such as day-3 rain.
        WorldDate tomorrow = new(Game1.Date);
        tomorrow.TotalDays++;
        string forecast = Game1.IsMasterGame ? Game1.weatherForTomorrow : Game1.netWorldState.Value.WeatherForTomorrow;
        return Game1.getWeatherModificationsForDate(tomorrow, forecast);
    }
}
