namespace ArtiStardewBridge.Protocol;

/// <summary>Bounded game presentation, carried by the existing ui.text occurrence protocol.</summary>
public static class ScreenText
{
    public const int MaxText = 2048;
    public const int MaxHudText = 1024;
    public static string Clip(string? value, int max = MaxText)
    {
        string s = value ?? "";
        if (s.Length <= max) return s;
        if (max > 0 && char.IsHighSurrogate(s[max - 1])) max--;
        return s[..max];
    }

    public static UiTextTickSample Bounded(UiTextTickSample sample)
    {
        var options = sample.Options.Take(8).Select(o => new Dictionary<string, object>(o)
        { ["label"] = Clip(o.GetValueOrDefault("label") as string, 64) }).ToArray();
        var labels = sample.Labels.Take(8).Select(s => Clip(s, 64)).ToArray();
        bool complete = sample.CaptureComplete && sample.Text.Length <= MaxText
            && sample.Options.Count <= 8 && sample.Labels.Count <= 8
            && sample.Options.All(o => (o.GetValueOrDefault("label") as string ?? "").Length <= 64)
            && sample.Labels.All(s => s.Length <= 64);
        return sample with { Text = Clip(sample.Text), Options = options, Labels = labels, CaptureComplete = complete };
    }

    public static UiTextTickSample Hud(UiTextTickSample binding, IReadOnlyList<string> messages)
    {
        string text = string.Join("\n", messages.Take(8));
        return binding with { Family = "hud", Source = "Game1.hudMessages", Text = Clip(text, MaxHudText),
            MenuInstanceToken = "hud", MenuType = null, PageType = null, Speaker = null,
            Options = Array.Empty<Dictionary<string, object>>(), Labels = Array.Empty<string>(), DeferredFamily = false,
            CaptureComplete = messages.Count <= 8 && text.Length <= MaxHudText };
    }
}
