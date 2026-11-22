namespace ArtiHostHelper;

/// <summary>Passive text is OK work; questions retain exact configured native response keys.</summary>
public static class PassiveDialoguePolicy
{
    public static (bool Advance, string? Answer) Plan(bool isQuestion, string? questionKey,
        bool sleeping, bool ownedGift, IReadOnlyDictionary<string, string> configured)
    {
        if (!isQuestion) return (true, null);
        if (ownedGift || questionKey == "Sleep" && sleeping) return (false, "Yes");
        return (false, configured.TryGetValue(questionKey ?? "", out var answer) ? answer : null);
    }
}

/// <summary>Bounds passive native story processing, independent of menu/page churn.</summary>
public sealed class PassiveEventBudget
{
    public const double WallSeconds = 600;
    public const int MaxDismissals = 128;
    double? since;
    int dismissals;

    public bool Observe(bool active, double now)
    {
        if (!active) { since = null; dismissals = 0; return true; }
        since ??= now;
        return now - since.Value < WallSeconds && dismissals < MaxDismissals;
    }

    public void Dismissed() => ++dismissals;
}
