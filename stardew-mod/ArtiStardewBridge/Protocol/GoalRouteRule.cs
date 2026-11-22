namespace ArtiStardewBridge.Protocol;

/// <summary>FIXB38 conservative Pierre admission. Native checkAction/lockedDoorWarp still executes the door.</summary>
public static class GoalRouteRule
{
    public static bool PierreOpen(int time, bool wednesday, bool festivalClosed) =>
        time >= 900 && time < 1700 && !wednesday && !festivalClosed;

    public static bool PierreDoor(string[] parts, int time, bool wednesday, bool festivalClosed) =>
        parts.Length == 6 && parts[0] == "LockedDoorWarp" && parts[3] == "SeedShop"
        && int.TryParse(parts[1], out int x) && x >= 0 && int.TryParse(parts[2], out int y) && y >= 0
        && int.TryParse(parts[4], out int open) && int.TryParse(parts[5], out int close)
        && open == 900 && close >= 1700 && close <= 2400 && time < close
        && PierreOpen(time, wednesday, festivalClosed);

    /// <summary>Live7 weather: today's native LocationWeather of the actor's location context (an indoor location
    /// reads its context, e.g. the FarmHouse reads the valley) as a stable token: sun / rain / storm / snow / wind /
    /// green_rain. The native flags decide (Sun / Festival / Wedding set none of them = sun).</summary>
    public static string WeatherToken(bool raining, bool lightning, bool snowing, bool debris, bool greenRain) =>
        greenRain ? "green_rain" : lightning ? "storm" : raining ? "rain" : snowing ? "snow" : debris ? "wind" : "sun";

    /// <summary>Native weather id (Game1.weather_*) to the same token set; null for an id it does not know.</summary>
    public static string? ForecastToken(string? id) => id switch
    {
        "Sun" or "Festival" or "Wedding" => "sun",
        "Rain" => "rain",
        "Storm" => "storm",
        "Snow" => "snow",
        "Wind" => "wind",
        "GreenRain" => "green_rain",
        _ => null,
    };
}
