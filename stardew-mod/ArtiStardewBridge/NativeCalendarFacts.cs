using StardewValley;
using StardewValley.TokenizableStrings;

namespace ArtiStardewBridge;

/// <summary>Read-only native calendar, not an event schedule maintained by the controller (ADR 39).</summary>
internal static class NativeCalendarFacts
{
    internal sealed record CalendarEvent(string id, string name, string kind, string[] dates, string[] aliases,
        string? location = null, int? starts_at = null, int? ends_at = null, bool passive = false);

    internal static void Hours(string? conditions, out string? location, out int? start, out int? end)
    {
        location = null; start = null; end = null;
        if (string.IsNullOrWhiteSpace(conditions)) return;
        string[] parts = conditions.Split('/');
        if (parts.Length < 2 || string.IsNullOrWhiteSpace(parts[0])) return;
        location = parts[0].Trim();
        string[] clocks = parts[1].Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (clocks.Length >= 1 && int.TryParse(clocks[0], out int a) && a is >= 600 and <= 2600) start = a;
        if (clocks.Length >= 2 && int.TryParse(clocks[1], out int b) && b is >= 600 and <= 2600) end = b;
    }

    internal static CalendarEvent[] Festivals(int year, Dictionary<string, string> index,
        Func<string, Dictionary<string, string>> load)
    {
        var rows = new List<CalendarEvent>();
        foreach (var entry in index)
        {
            var match = System.Text.RegularExpressions.Regex.Match(entry.Key, @"^(spring|summer|fall|winter)(\d+)$");
            if (!match.Success || !int.TryParse(match.Groups[2].Value, out int day) || day is < 1 or > 28) continue;
            var data = load("Data/Festivals/" + entry.Key);
            if (!data.TryGetValue("name", out var name)) continue;
            Hours(data.GetValueOrDefault("conditions"), out var loc, out var start, out var end);
            rows.Add(new(entry.Key, name, "festival",
                new[] { $"Y{year} {match.Groups[1].Value} {day}" }, new[] { entry.Key }, loc, start, end));
        }
        return rows.ToArray();
    }

    /// <summary>The calendar loaded with the save (game data files), never an observation (ADR 230 N4): it stays an
    /// admission/presentation input whose rows keep this exact shape, so fact-line-off pages are unchanged. An entry
    /// counts as seen only through a calendar_day_read receipt, the Billboard day tooltip she was shown
    /// (ModEntry.GameText.cs); the fact-line view and her story memory keep only those.</summary>
    internal static object[] Capture()
    {
        var rows = Festivals(Game1.year, DataLoader.Festivals_FestivalDates(Game1.content),
            path => Game1.content.Load<Dictionary<string, string>>(path)).ToList();
        string Date(string season, int day) => $"Y{Game1.year} {season.ToLowerInvariant()} {day}";
        void Add(string id, string name, string kind, IEnumerable<string> dates, params string[] aliases) =>
            rows.Add(new(id, name, kind, dates.ToArray(), aliases));
        Utility.ForEachVillager(npc => {
            if (npc.Birthday_Day is >= 1 and <= 28 && !string.IsNullOrEmpty(npc.Birthday_Season))
                Add("birthday:" + npc.Name, npc.displayName + " birthday", "birthday",
                    new[] { Date(npc.Birthday_Season, npc.Birthday_Day) }, npc.Name + " birthday", "ulang tahun " + npc.displayName);
            return true;
        });
        foreach (Season season in Enum.GetValues<Season>())
            for (int day = 1; day <= 28; day++)
                if (Utility.TryGetPassiveFestivalDataForDay(day, season, null, out string id, out var data, false)
                    && data.ShowOnCalendar)
                {
                    if (data.MapReplacements?.Count is not > 0)
                        rows.Add(new(id, TokenParser.ParseText(data.DisplayName), "festival",
                            new[] { Date(season.ToString(), day) }, new[] { id }, null, data.StartTime, 2600, true));
                    foreach (string location in data.MapReplacements?.Keys ?? Enumerable.Empty<string>())
                        rows.Add(new(id, TokenParser.ParseText(data.DisplayName), "festival",
                            new[] { Date(season.ToString(), day) }, new[] { id }, location, data.StartTime,
                            2600, true)); // Passive festivals remain accessible until the native 02:00 day boundary.
                }
        Add("bookseller", Game1.content.LoadString("Strings/1_6_Strings:Bookseller"), "dated_event",
            Utility.getDaysOfBooksellerThisSeason().Select(day => Date(Game1.currentSeason, day)));
        return rows.GroupBy(r => r.id).Select(g => g.First() with { dates = g.SelectMany(r => r.dates).Distinct().ToArray() }).ToArray();
    }
}
