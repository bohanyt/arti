using System.Security.Cryptography;
using System.Text;

namespace ArtiStardewBridge.Protocol;

/// <summary>ADR 219: lossless pieces of one actually displayed page, on the existing text stream.</summary>
public static class PresentedGameText
{
    public static string Hash(string text) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();

    public static IReadOnlyList<UiTextTickSample> Capture(UiTextTickSample binding, string kind, string id,
        string page, string text, string date, int clock, string place, string save, string? speaker = null)
    {
        if (text.Length == 0 && kind is not ("event_watched" or "mailbox_visible" or "mailbox_empty_visible")) return Array.Empty<UiTextTickSample>();
        // Keep the protocol's existing wire/history limits. No prefix clipping: every piece has its total.
        var pieces = new List<string>();
        if (text.Length == 0) pieces.Add("");
        for (int at = 0; at < text.Length;)
        {
            int size = Math.Min(256, text.Length - at);
            if (at + size < text.Length && char.IsHighSurrogate(text[at + size - 1])) size--;
            pieces.Add(text.Substring(at, size)); at += size;
        }
        string hash = Hash(text);
        string key = kind + ":" + id + ":" + page + ":" + Hash((speaker ?? "") + "\0" + text);
        return pieces.Select((piece, index) => binding with {
            Family = "game_text", Text = piece, Speaker = speaker,
            PageType = key + ":" + index, Options = Array.Empty<Dictionary<string, object>>(),
            Labels = Array.Empty<string>(), DeferredFamily = false, CaptureComplete = binding.CaptureComplete,
            Presented = new() { ["v"] = 1, ["kind"] = kind, ["id"] = id, ["page"] = page,
                ["key"] = key, ["hash"] = hash, ["part"] = index, ["parts"] = pieces.Count,
                ["presentation"] = binding.RuntimeInstanceId + ":" + (binding.MenuInstanceToken ?? "world"),
                ["date"] = date, ["clock"] = clock, ["place"] = place, ["save_id"] = save }
        }).ToArray();
    }
}
