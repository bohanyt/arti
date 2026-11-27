using System.Collections;
using ArtiStardewBridge.Protocol;

namespace ArtiStardewBridge;

/// <summary>The journal entry and the bundle page that are open now, as the menu itself holds and draws them.
/// Never a closed entry, another bundle, a data file, or a note whose letters she cannot read (the menu's own flag).
/// Outside ModEntry and without lambdas, so ModEntry's compiler-generated member ordinals (IL policy keys) stay.</summary>
internal static class PresentedMenus
{
    internal sealed record Shown(string Kind, string Id, string Text, string Source);

    internal static Shown? Read(object? menu)
    {
        try
        {
            return menu?.GetType().Name switch
            {
                "QuestLog" => Quest(menu),
                "JunimoNoteMenu" => Bundle(menu),
                _ => null,
            };
        }
        catch (Exception)
        {
            return null; // An unsupported game field must never break native input or capture.
        }
    }

    static Shown? Quest(object menu)
    {
        if (CalendarBoard.Field(menu, "questPage") is not int page || page < 0
            || CalendarBoard.Field(menu, "_shownQuest") is not { } quest) return null;
        // Quest's and SpecialOrder's own cached texts, filled by the menu's draw: until both are there, nothing is shown yet.
        var lines = new List<string>();
        Line(lines, CalendarBoard.Field(quest, "_questTitle") ?? CalendarBoard.Field(quest, "_localizedName"));
        object? description = CalendarBoard.Field(quest, "_questDescription") ?? CalendarBoard.Field(quest, "_localizedDescription");
        if (lines.Count == 0 || description is not string { Length: > 0 }) return null;
        // The menu draws its own objective lines only while the entry is not shown as complete.
        bool done = NetValue(CalendarBoard.Field(quest, "completed")) is true
            || NetValue(CalendarBoard.Field(quest, "questState"))?.ToString() is { } state && state != "InProgress";
        if (!done && CalendarBoard.Field(menu, "_objectiveText") is IEnumerable objectives)
            foreach (object? objective in objectives) Line(lines, objective);
        Line(lines, description);
        string id = NetText(CalendarBoard.Field(quest, "id"))
            ?? (NetText(CalendarBoard.Field(quest, "questKey")) is { } key ? "order:" + key : "content:" + PresentedGameText.Hash(lines[0]));
        return new Shown("quest_entry_read", id, string.Join("\n", lines), "QuestLog.shownQuest");
    }

    static Shown? Bundle(object menu)
    {
        if (CalendarBoard.Field(menu, "specificBundlePage") is not true || CalendarBoard.Field(menu, "scrambledText") is not false
            || CalendarBoard.Field(menu, "currentPageBundle") is not { } bundle
            || CalendarBoard.Field(bundle, "bundleIndex") is not int index
            || CalendarBoard.Field(bundle, "label") is not string label || label.Length == 0
            || CalendarBoard.Field(bundle, "ingredients") is not IEnumerable ingredients) return null;
        var lines = new List<string>();
        int done = 0;
        foreach (object? row in ingredients)
        {
            if (row is null) continue;
            int stack = CalendarBoard.Field(row, "stack") is int n ? n : 1;
            int quality = CalendarBoard.Field(row, "quality") is int q ? q : 0;
            int? category = CalendarBoard.Field(row, "category") as int?;
            bool completed = CalendarBoard.Field(row, "completed") is true;
            if (completed) done++;
            string flavor = CalendarBoard.Field(row, "preservesId") is string { Length: > 0 } preserves
                ? NativeBundleKnowledge.IngredientName(preserves, null) + " " : "";
            string name = category == -1 ? stack + "g"
                : flavor + NativeBundleKnowledge.IngredientName(CalendarBoard.Field(row, "id") as string, category)
                    + (stack > 1 ? " x" + stack : "") + (quality > 0 ? " q" + quality : "");
            lines.Add(name + (completed ? " (sudah)" : ""));
        }
        int slots = CalendarBoard.Field(bundle, "numberOfIngredientSlots") is int count ? count : lines.Count;
        // A single-bundle note (the raccoon's) carries no room index: 0 would be another bundle's.
        string id = CalendarBoard.Field(menu, "singleBundleMenu") is true ? "content:" + PresentedGameText.Hash(label) : "bundle:" + index;
        return new Shown("bundle_note_opened", id,
            label + " " + done + "/" + slots + "\n" + string.Join("\n", lines), "JunimoNoteMenu.currentPageBundle");
    }

    static object? NetValue(object? net) => net is null ? null : CalendarBoard.Field(net, "Value");

    static string? NetText(object? net) => NetValue(net) is string { Length: > 0 } text ? text : null;

    static void Line(List<string> lines, object? text)
    {
        if (text is string line && !string.IsNullOrWhiteSpace(line)) lines.Add(line.Trim());
    }
}
