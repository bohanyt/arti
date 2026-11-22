using ArtiStardewBridge.Protocol;
using StardewModdingAPI;

namespace ArtiStardewBridge;

/// <summary>
/// ADR 44: one SMAPI line per change of what a published ui.context had to shed (never per tick). Its own type with
/// its own state so ModEntry's member list, and with it the IL default-deny site keys, stays unchanged.
/// </summary>
internal static class UiContextFitLog
{
    static string last = "";

    public static void Note(IMonitor monitor, UiContextFit fit)
    {
        string key = string.Join(",", fit.Omitted);
        if (key == last)
            return;
        last = key;
        monitor.Log(fit.Trimmed
            ? $"UI_CONTEXT_BUDGET omitted=[{key}] bytes_before={fit.BytesBefore} bytes_after={fit.BytesAfter} budget={UiContextWire.BudgetBytes}"
            : "UI_CONTEXT_BUDGET within budget again", fit.Trimmed ? LogLevel.Warn : LogLevel.Info);
    }
}
