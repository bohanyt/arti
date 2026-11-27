using System.Collections;
using System.Reflection;
using ArtiStardewBridge.Protocol;
using StardewValley;
using StardewValley.Menus;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    // Extend the shared screen reader; never load Data/Mail, Data/Events or Data/Festivals.
    static object? PresentedField(object value, string name)
    {
        for (Type? type = value.GetType(); type is not null; type = type.BaseType)
        {
            if (type.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly) is { } field)
                return field.GetValue(value);
            if (type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly) is { } property)
                return property.GetValue(value);
        }
        return null;
    }

    IReadOnlyList<UiTextTickSample> CapturePresentedGameText(UiTextTickSample binding)
    {
        var result = new List<UiTextTickSample>();
        var menu = CurrentInteractiveMenu(Game1.activeClickableMenu);
        string date = $"{Game1.year}:{Game1.currentSeason}:{Game1.dayOfMonth}";
        ulong saveId = Game1.uniqueIDForThisGame;
        string save = saveId.ToString(System.Globalization.CultureInfo.InvariantCulture);
        void Add(string kind, string id, string page, string text, string source, string? speaker = null, string? instance = null, bool complete = true)
        {
            result.AddRange(PresentedGameText.Capture(binding with { Source = source, MenuInstanceToken = instance ?? binding.MenuInstanceToken, CaptureComplete = complete }, kind, id, page,
                text, date, Game1.timeOfDay, binding.LocationId, save, speaker));
        }
        // Typed current-page reader is the same one used on the legacy path, before its bounds.
        var legacy = this.CaptureScreenText(binding, exact: true);
        if (menu is DialogueBox { characterDialogue.speaker: { } npc }) binding = binding with { Speaker = npc.displayName };
        var raw = legacy[0] with { Speaker = binding.Speaker };
        if (menu is LetterViewerMenu letter && raw.Text.Length > 0)
        {
            string id = PresentedField(letter, "mailTitle") as string ?? "";
            // Anonymous/native generated letters have no game ID; content identity is explicit.
            if (id.Length == 0) id = "content:" + PresentedGameText.Hash(raw.Text);
            Add("letter_opened", id, letter.page.ToString(), raw.Text,
                PresentedField(letter, "isFromCollection") is true ? raw.Source + ":collection" : raw.Source, complete: raw.CaptureComplete);
        }
        else if (menu is DialogueBox && raw.Text.Length > 0)
        {
            string? eventId = Game1.CurrentEvent is { } scene ? PresentedField(scene, "id") as string : null;
            string command = Game1.CurrentEvent is { } current ? (PresentedField(current, "CurrentCommand") ?? PresentedField(current, "currentCommand"))?.ToString() ?? "0" : "0";
            Add(Game1.eventUp ? "event_watched" : raw.Speaker is { Length: > 0 } ? "dialogue_read" : "door_message",
                eventId ?? "content:" + PresentedGameText.Hash((raw.Speaker ?? "") + "\0" + raw.Text),
                command, raw.Text, raw.Source, raw.Speaker, complete: raw.CaptureComplete);
        }
        else if (menu is LevelUpMenu && raw.Text.Length > 0)
            Add("hud_notice", "level:" + PresentedGameText.Hash(raw.Text), "0", raw.Text, raw.Source, complete: raw.CaptureComplete);
        // ADR 230 N3: the end-of-day earnings screen as the game wrote it (its categories and totals, after the intro,
        // on the summary page), dated by the game day it is shown on. No screen shown, nothing recorded.
        else if (menu is ShippingMenu && raw.Text.Length > 0)
            Add("shipping_summary_seen", "shipping:" + date, "0", raw.Text, raw.Source, complete: raw.CaptureComplete);
        // The journal entry or the bundle page that is open now, in the menu's own words.
        else if (PresentedMenus.Read(menu) is { } shown)
            Add(shown.Kind, shown.Id, "0", shown.Text, shown.Source);
        else if (menu is null && Game1.eventUp)
        {
            Add("event_watched", Game1.CurrentEvent is { } active ? PresentedField(active, "id") as string ?? "UNKNOWN" : "UNKNOWN",
                "presentation", "", "Game1.CurrentEvent.presented");
            // A festival's world draws are its clock and its scores, a new string every second: no scene text of hers.
            if (Game1.CurrentEvent is not { } drawn || PresentedField(drawn, "isFestival") is not true)
                foreach (string text in GenericUiRenderedText.Current("world", Game1.CurrentEvent, exact: true))
                    Add("event_watched", Game1.CurrentEvent is { } scene ? PresentedField(scene, "id") as string ?? "UNKNOWN" : "UNKNOWN",
                        "draw:" + PresentedGameText.Hash(text), text, "event.rendered_ui_text", binding.Speaker);
        }
        // The calendar's hovered entry is the native displayed tooltip. Never enumerate hidden day data.
        else if (menu is Billboard && PresentedField(menu, "dailyQuestBoard") is not true)
        {
            string text = PresentedField(menu, "hoverText") as string ?? "";
            if (text.Length > 0 && PresentedField(menu, "calendarDays") is IEnumerable days)
            {
                int index = 0;
                foreach (object day in days)
                {
                    index++;
                    if (day is ClickableComponent component && component.containsPoint(Game1.getMouseX(), Game1.getMouseY()))
                    {
                        string dayId = int.TryParse(component.name, out int nativeDay) && nativeDay is >= 1 and <= 28 ? component.name : index.ToString();
                        Add("calendar_day_read", $"{Game1.year}:{Game1.currentSeason}:{dayId}", "0", text, "Billboard.hoverText");
                    }
                }
            }
        }
        // HUD message objects, each with its own identity; expired and hidden notices are excluded, and so are the
        // game's item pop-ups (messageSubject): their count changes with every pick-up and is no announcement.
        if (Game1.displayHUD)
        {
            foreach (var message in Game1.hudMessages.Where(m => m.timeLeft > 0 && m.transparency > 0 && PresentedField(m, "messageSubject") is null))
                Add("hud_notice", "content:" + PresentedGameText.Hash(message.message + "\0" + message.number), "0",
                    message.message, "Game1.hudMessages", instance: "hud:" + System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(message));
        }
        // The envelope indicator shows presence, not the mailbox's hidden queue length or letter bodies. The
        // same box in view with no envelope (an empty queue) is its own row, so the first flag after it is a day she knows.
        if (menu is null && Game1.player is { } player
            && ReferenceEquals(Game1.currentLocation, Game1.getFarm())
            && player.getMailboxPosition() is { } tile
            && tile.X * 64 >= Game1.viewport.X && tile.X * 64 < Game1.viewport.X + Game1.viewport.Width
            && tile.Y * 64 >= Game1.viewport.Y && tile.Y * 64 < Game1.viewport.Y + Game1.viewport.Height)
            Add(Game1.mailbox.Count > 0 ? "mailbox_visible" : "mailbox_empty_visible", $"mailbox:{tile.X},{tile.Y}", date, "",
                "Game1.mailbox.visibleIndicator");
        // Every family the stream has without the delivery stays, first and unchanged (her
        // voice, the stream notes and the scorecard read them); the exact rows ride beside them.
        result.Insert(0, ScreenText.Bounded(legacy[0]));
        for (int i = 1; i < legacy.Count; i++) result.Insert(i, legacy[i]);
        return result;
    }
}

/// <summary>ADR 230 N1, outside ModEntry so ModEntry's compiler-generated member ordinals (IL policy keys) stay.</summary>
internal static class CalendarBoard
{
    // The same declared-field/property read as ModEntry.PresentedField: presentation fields only, never a call.
    internal static object? Field(object value, string name)
    {
        for (Type? type = value.GetType(); type is not null; type = type.BaseType)
        {
            if (type.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly) is { } field)
                return field.GetValue(value);
            if (type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly) is { } property)
                return property.GetValue(value);
        }
        return null;
    }

    /// <summary>ADR 230 N1: the open calendar board's own day cells, as the menu holds them: each cell's day number
    /// (component name, myID 1-28), whether the game prepared anything for it (the board draws a mark only then), and
    /// that day's display string (Billboard.calendarDayData[myID].HoverText, which performHoverAction copies into the
    /// tooltip; the cell's own hoverText is empty). Never data files, festival tables or another season.
    /// [day, marked 0/1, text]; the text is runtime-only: Python releases one cell only when she reads it.</summary>
    internal static object[]? Cells(object? menu)
    {
        if (menu is not Billboard board || Field(board, "dailyQuestBoard") is true
            || Field(board, "calendarDays") is not IEnumerable days) return null;
        var data = Field(board, "calendarDayData") as IDictionary;
        var cells = new List<object>();
        foreach (object day in days)
        {
            if (day is not ClickableComponent c || c.myID is < 1 or > 28 || c.name != c.myID.ToString()) continue;
            object? entry = data is not null && data.Contains(c.myID) ? data[c.myID] : null;
            string text = entry is null ? "" : Field(entry, "HoverText") as string ?? "";
            cells.Add(new object[] { c.myID, entry is null ? 0 : 1, text.Length <= 512 ? text : text[..512] });
        }
        return cells.Count > 0 ? cells.ToArray() : null;
    }
}
