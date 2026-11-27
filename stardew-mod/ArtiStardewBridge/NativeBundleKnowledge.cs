using StardewValley;

namespace ArtiStardewBridge;

/// <summary>Read-only bundle labels/rewards, from the installed note UI and synchronized bundle data (ADR 58).</summary>
static class NativeBundleKnowledge
{
    static readonly string[] RewardKeys = { "Pantry", "Crafts", "FishTank", "Boiler", "Vault", "Bulletin" };
    // Installed 1.6.15 completion-flag consumers; descriptions are conditional effects, not current unlock claims.
    static readonly string[] Effects = { "Restores the farm greenhouse", "Repairs the Mountain bridge to the Quarry",
        "Removes the Mountain glittering boulder; enables the panning introduction",
        "Restores minecart transport", "Repairs the bus service to the Desert", "Increases villager friendship" };

    public static string Reward(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return "none (native reward field empty)";
        // Data/Bundles uses the native item-description grammar; never instantiate a reward item.
        string[] parts = raw.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length != 3 || !int.TryParse(parts[2], out int count) || count <= 0) return "unknown: " + raw;
        string? prefix = parts[0] switch { "O" or "R" => "(O)", "BO" => "(BC)", _ => null };
        if (prefix is null) return "unknown: " + raw;
        var data = ItemRegistry.GetData(prefix + parts[1]);
        string name = data?.DisplayName ?? "";
        return data is null ? "unknown: " + raw : name[..Math.Min(name.Length, 60)] + " x" + count;
    }

    /// <summary>The item's name, as the bundle page's own icon shows it on hover; a category as the progress facts name it.</summary>
    public static string IngredientName(string? id, int? category)
    {
        if (string.IsNullOrEmpty(id)) return category is { } c ? "category " + c : "?";
        return ItemRegistry.GetData(ItemRegistry.QualifyItemId(id) ?? id)?.DisplayName is { Length: > 0 } name ? name : id;
    }

    public static object[] Rooms(string[] rooms, string[] flags)
    {
        var labels = Game1.content.Load<Dictionary<string, string>>("Strings/UI");
        return rooms.Select((room, i) => (object)new Dictionary<string, object> {
            ["room"] = room, ["completion_flag"] = flags[i],
            ["reward"] = labels.GetValueOrDefault("JunimoNote_Reward" + RewardKeys[i], "unknown"),
            ["effect"] = Effects[i],
            ["effect_basis"] = "installed 1.6.15 completion-flag consumers; reward effect, not current access",
            ["source"] = "Strings/UI:JunimoNote_Reward" + RewardKeys[i],
        }).ToArray();
    }

    public static object Note(bool readable)
    {
        // This text is the readable scroll itself, not invented exposition or an observed reading event.
        if (!readable) return new { readable = false, text = (string?)null };
        var text = Game1.content.Load<Dictionary<string, string>>("Strings/StringsFromCSFiles")
            .GetValueOrDefault("JunimoNoteMenu.cs.10786");
        return new { readable = true, text, source = "Strings/StringsFromCSFiles:JunimoNoteMenu.cs.10786",
            relation = "native readable scroll text; not proof this actor has read it" };
    }
}
