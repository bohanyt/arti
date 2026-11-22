using System.Text.Json;
using StardewModdingAPI;
using StardewValley;

namespace ArtiStardewBridge;

public sealed partial class ModEntry
{
    void ReportNativeCompatibility()
    {
        static Dictionary<string, bool> Flags(object source) => source.GetType().GetProperties()
            .Where(p => p.PropertyType == typeof(bool)).ToDictionary(p => p.Name, p => (bool)p.GetValue(source)!);
        using var stream = typeof(ModEntry).Assembly.GetManifestResourceStream("il_d_rows.json")!;
        using var rows = JsonDocument.Parse(stream);
        var flags = Flags(this.config);
        this.Monitor.Log("NATIVE_COMPAT " + JsonSerializer.Serialize(new {
            game_mvid = typeof(Game1).Module.ModuleVersionId, pinned_game_mvid = PinnedGameMvid,
            game_pin_matches = GamePinned, cursor_shape = PadCursorDrawWitness.Shape,
            background_shape = this.backgroundMove?.FieldShape,
            purchase_shape = new { signature = "ShopMenu.tryToPurchaseItem(ISalable,ISalable,int,int,int):bool",
                bound = TryToPurchaseItemMethod.Value is not null },
            smapi = Constants.ApiVersion.ToString(), minimum_smapi = "4.5.2", harmony = true,
            flags, policy_flags = Flags(this.config.EconomyItemPolicy ?? new()),
            festival_reflex_off = !this.config.FestivalReflexEnabled,
            festival_flag_compatible = !this.config.FestivalReflexEnabled,
            q12 = new { open_menu = Protocol.Q12Capability.State, LB = Protocol.Q12Capability.State,
                RB = Protocol.Q12Capability.State, enabled = this.config.Q12BackendEdgesEnabled },
            native_only = false, d_rows = rows.RootElement.GetProperty("rows").EnumerateArray().Select(r => new {
                id = r.GetProperty("id").GetString(), reachability = r.GetProperty("reachability").GetString(),
                reachable = r.GetProperty("sites").GetArrayLength() > 0 && (!r.GetProperty("flags").EnumerateArray().Any()
                    || r.GetProperty("flags").EnumerateArray().Any(f => !flags.TryGetValue(f.GetString()!, out bool on) || on)),
                replacement = r.GetProperty("replacement").GetString(), delete_phase = r.GetProperty("delete_phase").GetString() }).ToArray()
        }), !this.config.FestivalReflexEnabled && GamePinned ? LogLevel.Info : LogLevel.Warn);
    }
}
