namespace ArtiStardewBridge.Protocol;

/// <summary>One string the game drew in the menu's own draw pass, at the position it was drawn (UI coordinates).</summary>
public readonly record struct PlacedText(string Text, int X, int Y);

/// <summary>A scanned part of the current menu page. <paramref name="Unnamed"/>: the game gave it no label, hover or
/// screen-reader text and it holds no item, and native saw it drawn this frame.</summary>
public readonly record struct PartBox(int Id, int X, int Y, int W, int H, bool Unnamed);

/// <summary>
/// fix137A (ADR 237 A): a part the game shows no words for is described by the words the game draws on it.
/// mabar 2026-11-18 c1 d2: the QuestLog reward box is a bare texture; the game draws its amount beside it, and her page
/// carried our English relabel instead. One rule for every menu, no menu names: a drawn string belongs to the drawn part
/// that contains its draw point (the smallest); a string inside no part belongs to the single nearest part on its row
/// when the gap is at most that part's own width. Only unnamed parts take words; a named part only keeps a string from
/// being given to a neighbour. Tooltip and hover text never reaches this read (GenericUiRenderedText).
/// </summary>
public static class GenericUiPartText
{
    public const int MaxChars = 96;
    /// <summary>The mod's row for a part with no words of its own (arti_runtime/fact_names.py UNNAMED_CONTROL).</summary>
    public const string Unnamed = "kontrol (nama belum diketahui)";

    public static Dictionary<int, string> Describe(IReadOnlyList<PartBox> parts, IReadOnlyList<PlacedText> placed)
    {
        var drawn = parts.Where(p => p.W > 0 && p.H > 0).ToArray();
        var owned = new Dictionary<int, List<PlacedText>>();
        foreach (PlacedText text in placed)
        {
            if (string.IsNullOrWhiteSpace(text.Text)) continue;
            PartBox? owner = null;
            foreach (PartBox p in drawn)
                if (text.X >= p.X && text.X < p.X + p.W && text.Y >= p.Y && text.Y < p.Y + p.H
                    && (owner is null || (long)p.W * p.H < (long)owner.Value.W * owner.Value.H))
                    owner = p;
            if (owner is null)
            {
                var row = drawn.Where(p => text.Y >= p.Y && text.Y < p.Y + p.H)
                    .Select(p => (Part: p, Gap: text.X < p.X ? p.X - text.X : text.X - (p.X + p.W)))
                    .OrderBy(r => r.Gap).ToArray();
                if (row.Length > 0 && row[0].Gap <= row[0].Part.W && (row.Length == 1 || row[1].Gap > row[0].Gap))
                    owner = row[0].Part;
            }
            if (owner is { Unnamed: true } part)
                (owned.TryGetValue(part.Id, out var list) ? list : owned[part.Id] = new()).Add(text);
        }
        var result = new Dictionary<int, string>();
        foreach (var (id, texts) in owned)
        {
            string words = string.Join(" ", texts.OrderBy(t => t.Y).ThenBy(t => t.X)
                .Select(t => string.Join(" ", t.Text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)))
                .Where(t => t.Length > 0).Distinct());
            if (words.Length > 0) result[id] = words.Length <= MaxChars ? words : words[..MaxChars];
        }
        return result;
    }
}
